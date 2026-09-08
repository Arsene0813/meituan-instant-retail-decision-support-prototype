"""Publish selected intake batches and analyze one verified publication."""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from rac.src.grounded_pipeline import run_grounded_pipeline
from retail_ops.scripts.generate_demo2_retail_memory_facts import build_facts
from retail_ops.sql_runtime import QUERIES, run_query

from .intake_registry import _unique
from .publication import open_publication, publish
from .publication_recipe import FACTS_PATH, SQL_OUTPUTS, csv_bytes


def analyze_publication(root: Path, directory: Path, publication_id: str, question: str,
                        query_name: str = "02_demo2_cross_store_comparability.sql") -> dict:
    """Pin once; derive and reconcile every result inside that private view."""
    with open_publication(root, directory, publication_id) as pinned:
        if query_name not in SQL_OUTPUTS:
            raise ValueError("query is not registered for publication analysis")
        columns, rows = run_query(query_name, root=pinned.root)
        expected = list(csv.reader(io.StringIO(csv_bytes(columns, rows).decode(), newline=""), strict=True))
        with (pinned.root / SQL_OUTPUTS[query_name]).open(encoding="utf-8-sig", newline="") as handle:
            saved = list(csv.reader(handle, strict=True))
        if saved != expected:
            raise ValueError(f"{query_name}: published SQL differs from selected source records")
        facts = build_facts(pinned.root)
        saved_facts = json.loads((pinned.root / FACTS_PATH).read_bytes(), object_pairs_hook=_unique)
        if facts != saved_facts:
            raise ValueError("Published facts differ from selected SQL and source records")
        state = run_grounded_pipeline(question, root=pinned.root)
        return {"publication_id": pinned.publication_id,
                "sql": {"query": query_name, "columns": columns, "rows": rows},
                "facts": facts, "rac": state}


def _selection(path: Path) -> list[str]:
    value = json.loads(path.expanduser().read_bytes(), object_pairs_hook=_unique)
    if not isinstance(value, dict) or set(value) != {"batch_ids"}:
        raise ValueError("selection requires exactly {batch_ids: [...]} with explicit batch IDs")
    ids = value["batch_ids"]
    if (not isinstance(ids, list) or not ids
            or any(not isinstance(item, str) or not item or item != item.strip() for item in ids)
            or len(ids) != len(set(ids))):
        raise ValueError("batch_ids must be a non-empty list of distinct explicit batch IDs")
    return ids


def _save_result(path: Path, payload: str, *, root: Path, directory: Path) -> Path:
    path = path.expanduser().absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("analysis output must not use symbolic links")
    for forbidden in (root.resolve(), directory.expanduser().resolve()):
        if path.resolve() == forbidden or forbidden in path.resolve().parents:
            raise ValueError("keep analysis output outside the repository and publication directory")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # Publish a complete file while refusing to replace an existing result.
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("publish", help="publish an explicit set of validated batch IDs")
    create.add_argument("--database", type=Path, required=True)
    create.add_argument("--directory", type=Path, required=True)
    create.add_argument("--selection", type=Path, required=True)
    inspect = commands.add_parser("inspect", help="verify and inspect a publication by ID")
    inspect.add_argument("--directory", type=Path, required=True)
    inspect.add_argument("--publication-id", required=True)
    analyze = commands.add_parser("analyze", help="run SQL, facts and RAC against one publication")
    analyze.add_argument("--directory", type=Path, required=True)
    analyze.add_argument("--publication-id", required=True)
    analyze.add_argument("--question", required=True)
    analyze.add_argument("--query", choices=QUERIES, default="02_demo2_cross_store_comparability.sql")
    analyze.add_argument("--output", type=Path, help="save complete JSON to a new external file")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[2]
    try:
        directory = args.directory.expanduser()
        if args.command == "publish":
            result = publish(root, args.database.expanduser(), directory, _selection(args.selection))
        elif args.command == "inspect":
            with open_publication(root, directory, args.publication_id) as pinned:
                result = pinned.manifest
        else:
            result = analyze_publication(root, directory, args.publication_id, args.question, args.query)
        payload = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if args.command == "analyze" and args.output:
            saved = _save_result(args.output, payload, root=root, directory=directory)
            print(json.dumps({"publication_id": result["publication_id"], "output": str(saved)},
                             ensure_ascii=False, indent=2))
        else:
            print(payload, end="")
    except (ValueError, OSError, sqlite3.Error, csv.Error) as exc:
        parser.exit(2, f"Cannot process publication: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
