"""Read selected windows once and recompute a RAC review without saving a report."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3

from .publication import open_publication
from .source_query import _query_scope, _range, month_dates, query_pinned_publication


def analyze_publication_range(root: Path, directory: Path, publication_id: str,
                              dataset_id: str, store_ids: list[str],
                              period_start: str, period_end: str,
                              fields: list[str] | None = None, *,
                              baseline_start: str | None = None,
                              baseline_end: str | None = None) -> dict:
    """Both windows and every RAC operand come from one verified private copy.

    Query results, source context and model-produced evidence are not accepted as
    inputs. The selected publication is the source for each invocation.
    """
    first, _, stores = _query_scope(store_ids, period_start, period_end)
    if (baseline_start is None) != (baseline_end is None):
        raise ValueError("baseline requires both period_start and period_end")
    if baseline_start is not None:
        _, last_baseline = _range(baseline_start, baseline_end)
        if len(stores) != 1:
            raise ValueError("a baseline comparison requires exactly one store")
        if last_baseline >= first:
            raise ValueError("baseline must end before the selected current window begins")
    from rac.src.range_evidence_review import default_review_fields, review_range_queries, validate_range_state

    with open_publication(root, directory, publication_id) as pinned:
        selected_fields = default_review_fields(pinned.root) if fields is None else fields
        current = query_pinned_publication(pinned, dataset_id, stores, period_start, period_end, selected_fields)
        baseline = None
        if baseline_start is not None:
            baseline = query_pinned_publication(pinned, dataset_id, stores,
                                                baseline_start, baseline_end, selected_fields)
        state = review_range_queries(current, baseline, root=pinned.root)
        validate_range_state(state, current, baseline, root=pinned.root)
        return {"publication_id": pinned.publication_id, "query": current,
                "baseline": baseline, "rac": state}


def _dates(month, start, end, label):
    if month is not None:
        if start is not None or end is not None:
            raise ValueError(f"{label}: select a month or explicit start/end dates")
        return month_dates(month)
    if start is None or end is None:
        raise ValueError(f"{label}: both start and end dates are required")
    _range(start, end)
    return start, end


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--publication-id", required=True)
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--store-id", action="append", required=True)
    period = parser.add_mutually_exclusive_group(required=True)
    period.add_argument("--month")
    period.add_argument("--period-start")
    parser.add_argument("--period-end")
    baseline = parser.add_mutually_exclusive_group()
    baseline.add_argument("--baseline-month")
    baseline.add_argument("--baseline-start")
    parser.add_argument("--baseline-end")
    parser.add_argument("--field", action="append")
    args = parser.parse_args(argv)
    try:
        start, end = _dates(args.month, args.period_start, args.period_end, "current window")
        old_start = old_end = None
        if any(value is not None for value in (args.baseline_month, args.baseline_start, args.baseline_end)):
            old_start, old_end = _dates(args.baseline_month, args.baseline_start, args.baseline_end, "baseline")
        result = analyze_publication_range(Path(__file__).resolve().parents[2], args.directory.expanduser(),
            args.publication_id, args.dataset_id, args.store_id, start, end, args.field,
            baseline_start=old_start, baseline_end=old_end)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(2, f"Cannot review source windows: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
