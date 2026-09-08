"""Read a selected date range from one publication without saving a query."""
from __future__ import annotations

import argparse
import calendar
from contextlib import closing
from datetime import date
from decimal import Decimal, localcontext
import json
from pathlib import Path
import re
import sqlite3

from .contracts import load_dataset_contracts
from .intake_registry import _unique, aggregation_scope_sha256, validate_aggregation_scope
from .preview import COUNTS, KEYS, SCHEMAS, _value
from .publication import open_publication
from .source_windows import validate_source_dataset

POLICY_PATH = "retail_ops/contracts/range_query.v1.json"
RECORDS_PATH = "retail_ops/outputs/published_source_records.json"
SCOPE_FIELDS = ("binding_id", "source_system", "source_account_id", "source_store_id", "source_page",
                "aggregation_scope_sha256")
SOURCE_FIELDS = {*SCOPE_FIELDS, "batch_id", "source_line_end", "extracted_at", "file_sha256", "mapping_version",
                 "aggregation_scope"}
ADDITIVE_FIELDS = {"transaction_amount", "transaction_orders"}


def _day(value: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("query dates require YYYY-MM-DD")
    return date.fromisoformat(value)


def _range(start: str, end: str) -> tuple[date, date]:
    first, last = _day(start), _day(end)
    if last < first:
        raise ValueError("period_end is earlier than period_start")
    return first, last


def month_dates(month: str) -> tuple[str, str]:
    """A calendar month is only a shortcut for its inclusive start/end dates."""
    if not isinstance(month, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}", month):
        raise ValueError("month requires YYYY-MM")
    first = _day(month + "-01")
    return first.isoformat(), date(first.year, first.month, calendar.monthrange(first.year, first.month)[1]).isoformat()


def _load_policy(root: Path) -> dict:
    policy = json.loads((root / POLICY_PATH).read_bytes(), object_pairs_hook=_unique)
    expected = {
        "query_contract_version": "1", "publication_profile": "source_records_v1",
        "records_path": RECORDS_PATH, "default_fields": "all_registered_non_key_fields",
        "source_scope_fields": list(SCOPE_FIELDS),
        "summary_policies": {
            "transaction_amount": {"operation": "sum_non_overlapping_days", "grain": "store_period",
                "dictionary_definition": "所选时间周期内，该账号所选择条件下门店的当天支付且当天未取消的订单用户实际支付金额。"},
            "transaction_orders": {"operation": "sum_non_overlapping_days", "grain": "store_period",
                "dictionary_definition": "所选时间周期内，该账号所选择条件下门店的当天支付且当天未取消的订单量。"}},
        "sum_requirements": ["every_requested_day_present", "one_record_per_day", "one_source_scope",
                             "all_values_non_null", "no_overlapping_source_window", "reviewed_aggregation_scope"],
        "aggregation_scope_requirement": "operator_reviewed_timezone_and_selection_conditions",
        "exact_window_policy": "return_one_store_period_source_value_unchanged",
        "other_field_policy": "retain_source_records_without_aggregation",
        "ranking_policy": "retain_separate_source_rows_and_ranks_without_cross_window_merge",
        "partial_overlap_policy": "show_separately_without_proration",
        "query_storage": "no_persistent_query_record",
    }
    if policy != expected:
        raise ValueError("range query policy changed; review registered summary fields and all query rules together")
    return policy


def _validate_entries(payload, contracts) -> list[dict]:
    if not isinstance(payload, list):
        raise ValueError("published source records require a list")
    for item in payload:
        if not isinstance(item, dict) or set(item) != {"dataset_id", "record", "source"}:
            raise ValueError("invalid published source record")
        dataset = item["dataset_id"]
        if not isinstance(dataset, str) or dataset not in contracts or dataset not in SCHEMAS:
            raise ValueError("published dataset is not registered")
        record, source = item["record"], item["source"]
        if not isinstance(record, dict) or set(record) != SCHEMAS[dataset]:
            raise ValueError("published source fields differ from the registered schema")
        for field, value in record.items():
            canonical = _value(field, value)
            if isinstance(canonical, Decimal):
                canonical = format(canonical, "f")
            if type(canonical) is not type(value) or canonical != value:
                raise ValueError(f"published {field} is not canonical")
        if any(record.get(field) is None for field in contracts[dataset].key_fields):
            raise ValueError("published source key is missing")
        _range(record["period_start"], record["period_end"])
        if not isinstance(source, dict) or set(source) != SOURCE_FIELDS:
            raise ValueError("published source lineage is incomplete")
        for field in SOURCE_FIELDS - {"source_line_end", "aggregation_scope", "aggregation_scope_sha256"}:
            if not isinstance(source[field], str) or not source[field] or source[field] != source[field].strip():
                raise ValueError(f"published source {field} is invalid")
        if type(source["source_line_end"]) is not int or source["source_line_end"] < 2:
            raise ValueError("published source line is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", source["file_sha256"]):
            raise ValueError("published source hash is invalid")
        # Operator review records the filters and timezone; it does not authenticate
        # the backend or independently prove that an export used those filters.
        scope = validate_aggregation_scope(source["aggregation_scope"])
        if scope is not None and source["mapping_version"] != "canonical_csv_v2":
            raise ValueError("published aggregation scope requires canonical_csv_v2")
        if source["aggregation_scope_sha256"] != aggregation_scope_sha256(scope):
            raise ValueError("published aggregation scope does not match its digest")
        month = validate_source_dataset(dataset, source["mapping_version"],
                                        record["period_start"], record["period_end"])
        if record["period_month"] != month:
            raise ValueError("published period_month disagrees with the actual source window")
        if source["source_system"] != contracts[dataset].source_system:
            raise ValueError("published source system differs from its dataset contract")
    return payload


def _coverage(windows, first: date, last: date) -> dict:
    """Merge source windows without expanding them into individual dates."""
    left, right = first.toordinal(), last.toordinal()
    intervals = sorted((max(left, _day(start).toordinal()), min(right, _day(end).toordinal()))
                       for start, end in windows)
    merged = []
    for start, end in intervals:
        if start > end:
            continue
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(end, merged[-1][1])
        else:
            merged.append([start, end])
    missing, cursor = [], left
    for start, end in merged:
        if start > cursor:
            missing.append({"period_start": date.fromordinal(cursor).isoformat(),
                            "period_end": date.fromordinal(start - 1).isoformat()})
        cursor = end + 1
    if cursor <= right:
        missing.append({"period_start": date.fromordinal(cursor).isoformat(),
                        "period_end": date.fromordinal(right).isoformat()})
    return {"requested_days": right - left + 1,
            "covered_days": sum(end - start + 1 for start, end in merged),
            "missing_ranges": missing}


def _window(entry):
    return entry["record"]["period_start"], entry["record"]["period_end"]


def _scope_sort_key(values):
    return tuple("" if value is None else value for value in values)


def _source_windows(entries):
    # Several ranks in one source report describe the same window, not overlap.
    result = {}
    for item in entries:
        start, end = _window(item)
        source = item["source"]
        key = (start, end, source["batch_id"], *(source[field] for field in SCOPE_FIELDS))
        result[key] = {"batch_id": source["batch_id"], "period_start": start, "period_end": end}
    return [result[key] for key in sorted(result, key=_scope_sort_key)]


def _overlaps(windows):
    conflicts = []
    for index, left in enumerate(windows):
        for right in windows[index + 1:]:
            if right["period_start"] > left["period_end"]:
                break
            conflicts.append({"left": left, "right": right,
                              "period_start": max(left["period_start"], right["period_start"]),
                              "period_end": min(left["period_end"], right["period_end"])})
    return conflicts


def _sum_exact(values, field):
    if field in COUNTS:
        return sum(values)
    numbers = [Decimal(value) for value in values]
    # The default Decimal context can round a valid large integer or long fraction.
    integer_digits = max(max(number.adjusted() + 1, 1) for number in numbers)
    fraction_digits = max(max(-number.as_tuple().exponent, 0) for number in numbers)
    with localcontext() as context:
        context.prec = integer_digits + fraction_digits + len(str(len(numbers))) + 2
        return format(sum(numbers, Decimal(0)), "f")


def _store_result(store_id, entries, fields, contract, first, last):
    contained, crossing = [], []
    for item in entries:
        start, end = _window(item)
        (contained if start >= first.isoformat() and end <= last.isoformat() else crossing).append(item)
    source_windows = _source_windows(entries)
    conflicts = _overlaps(source_windows)
    scope_keys = sorted({tuple(item["source"][field] for field in SCOPE_FIELDS) for item in entries},
                        key=_scope_sort_key)
    scopes = [dict(zip(SCOPE_FIELDS, key)) for key in scope_keys]
    coverage = _coverage([_window(item) for item in contained], first, last)
    ambiguities = []
    if conflicts:
        ambiguities.append("overlapping_source_windows")
    if crossing:
        ambiguities.append("partially_overlapping_source_windows")
    if len(scopes) > 1:
        ambiguities.append("different_source_scopes")
    metrics = {}
    for field in fields:
        present = [item for item in contained if item["record"][field] is not None]
        metric_coverage = _coverage([_window(item) for item in present], first, last)
        metric_coverage["null_source_records"] = len(contained) - len(present)
        value, basis = None, "source_records"
        if contract.grain != "store_period":
            reason = "ranked_records_only"
        elif ambiguities:
            reason = ("aggregation_scope_unreviewed" if field in ADDITIVE_FIELDS and
                      any(item["source"]["aggregation_scope"] is None for item in entries)
                      else "ambiguous_source_scope_or_window")
        elif not contained:
            reason = "no_contained_source_records"
        elif len(contained) == 1 and _window(contained[0]) == (first.isoformat(), last.isoformat()):
            value, basis = contained[0]["record"][field], "reported_window"
            reason = "reported_value" if value is not None else "missing_reported_value"
        elif field not in ADDITIVE_FIELDS:
            reason = "aggregation_not_registered"
        elif any(item["source"]["aggregation_scope"] is None for item in contained):
            reason = "aggregation_scope_unreviewed"
        elif coverage["missing_ranges"]:
            reason = "missing_dates"
        elif any(start != end for start, end in map(_window, contained)):
            reason = "aggregation_requires_daily_source_records"
        elif len(contained) != coverage["requested_days"] or len({_window(item) for item in contained}) != len(contained):
            reason = "duplicate_daily_records"
        elif metric_coverage["null_source_records"]:
            reason = "missing_metric_values"
        else:
            value = _sum_exact([item["record"][field] for item in contained], field)
            basis, reason = "sum_non_overlapping_days", "complete_daily_source_values"
        metrics[field] = {"value": value, "basis": basis, "reason": reason, "coverage": metric_coverage}
    # Preserve identifiers, rank keys and the requested source values together.
    selected = sorted(set(fields) | set(contract.key_fields) | {"period_month"})
    def project(item):
        return {"record": {key: item["record"][key] for key in selected}, "source": item["source"]}
    return {"store_id": store_id, "records": [project(item) for item in contained],
            "overlapping_records": [project(item) for item in crossing],
            "coverage": coverage, "source_scopes": scopes, "source_window_overlaps": conflicts,
            "ambiguities": ambiguities, "metrics": metrics}


def _query_scope(store_ids, period_start, period_end):
    first, last = _range(period_start, period_end)
    if (not isinstance(store_ids, list) or not store_ids or
            any(not isinstance(store, str) or not store or store != store.strip() for store in store_ids)
            or len(store_ids) != len(set(store_ids))):
        raise ValueError("store_ids require distinct, non-empty canonical store IDs")
    stores = sorted(store_ids)
    return first, last, stores


def _query_pinned(pinned, dataset_id, stores, first, last, fields):
    _load_policy(pinned.root)
    summary = pinned.manifest.get("summary", {})
    if (summary.get("profile") != "source_records_v1" or summary.get("records_path") != RECORDS_PATH):
        raise ValueError("date-range queries require a source_records_v1 publication")
    contracts = load_dataset_contracts(pinned.root)
    if not isinstance(dataset_id, str) or dataset_id not in contracts or dataset_id not in SCHEMAS:
        raise ValueError("dataset_id requires a registered contract and field schema")
    contract = contracts[dataset_id]
    allowed = SCHEMAS[dataset_id] - KEYS - set(contract.key_fields)
    if fields is None:
        selected = sorted(allowed)
    elif (not isinstance(fields, list) or not fields or
          any(not isinstance(field, str) or field not in allowed for field in fields)
          or len(fields) != len(set(fields))):
        raise ValueError("fields require distinct registered non-key field names")
    else:
        selected = sorted(fields)
    entries = _validate_entries(json.loads((pinned.root / RECORDS_PATH).read_bytes(),
                                           object_pairs_hook=_unique), contracts)
    if type(summary.get("record_count")) is not int or summary["record_count"] != len(entries):
        raise ValueError("published source record count does not match its manifest")
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute("PRAGMA temp_store = MEMORY")
        connection.execute("CREATE TABLE source_records (dataset_id TEXT, store_id TEXT, period_start TEXT, period_end TEXT, batch_id TEXT, source_line_end INTEGER, payload TEXT)")
        connection.executemany("INSERT INTO source_records VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(item["dataset_id"], item["record"]["store_id"], *_window(item), item["source"]["batch_id"],
              item["source"]["source_line_end"], json.dumps(item, ensure_ascii=False, sort_keys=True, allow_nan=False))
             for item in entries])
        rows = connection.execute(
            "SELECT payload FROM source_records WHERE dataset_id = ? AND store_id IN (" +
            ", ".join("?" for _ in stores) + ") AND period_start <= ? AND period_end >= ? "
            "ORDER BY store_id, period_start, period_end, batch_id, source_line_end, payload",
            [dataset_id, *stores, last.isoformat(), first.isoformat()]).fetchall()
    found = {store: [] for store in stores}
    for row in rows:
        item = json.loads(row[0])
        found[item["record"]["store_id"]].append(item)
    return {"publication_id": pinned.publication_id, "dataset_id": dataset_id,
            "period_start": first.isoformat(), "period_end": last.isoformat(), "fields": selected,
            "stores": [_store_result(store, found[store], selected, contract, first, last) for store in stores]}


