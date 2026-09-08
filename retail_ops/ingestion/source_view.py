"""Retain independently replayed source records without analytical projection."""
from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
import re

from . import batch_store, intake_registry, preview
from .contracts import load_dataset_contracts


RECORDS_PATH = "retail_ops/outputs/published_source_records.json"
PROFILE = "source_records_v1"


def _require(condition, message):
    if not condition:
        raise ValueError("Source record publication: " + message)


def _source_rows(data, fields):
    _require(isinstance(data, bytes), "archived source bytes are required")
    reader = csv.reader(io.StringIO(data.decode("utf-8-sig"), newline=""), strict=True)
    header = next(reader, [])
    _require(header and len(set(header)) == len(header) and set(header) <= fields,
             "source header conflicts with the registered fields")
    rows = {}
    for cells in reader:
        if not cells:
            continue
        _require(len(cells) == len(header), "source row width is invalid")
        rows[reader.line_num] = dict(zip(header, cells))
    _require(rows, "source rows are required")
    return rows


def _records(root, selected):
    contracts = load_dataset_contracts(root)
    _require(isinstance(selected, list) and selected, "select replayed batches")
    records, seen, batches = [], set(), set()
    for item in selected:
        result = item["result"]
        metadata, registration, checked = (result[key] for key in ("metadata", "registration", "preview"))
        context, binding, receipt = (registration[key] for key in ("context", "binding", "receipt"))
        dataset_id = context["dataset_id"]
        _require(dataset_id in contracts and dataset_id in preview.SCHEMAS, "dataset is not registered")
        contract, fields = contracts[dataset_id], preview.SCHEMAS[dataset_id]
        batch_id = result["batch_id"]
        _require(isinstance(batch_id, str) and re.fullmatch(r"batch_[0-9a-f]{32}", batch_id)
                 and batch_id not in batches, "batch identity is invalid or duplicated")
        batches.add(batch_id)
        _require(all(value["status"] == "validated" for value in (result, metadata, checked))
                 and not result["errors"] and not checked["errors"] and not checked["quarantined_records"],
                 "only validated rows without pending errors can be published")
        _require(checked["context"] == context and metadata["dataset_id"] == dataset_id
                 and receipt["dataset_id"] == dataset_id and dataset_id in binding["dataset_ids"]
                 and context["grain"] == contract.grain and context["ranking_basis"] == contract.ranking_basis,
                 "dataset and source context disagree")
        _require(metadata["batch_id"] == batch_id and receipt["upload_id"] == result["upload_id"]
                 and receipt["binding_id"] == binding["binding_id"] and binding["store_id"] == context["store_id"],
                 "batch, upload or store identity disagrees")
        _require(metadata["source_system"] == binding["source_system"] == contract.source_system
                 and metadata["source_name"] == contract.source_name
                 and metadata["snapshot_semantics"] == contract.snapshot_semantics,
                 "registered source semantics disagree")
        for suffix in ("start", "end"):
            _require(context["period_" + suffix] == receipt["period_" + suffix] == metadata["coverage_" + suffix],
                     "record coverage and upload coverage disagree")
        start, end = (preview._date(context["period_" + suffix]) for suffix in ("start", "end"))
        _require(start <= end, "source coverage is reversed")
        digest = hashlib.sha256(item["data"]).hexdigest()
        _require(all(value["file_sha256"] == digest for value in (result, metadata, checked, receipt)),
                 "source bytes do not match their recorded digest")
        for field in ("source_page", "extracted_at", "mapping_version"):
            _require(metadata[field] == receipt[field], "source metadata disagrees: " + field)
        _require(checked["mapping_version"] == metadata["mapping_version"], "mapping version disagrees")
        source = {"batch_id": batch_id, **{key: binding[key] for key in (
            "binding_id", "source_system", "source_account_id", "source_store_id")},
            **{key: metadata[key] for key in ("source_page", "extracted_at", "file_sha256", "mapping_version")}}
        _require(all(isinstance(value, str) and value and value == value.strip() for value in source.values()),
                 "source identity and provenance require non-empty text")
        if "aggregation_scope" in receipt:
            _require(receipt["mapping_version"] == "canonical_csv_v2" and receipt["aggregation_scope"] is not None,
                     "aggregation scope requires a reviewed version 2 receipt object")
        scope = intake_registry.validate_aggregation_scope(receipt.get("aggregation_scope"))
        source.update(aggregation_scope=scope,
                      aggregation_scope_sha256=intake_registry.aggregation_scope_sha256(scope))
        raw_rows = _source_rows(item["data"], fields)
        validated = checked["validated_records"]
        _require(isinstance(validated, list) and validated, "validated records are required")
        source_lines = set()
        for row in validated:
            _require(isinstance(row, dict) and set(row) == {"source_line_end", "record"},
                     "validated row fields are incomplete or unregistered")
            record, line = row["record"], row["source_line_end"]
            _require(type(line) is int and line in raw_rows and line not in source_lines,
                     "source line is absent or duplicated")
            source_lines.add(line)
            _require(isinstance(record, dict) and set(record) == fields, "canonical record fields disagree")
            for field in fields:
                normalized = preview._value(field, raw_rows[line].get(field))
                _require(batch_store._json(record[field]) == batch_store._json(normalized),
                         "canonical value differs from its source line: " + field)
            _require(all(record[field] == context[field] for field in ("store_id", "period_start", "period_end")),
                     "record belongs to a different store or source window")
            key = (dataset_id, *(record[field] for field in contract.key_fields))
            _require(all(value is not None for value in key) and key not in seen,
                     "record logical key is incomplete or duplicated")
            seen.add(key)
            records.append((key, {"dataset_id": dataset_id, "record": dict(record),
                                  "source": {**source, "source_line_end": line}}))
        _require(source_lines == set(raw_rows), "the validated selection omits a source row")
    records.sort(key=lambda item: (*item[0], item[1]["source"]["batch_id"]))
    return [value for _, value in records]


def source_records(root: Path, selected: list[dict]) -> list[dict]:
    """Build exact records from replayed batches; never read repository data CSVs."""
    try:
        return _records(Path(root), selected)
    except (KeyError, TypeError, AttributeError, UnicodeError, csv.Error) as exc:
        raise ValueError("Source record publication: malformed replayed source metadata or records") from exc


def build_source_view(root: Path, selected: list[dict]) -> dict:
    """Write one deterministic source artifact in the private publication staging root."""
    records = source_records(root, selected)
    data = (json.dumps(records, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    path = Path(root) / RECORDS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"profile": PROFILE, "records_path": RECORDS_PATH, "record_count": len(records),
            "datasets": sorted({record["dataset_id"] for record in records})}
