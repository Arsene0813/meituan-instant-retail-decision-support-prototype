"""Validate canonical CSV rows before connecting uploads to analysis storage."""

from __future__ import annotations

import argparse
import calendar
import csv
import hashlib
import io
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from .contracts import DatasetContractError, load_dataset_contracts


# These are reviewed dictionary fields, not fields inferred from an upload.
TEXT = set("store_id region_type store_type sku_name sku_name_en "
           "sku_category_note search_term search_term_en".split())
COUNTS = set("transaction_orders exposure_users exposure_times entry_users "
             "entry_times order_users order_times payment_users search_exposure_users "
             "search_entry_users merchant_list_exposure_users merchant_list_entry_users "
             "activity_zone_exposure_users activity_zone_entry_users order_page_exposure_users "
             "order_page_entry_users other_exposure_users other_entry_users activity_orders "
             "full_refund_orders refund_orders_all_or_partial sales_volume "
             "search_term_exposure_times search_term_click_times search_term_order_times".split())
DECIMALS = set("transaction_amount estimated_income_proxy average_order_value "
               "store_average_rank entry_conversion_rate_pct order_conversion_rate_pct "
               "order_amount payment_amount payment_conversion_rate_pct search_average_rank "
               "merchant_list_average_rank activity_original_transaction_amount activity_cost "
               "merchant_subsidy_amount platform_subsidy_amount activity_cost_ratio_pct "
               "refund_amount sku_transaction_amount".split())
RANKS = {"sku_rank", "search_term_rank", "business_district_rank"}
PERIOD = {"period_start", "period_end", "period_month"}
KEYS = {"store_id", *PERIOD}
SKU = KEYS | {"sku_rank", "sku_name", "sku_transaction_amount", "sales_volume", "sku_category_note"}
SEARCH = KEYS | {"search_term_rank", "search_term", "search_term_en",
                 "search_term_exposure_times", "search_term_click_times", "search_term_order_times"}
STORE = (COUNTS | DECIMALS | KEYS | {"region_type", "store_type"}) - {
    "sales_volume", "sku_transaction_amount", "search_term_exposure_times",
    "search_term_click_times", "search_term_order_times",
}
SCHEMAS = {
    "store_a_monthly_metrics": STORE,
    "demo2_store_period_metrics": STORE | {"business_district_rank"},
    "store_period_panel_metrics": STORE | {"business_district_rank"},
    "store_a_top_skus": SKU,
    "demo2_top_skus_by_transaction_amount": SKU | {"sku_name_en"},
    "demo2_top_skus_by_sales_volume": SKU | {"sku_name_en"},
    "demo2_top_search_terms": SEARCH,
}
ROUTES = {
    "store_a_monthly_metrics": ("store_period", None),
    "demo2_store_period_metrics": ("store_period", None),
    "store_period_panel_metrics": ("store_period", None),
    "store_a_top_skus": ("store_sku_period", "not_recorded_in_current_fixture"),
    "demo2_top_skus_by_transaction_amount": ("store_sku_period", "transaction_amount"),
    "demo2_top_skus_by_sales_volume": ("store_sku_period", "sales_volume"),
    "demo2_top_search_terms": ("store_search_term_period", None),
}
IGNORED = {"有效订单数", "无效订单数"}


@dataclass(frozen=True)
class UploadContext:
    """Confirmed upload scope; must be supplied independently of model output."""

    dataset_id: str
    store_id: str
    period_start: str
    period_end: str
    grain: str
    ranking_basis: str | None = None


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("expected YYYY-MM-DD")
    return date.fromisoformat(value)


