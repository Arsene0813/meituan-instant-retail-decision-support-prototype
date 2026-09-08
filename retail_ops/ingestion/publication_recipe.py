"""Build the explicitly registered retail evidence views for a publication."""
from __future__ import annotations

import csv
import hashlib
import io
from importlib.metadata import version
import json
import sqlite3
import sys
from pathlib import Path

from . import preview
from .contracts import load_dataset_contracts
from .intake_registry import _unique
from retail_ops.sql_runtime import QUERIES, run_query
from retail_ops.scripts.generate_demo2_retail_memory_facts import build_facts

PROFILE_PATH = "retail_ops/contracts/publication.v1.json"
SQL_OUTPUTS = {
    "01_store_a_month_over_month_diagnostic.sql": "retail_ops/outputs/store_a_demo1_sql_output.csv",
    "02_demo2_cross_store_comparability.sql": "retail_ops/outputs/demo2_cross_store_comparability_output.csv",
    "03_store_period_panel_coverage.sql": "retail_ops/outputs/store_period_panel_coverage_output.csv",
    "04_repeated_window_panel_summary.sql": "retail_ops/outputs/repeated_window_panel_summary_output.csv",
}
FACTS_PATH = "retail_ops/outputs/generated_demo2_retail_memory_facts.json"
STATIC_FILES = (
    PROFILE_PATH, "retail_ops/contracts/datasets.v1.json",
    "retail_ops/contracts/range_query.v1.json",
    "retail_ops/data/DATA_DICTIONARY.md", "retail_ops/TECHNICAL_APPENDIX.md",
    "retail_ops/COMPARABILITY_GATE_V0.md", "rac/README.md",
    "rac/schemas/cognition_state.schema.json",
    *("retail_ops/sql/" + name for name in SQL_OUTPUTS),
)
CODE_FILES = (
    "retail_ops/ingestion/contracts.py", "retail_ops/ingestion/preview.py",
    "retail_ops/ingestion/source_windows.py", "retail_ops/ingestion/source_view.py",
    "retail_ops/ingestion/source_query.py",
    "retail_ops/ingestion/intake_registry.py", "retail_ops/ingestion/batch_store.py",
    "retail_ops/ingestion/publication.py", "retail_ops/ingestion/publication_recipe.py",
    "retail_ops/ingestion/publication_cli.py", "retail_ops/sql_runtime.py",
    "retail_ops/scripts/generate_demo2_retail_memory_facts.py",
    "rac/src/csv_evidence_validation.py", "rac/src/demo2_csv_grounding.py",
    "rac/src/store_a_csv_grounding.py", "rac/src/evidence_review.py",
    "rac/src/local_evidence_resolver.py", "rac/src/state_validation.py",
    "rac/src/grounded_pipeline.py", "rac/src/mock_pipeline.py",
    "api/retail_entity_scope.py", "api/retail_evidence_scope.py",
)


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()


def _read(root, name):
    path = root / name
    if root.resolve() not in path.resolve().parents or any(
        item.is_symlink() for item in (path, *path.parents)
    ):
        raise ValueError("publication recipe files require contained regular paths")
    return path.read_bytes()


def recipe_files(root: Path) -> dict[str, bytes]:
    """Only copy reviewed static resources; record executable code as hashes."""
    import duckdb
    root = Path(root)
    files = {name: _read(root, name) for name in STATIC_FILES}
    files["publication_runtime.json"] = _json_bytes({
        "code_sha256": {name: hashlib.sha256(_read(root, name)).hexdigest() for name in CODE_FILES},
        "engines": {"python": ".".join(map(str, sys.version_info[:2])),
                    "sqlite": sqlite3.sqlite_version, "duckdb": duckdb.__version__,
                    "jsonschema": version("jsonschema"), "numpy": version("numpy")},
    })
    return files