def query_pinned_publication(pinned, dataset_id: str, store_ids: list[str],
                             period_start: str, period_end: str,
                             fields: list[str] | None = None) -> dict:
    """Select from an already verified private publication; never open a new version."""
    first, last, stores = _query_scope(store_ids, period_start, period_end)
    return _query_pinned(pinned, dataset_id, stores, first, last, fields)


def query_publication(root: Path, directory: Path, publication_id: str, dataset_id: str,
                      store_ids: list[str], period_start: str, period_end: str,
                      fields: list[str] | None = None) -> dict:
    """Pin once, select with parameterized in-memory SQL, return without saving."""
    first, last, stores = _query_scope(store_ids, period_start, period_end)
    with open_publication(root, directory, publication_id) as pinned:
        return _query_pinned(pinned, dataset_id, stores, first, last, fields)


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
    parser.add_argument("--field", action="append")
    args = parser.parse_args(argv)
    try:
        if args.month:
            if args.period_end:
                raise ValueError("use either --month or --period-start with --period-end")
            start, end = month_dates(args.month)
        else:
            if not args.period_end:
                raise ValueError("--period-start requires --period-end")
            start, end = args.period_start, args.period_end
        result = query_publication(Path(__file__).resolve().parents[2], args.directory.expanduser(),
                                   args.publication_id, args.dataset_id, args.store_id, start, end, args.field)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False))
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(2, f"Cannot query publication: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