def _value(field, value):
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("expected source text or an exact number")
    text = str(value).strip()
    if field in TEXT:
        if not isinstance(value, str):
            raise ValueError("expected text")
        return text
    if field in {"period_start", "period_end"}:
        return _date(text).isoformat()
    if field == "period_month":
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}", text):
            raise ValueError("expected YYYY-MM")
        _date(text + "-01")
        return text
    if field in COUNTS | RANKS:
        if not re.fullmatch(r"[0-9]+", text):
            raise ValueError("expected a non-negative integer")
        number = int(text)
        if field in RANKS and number < 1:
            raise ValueError("rank must be positive")
        return number
    if field in DECIMALS and re.fullmatch(r"[+-]?[0-9]+(?:\.[0-9]+)?", text):
        return Decimal(text)
    raise ValueError("expected a decimal without percent signs or unit conversion")


def _normalize(raw, fields, keys, context):
    errors = []
    for field in sorted(set(raw) - fields - IGNORED):
        errors.append(f"unregistered field: {field}")
    record = {}
    for field in sorted(fields):
        try:
            record[field] = _value(field, raw.get(field))
        except (ValueError, ArithmeticError) as exc:
            errors.append(f"{field}: {exc}")
            record[field] = None
    # Current canonical CSV sources require a month label as well as their key.
    # Do not fill missing source metadata from the upload context.
    for field in (*keys, "period_month"):
        if record.get(field) is None:
            errors.append(f"missing required period metadata: {field}" if field == "period_month"
                          else f"missing key: {field}")
    for field in ("store_id", "period_start", "period_end"):
        if record.get(field) != getattr(context, field):
            errors.append(f"{field} conflicts with confirmed upload scope")
    if record.get("period_month") not in (None, context.period_start[:7]):
        errors.append("period_month conflicts with confirmed upload scope")
    return record, errors