def _profile(root):
    profile = json.loads((root / PROFILE_PATH).read_bytes(), object_pairs_hook=_unique)
    if (not isinstance(profile, dict) or set(profile) != {"publication_profile_version", "views"}
            or profile["publication_profile_version"] != "1" or not isinstance(profile["views"], list)):
        raise ValueError("publication profile is not registered")
    contracts = load_dataset_contracts(root)
    views = {}
    for view in profile["views"]:
        if (not isinstance(view, dict) or set(view) != {"dataset_id", "input_dataset_ids", "missing_input"}
                or view["missing_input"] != "empty_table"):
            raise ValueError("publication view requires an explicit input and absence rule")
        target = view["dataset_id"]
        sources = view["input_dataset_ids"]
        if (not isinstance(target, str) or target not in preview.SCHEMAS or target not in contracts
                or target in views or not isinstance(sources, list) or not sources
                or any(not isinstance(source, str) for source in sources) or len(set(sources)) != len(sources)):
            raise ValueError("publication view contains unknown or duplicate datasets")
        target_contract = contracts[target]
        if target_contract.source_path != f"retail_ops/data/{target}.csv":
            raise ValueError("publication target path must match the registered consumer path")
        for source in sources:
            if source not in contracts or source not in preview.SCHEMAS:
                raise ValueError("publication source dataset is not registered")
            source_contract = contracts[source]
            attributes = ("source_system", "grain", "key_fields", "dimension_fields", "ranking_basis",
                          "overlap_group", "overlap_policy", "snapshot_semantics")
            if (preview.SCHEMAS[source] != preview.SCHEMAS[target]
                    or any(getattr(source_contract, key) != getattr(target_contract, key) for key in attributes)):
                raise ValueError("explicit publication projection conflicts with canonical field contracts")
        views[target] = view
    if set(views) != set(preview.SCHEMAS) or set(QUERIES) != set(SQL_OUTPUTS):
        raise ValueError("publication profile requires the reviewed seven views and four queries")
    return contracts, views


def csv_bytes(columns, rows):
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n", quoting=csv.QUOTE_ALL)
    writer.writerow(columns)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _write(root, name, data):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def build_evidence_view(root: Path, selected: list[dict]) -> dict:
    """Materialize only replayed batches, then derive SQL and Demo 2 facts."""
    contracts, views = _profile(root)
    materialized, lineage, used_batches = {}, {}, set()
    for target, view in sorted(views.items()):
        columns = sorted(preview.SCHEMAS[target])
        rows = []
        for batch in selected:
            result = batch["result"]
            if result["metadata"]["dataset_id"] not in view["input_dataset_ids"]:
                continue
            used_batches.add(result["batch_id"])
            for item in result["preview"]["validated_records"]:
                record = item["record"]
                if set(record) != set(columns):
                    raise ValueError("replayed record does not match the registered target field set")
                key = tuple(record[field] for field in contracts[target].key_fields)
                rows.append((key, record, result["batch_id"], item["source_line_end"]))
        rows.sort(key=lambda row: row[0])
        if len({row[0] for row in rows}) != len(rows):
            raise ValueError("multiple selected records target the same canonical key")
        data = csv_bytes(columns, [[row[1][column] for column in columns] for row in rows])
        path = contracts[target].source_path
        _write(root, path, data)
        reader = csv.reader(io.StringIO(data.decode(), newline=""))
        next(reader)
        target_lineage = []
        for row, cells in zip(rows, reader):
            target_lineage.append({"key": dict(zip(contracts[target].key_fields, row[0])),
                                   "output_line_end": reader.line_num,
                                   "batch_id": row[2], "source_line_end": row[3]})
        lineage[target] = target_lineage
        materialized[target] = {"source_path": path, "input_dataset_ids": view["input_dataset_ids"],
                                "row_count": len(rows), "batch_ids": sorted({row[2] for row in rows}),
                                "missing_input": not rows}
    if used_batches != {batch["result"]["batch_id"] for batch in selected}:
        raise ValueError("a selected batch has no registered publication view")
    query_summary = {}
    for query, path in SQL_OUTPUTS.items():
        columns, rows = run_query(query, root=root)
        _write(root, path, csv_bytes(columns, rows))
        query_summary[query] = {"output_path": path, "row_count": len(rows), "columns": columns}
    facts = build_facts(root)
    _write(root, FACTS_PATH, _json_bytes(facts))
    return {"views": materialized, "row_lineage": lineage, "queries": query_summary,
            "facts": {"output_path": FACTS_PATH, "count": len(facts)}}