def preview_csv(root: Path, data: bytes, context: UploadContext, proposals=None):
    """Return a preview only. No database, source CSV or memory facts are written."""
    result = {
        "mode": "preview", "status": "quarantined", "context": asdict(context),
        "file_sha256": hashlib.sha256(data).hexdigest(),
        "mapping_version": "canonical_csv_v1",
        "errors": [], "validated_records": [], "quarantined_records": [],
    }
    try:
        contracts = load_dataset_contracts(root)
        contract = contracts.get(context.dataset_id)
        fields = SCHEMAS.get(context.dataset_id)
        if contract is None or fields is None:
            raise ValueError("dataset_id requires a registered contract and explicit field schema")
        expected_route = ROUTES[context.dataset_id]
        expected_keys = ("store_id", "period_start", "period_end")
        if expected_route[0] == "store_sku_period":
            expected_keys += ("sku_rank",)
        elif expected_route[0] == "store_search_term_period":
            expected_keys += ("search_term_rank",)
        if (contract.grain, contract.ranking_basis) != expected_route or contract.key_fields != expected_keys:
            raise ValueError("dataset contract changed; review its route and field schema together")
        if (context.grain, context.ranking_basis) != (contract.grain, contract.ranking_basis):
            raise ValueError("grain or ranking_basis conflicts with the dataset contract")
        if not isinstance(context.store_id, str) or not context.store_id.strip():
            raise ValueError("confirmed store_id is required")
        if context.store_id != context.store_id.strip():
            raise ValueError("confirmed store_id must not contain surrounding whitespace")
        start, end = _date(context.period_start), _date(context.period_end)
        if start.day != 1 or end != date(start.year, start.month, calendar.monthrange(start.year, start.month)[1]):
            raise ValueError("this preview supports complete calendar-month source windows")
        field_types = {field: next(kind for kind, names in (
            ("text", TEXT), ("count", COUNTS), ("decimal", DECIMALS), ("rank", RANKS), ("period", PERIOD),
        ) if field in names) for field in sorted(fields)}
        schema = {"contract": asdict(contract), "fields": field_types, "mapping_version": result["mapping_version"]}
        schema["normalizer_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        result["schema_sha256"] = hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest()
        result["dictionary_sha256"] = hashlib.sha256((root / "retail_ops/data/DATA_DICTIONARY.md").read_bytes()).hexdigest()
        reader = csv.reader(io.StringIO(data.decode("utf-8-sig"), newline=""), strict=True)
        header = next(reader, [])
        # Drop the two excluded columns before retaining rows or diagnostics.
        indices = [i for i, field in enumerate(header) if field not in IGNORED]
        kept_header = [header[i] for i in indices]
        if not kept_header or any(not h or h != h.strip() for h in kept_header):
            raise ValueError("CSV requires non-empty canonical column names")
        if len(kept_header) != len(set(kept_header)):
            raise ValueError("duplicate CSV column names")
        source_rows = []
        for cells in reader:
            if not cells:
                continue
            if len(cells) != len(header):
                raise ValueError(f"CSV row ending at line {reader.line_num} has the wrong number of cells")
            source_rows.append((reader.line_num, {header[i]: cells[i] for i in indices}))
        if not source_rows:
            raise ValueError("CSV has no data rows")
        if proposals is not None and (not isinstance(proposals, list) or len(proposals) != len(source_rows)):
            raise ValueError("proposals must have one item per source row")
    except (ValueError, TypeError, UnicodeError, OSError, csv.Error, DatasetContractError) as exc:
        result["errors"].append(str(exc))
        return result

    candidates = []
    for index, (line, raw) in enumerate(source_rows):
        record, errors = _normalize(raw, fields, contract.key_fields, context)
        if proposals is not None:
            proposed = proposals[index]
            if not isinstance(proposed, dict) or set(proposed) != {"dataset_id", "grain", "ranking_basis", "record"}:
                errors.append("proposal requires dataset_id, grain, ranking_basis and record")
            elif any(proposed[k] != getattr(context, k) for k in ("dataset_id", "grain", "ranking_basis")):
                errors.append("proposal routing conflicts with confirmed upload scope")
            elif not isinstance(proposed["record"], dict) or any(not isinstance(k, str) for k in proposed["record"]):
                errors.append("proposal record must have text field names")
            else:
                proposed_record, proposed_errors = _normalize(proposed["record"], fields, contract.key_fields, context)
                errors.extend("proposal: " + error for error in proposed_errors)
                if proposed_record != record:
                    errors.append("proposal values differ from the source row")
        key = tuple(record.get(field) for field in contract.key_fields)
        candidates.append((line, raw, record, errors, key))
    key_counts = Counter(key for _, _, _, _, key in candidates if None not in key)
    for _, _, _, errors, key in candidates:
        if None not in key and key_counts[key] > 1:
            errors.append("duplicate logical key in this upload")
    batch_has_errors = any(errors for _, _, _, errors, _ in candidates)
    for line, raw, record, errors, _ in candidates:
        if batch_has_errors and not errors:
            errors.append("another row needs review; the complete upload is held")
        if errors:
            result["quarantined_records"].append({"source_line_end": line, "source_record": raw, "errors": errors})
        else:
            result["validated_records"].append({"source_line_end": line, "record": record})
    if not result["quarantined_records"]:
        result["status"] = "validated"
    return result


def preview_json(result):
    # Decimal text is exact; SQL adapters must bind it with the registered type.
    return json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False,
                      default=lambda value: format(value, "f") if isinstance(value, Decimal) else _bad_json(value))


def _bad_json(value):
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result and key not in IGNORED:
            raise ValueError("duplicate JSON field name")
        if key not in IGNORED:
            result[key] = value
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    for name in ("dataset-id", "store-id", "period-start", "period-end", "grain"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--ranking-basis")
    parser.add_argument("--proposals", type=Path)
    args = parser.parse_args()
    try:
        proposals = json.loads(args.proposals.read_text(encoding="utf-8"), parse_float=Decimal,
                               object_pairs_hook=_json_object) if args.proposals else None
        scope = UploadContext(**{key: getattr(args, key) for key in UploadContext.__dataclass_fields__})
        result = preview_csv(Path(__file__).resolve().parents[2], args.input.read_bytes(), scope, proposals)
        result["source_file"] = str(args.input)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Cannot preview upload: {exc}\n")
    print(preview_json(result))
    return 0 if result["status"] == "validated" else 2


if __name__ == "__main__":
    raise SystemExit(main())
