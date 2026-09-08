from __future__ import annotations

import calendar
import csv
import json
import math
import os
import re
import stat
import sys
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from retail_ops.ingestion import preview
from retail_ops.sql_runtime import prepare_rows, read_source

OUTPUT_DIR = Path("retail_ops/outputs")
COMPARABILITY_OUTPUT = OUTPUT_DIR / "demo2_cross_store_comparability_output.csv"

OUTPUT_PATH = OUTPUT_DIR / "generated_demo2_retail_memory_facts.json"

SOURCE_PATH = "retail_ops/outputs/demo2_cross_store_comparability_output.csv"
TOP_SEARCH_TERMS_SOURCE_PATH = "retail_ops/data/demo2_top_search_terms.csv"
TOP_SKUS_BY_AMOUNT_SOURCE_PATH = "retail_ops/data/demo2_top_skus_by_transaction_amount.csv"
LINEAGE_PATH = "retail_ops/TECHNICAL_APPENDIX.md"

VISIBILITY_ENTRY_LIMITATIONS = [
    "{period_text} same-period diagnostic record.",
    "Traffic-source user counts may overlap.",
    (
        "Visibility and entry metrics are reviewed together across "
        "exposure, ranking, entry, and search-entry context."
    ),
]

ACTIVITY_LEVER_LIMITATIONS = [
    (
        "Activity mechanism details and promotion cycle dates can extend "
        "the current activity context."
    ),
    (
        "Activity fields record backend activity involvement and related "
        "cost measures within the store-period operating profile."
    ),
    (
        "activity_cost_ratio_pct follows the platform-defined cost-ratio "
        "formula recorded in DATA_DICTIONARY.md."
    ),
]

TRANSACTION_CONVERSION_LIMITATIONS = [
    "{period_text} transaction and conversion outcomes.",
    (
        "order_conversion_rate_pct follows the backend definition "
        "recorded in DATA_DICTIONARY.md."
    ),
    (
        "Transaction amount, transaction orders, payment, and conversion "
        "fields form the monthly transaction profile."
    ),
]

TOP3_SKU_LIMITATIONS = [
    (
        "Top-3 SKU transaction-amount evidence provides listed "
        "product-mix context."
    ),
    (
        "sku_category_note records the manual category note attached "
        "to the listed SKU evidence."
    ),
    (
        "English SKU names provide helper translations alongside "
        "the original SKU names."
    ),
]

ATTRIBUTION_GUARD_LIMITATIONS = [
    "{period_text} same-period diagnostic record.",
    (
        "comparison_scope_flag records row-level readiness for "
        "the same-period diagnostic."
    ),
    (
        "The combined metrics preserve visibility, entry, transaction, "
        "conversion, activity, and listed-SKU context."
    ),
]



# These are existing SQL-derived dictionary fields, not inferred input mappings.
DERIVED_DECIMALS = {
    "search_entry_rate_pct", "search_entry_share_pct", "activity_order_share_pct",
    "top3_sku_transaction_amount", "top3_sku_transaction_amount_share_pct",
}
DIAGNOSTIC_TEXT = {"comparison_scope_flag", "comparison_limit_notes"}
DIAGNOSTIC_FIELDS = preview.SCHEMAS["demo2_store_period_metrics"] | DERIVED_DECIMALS | DIAGNOSTIC_TEXT


def as_number(field: str, value: Any) -> int | float | None:
    """Keep blanks, exact counts and decimal values that round-trip through JSON."""
    if field in preview.COUNTS | preview.RANKS:
        return preview._value(field, value)
    if field not in preview.DECIMALS | DERIVED_DECIMALS:
        raise ValueError(f"unregistered fact numeric field: {field}")
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError(f"{field}: expected exact numeric text")
    # SQL output can use exponent notation; canonical source CSVs are checked
    # separately by their registered source rules before reaching this helper.
    text = str(value).strip()
    if not re.fullmatch(r"[+-]?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?", text):
        raise ValueError(f"{field}: invalid SQL numeric text")
    number = Decimal(text)
    if not number.is_finite():
        raise ValueError(f"{field}: non-finite value")
    floating = float(number)
    if not math.isfinite(floating) or Decimal(str(floating)) != number:
        raise ValueError(f"{field}: value loses precision in the current JSON number format")
    return floating


def read_diagnostics(root: Path) -> list[dict[str, Any]]:
    path = root / COMPARABILITY_OUTPUT
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle, strict=True)
        header = next(reader, [])
        if (not header or len(header) != len(set(header))
                or set(header) - DIAGNOSTIC_FIELDS or not preview.KEYS <= set(header)):
            raise ValueError("Demo 2 output: missing period metadata, duplicate or unregistered columns")
        rows = []
        for cells in reader:
            if not cells:
                continue
            if len(cells) != len(header):
                raise ValueError(f"Demo 2 output: wrong cell count at line {reader.line_num}")
            rows.append(dict(zip(header, cells)))
    # Reuse the current month/key rules without treating derived output fields
    # as source columns. This also rejects duplicate store-period records.
    keys = sorted(preview.KEYS)
    prepare_rows("demo2_store_period_metrics", keys,
                 [{key: row[key] for key in keys} for row in rows])
    checked = []
    for index, row in enumerate(rows, 2):
        try:
            record = {}
            for field in DIAGNOSTIC_FIELDS:
                value = row.get(field)
                if field in preview.COUNTS | preview.RANKS | preview.DECIMALS | DERIVED_DECIMALS:
                    record[field] = as_number(field, value)
                elif field in DIAGNOSTIC_TEXT:
                    record[field] = value.strip() if value and value.strip() else None
                else:
                    record[field] = preview._value(field, value)
            checked.append(record)
        except (ValueError, ArithmeticError) as exc:
            raise ValueError(f"Demo 2 output record {index}: {field}: {exc}") from exc
    return checked


def period_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return row["store_id"], row["period_start"], row["period_end"]


def group_source(root: Path, dataset_id: str, rank_field: str) -> dict[tuple, list[dict]]:
    fields, rows = read_source(root, dataset_id)
    checked = prepare_rows(dataset_id, fields, rows)
    grouped: dict[tuple, list[dict]] = {}
    for row in checked:
        for field in preview.COUNTS | preview.RANKS | preview.DECIMALS:
            if field in row:
                row[field] = as_number(field, row[field])
        grouped.setdefault(period_key(row), []).append(row)
    for items in grouped.values():
        items.sort(key=lambda row: row[rank_field])
    return grouped


def make_fact(
    *,
    row: dict[str, Any],
    entity_id: str,
    slot: str,
    value: str,
    observed_values: dict[str, Any],
    calculation: str,
    source_fields: list[str],
    confidence: str,
    limitations: list[str],
    source_path: str = SOURCE_PATH,
    supporting_source_paths: list[str] | None = None,
) -> dict[str, Any]:
    start = date.fromisoformat(row["period_start"])
    period_text = f"{calendar.month_name[start.month]} {start.year}"
    fact: dict[str, Any] = {
        "kind": "retail_memory_fact",
        "type": "retail_metric_profile",
        "entity_id": entity_id,
        "slot": slot,
        "period_label": row["period_month"],
        "period_start": row["period_start"],
        "period_end": row["period_end"],
        "value": value,
        "observed_values": observed_values,
        "calculation": calculation,
        "source_fields": source_fields,
        "confidence": confidence,
        "source_path": source_path,
        "lineage_path": LINEAGE_PATH,
        "limitations": [item.format(period_text=period_text) for item in limitations],
        "is_active": True,
        "period_granularity": "month",
    }

    if supporting_source_paths:
        additional_paths = [
            path
            for path in dict.fromkeys(
                supporting_source_paths
            )
            if path != source_path
        ]

        if additional_paths:
            fact["supporting_source_paths"] = (
                additional_paths
            )

    return fact


def build_facts(root: Path = ROOT) -> list[dict[str, Any]]:
    comparability_rows = read_diagnostics(root)
    top_search_by_period = group_source(root, "demo2_top_search_terms", "search_term_rank")
    top_skus_by_period = group_source(root, "demo2_top_skus_by_transaction_amount", "sku_rank")

    facts: list[dict[str, Any]] = []

    for row in comparability_rows:
        store_id = row["store_id"]
        entity_id = f"store_{store_id}"
        key = period_key(row)
        start = date.fromisoformat(row["period_start"])
        period_text = f"{calendar.month_name[start.month]} {start.year}"

        top_search_terms = [
            {
                "search_term_rank": item["search_term_rank"],
                "search_term": item["search_term"],
                "search_term_en": item["search_term_en"],
                "search_term_exposure_times": item["search_term_exposure_times"],
                "search_term_click_times": item["search_term_click_times"],
                "search_term_order_times": item["search_term_order_times"],
            }
            for item in top_search_by_period.get(key, [])
        ]

        top_skus_by_amount = [
            {
                "sku_rank": item["sku_rank"],
                "sku_name": item["sku_name"],
                "sku_name_en": item["sku_name_en"],
                "sku_category_note": item["sku_category_note"],
                "sku_transaction_amount": item["sku_transaction_amount"],
            }
            for item in top_skus_by_period.get(key, [])
            if item["sku_rank"] <= 3
        ]


        facts.append(
            make_fact(
                row=row,
                entity_id=entity_id,
                slot="visibility_entry_profile",
                value=(
                    f"Store {store_id}'s {period_text} visibility and entry profile records exposure, rank, entry, "
                    f"and search-entry metrics across the being seen → being entered stages. "
                    f"It should be read alongside transaction, conversion, activity, and SKU evidence."
                ),
                observed_values={
                    "region_type": row["region_type"],
                    "store_type": row["store_type"],
                    "exposure_users": row["exposure_users"],
                    "exposure_times": row["exposure_times"],
                    "store_average_rank": row["store_average_rank"],
                    "entry_users": row["entry_users"],
                    "entry_times": row["entry_times"],
                    "entry_conversion_rate_pct": row["entry_conversion_rate_pct"],
                    "search_exposure_users": row["search_exposure_users"],
                    "search_average_rank": row["search_average_rank"],
                    "search_entry_users": row["search_entry_users"],
                    "search_entry_rate_pct": row["search_entry_rate_pct"],
                    "search_entry_share_pct": row["search_entry_share_pct"],
                    "top_search_terms": top_search_terms,
                },
                calculation=(
                    "search_entry_rate_pct = search_entry_users / search_exposure_users * 100; "
                    "search_entry_share_pct = search_entry_users / entry_users * 100"
                ),
                source_fields=[
                    "region_type",
                    "store_type",
                    "exposure_users",
                    "exposure_times",
                    "store_average_rank",
                    "entry_users",
                    "entry_times",
                    "entry_conversion_rate_pct",
                    "search_exposure_users",
                    "search_average_rank",
                    "search_entry_users",
                    "search_entry_rate_pct",
                    "search_entry_share_pct",
                    "search_term_rank",
                    "search_term",
                    "search_term_en",
                    "search_term_exposure_times",
                    "search_term_click_times",
                    "search_term_order_times",
                ],
                confidence="high",
                limitations=VISIBILITY_ENTRY_LIMITATIONS,
                supporting_source_paths=[TOP_SEARCH_TERMS_SOURCE_PATH],
            )
        )

        facts.append(
            make_fact(
                row=row,
                entity_id=entity_id,
                slot="activity_lever_profile",
                value=(
                    f"Store {store_id}'s {period_text} activity fields record activity involvement and related cost measures. "
                    f"These metrics should be treated as operating-tool evidence, not as proof that activity caused the store's result."
                ),
                observed_values={
                    "activity_original_transaction_amount": row["activity_original_transaction_amount"],
                    "activity_orders": row["activity_orders"],
                    "transaction_orders": row["transaction_orders"],
                    "activity_cost": row["activity_cost"],
                    "merchant_subsidy_amount": row["merchant_subsidy_amount"],
                    "platform_subsidy_amount": row["platform_subsidy_amount"],
                    "activity_cost_ratio_pct": row["activity_cost_ratio_pct"],
                    "activity_order_share_pct": row["activity_order_share_pct"],
                },
                calculation=(
                    "activity_order_share_pct = activity_orders / transaction_orders * 100; "
                    "activity_cost_ratio_pct follows the data dictionary formula activity_cost / activity_original_transaction_amount * 100"
                ),
                source_fields=[
                    "activity_original_transaction_amount",
                    "activity_orders",
                    "transaction_orders",
                    "activity_cost",
                    "merchant_subsidy_amount",
                    "platform_subsidy_amount",
                    "activity_cost_ratio_pct",
                    "activity_order_share_pct",
                ],
                confidence="high",
                limitations=ACTIVITY_LEVER_LIMITATIONS,
            )
        )

        facts.append(
            make_fact(
                row=row,
                entity_id=entity_id,
                slot="transaction_conversion_profile",
                value=(
                    f"Store {store_id}'s {period_text} transaction and conversion profile records transaction amount, order volume, "
                    f"order conversion rate, payment conversion, and average order value as transaction outcomes in the current monthly evidence. "
                    f"It should be read alongside visibility, entry, activity, and SKU evidence."
                ),
                observed_values={
                    "transaction_amount": row["transaction_amount"],
                    "transaction_orders": row["transaction_orders"],
                    "average_order_value": row["average_order_value"],
                    "order_users": row["order_users"],
                    "order_times": row["order_times"],
                    "order_conversion_rate_pct": row["order_conversion_rate_pct"],
                    "order_amount": row["order_amount"],
                    "payment_users": row["payment_users"],
                    "payment_amount": row["payment_amount"],
                    "payment_conversion_rate_pct": row["payment_conversion_rate_pct"],
                    "entry_users": row["entry_users"],
                },
                calculation=(
                    "average_order_value = transaction_amount / transaction_orders; "
                    "order_conversion_rate_pct follows the backend definition order_users / entry_users * 100; "
                    "payment_conversion_rate_pct = payment_users / order_users * 100"
                ),
                source_fields=[
                    "transaction_amount",
                    "transaction_orders",
                    "average_order_value",
                    "order_users",
                    "order_times",
                    "order_conversion_rate_pct",
                    "order_amount",
                    "payment_users",
                    "payment_amount",
                    "payment_conversion_rate_pct",
                    "entry_users",
                ],
                confidence="high",
                limitations=TRANSACTION_CONVERSION_LIMITATIONS,
            )
        )

        facts.append(
            make_fact(
                row=row,
                entity_id=entity_id,
                slot="top3_sku_product_mix_note",
                value=(
                    f"Store {store_id}'s {period_text} top-3 SKU transaction-amount evidence "
                    f"provides listed product-mix context from the current source records."
                ),
                observed_values={
                    "top3_sku_transaction_amount": row["top3_sku_transaction_amount"],
                    "transaction_amount": row["transaction_amount"],
                    "top3_sku_transaction_amount_share_pct": row["top3_sku_transaction_amount_share_pct"],
                    "top_skus_by_transaction_amount": top_skus_by_amount,
                },
                calculation="top3_sku_transaction_amount_share_pct = top3_sku_transaction_amount / transaction_amount * 100",
                source_fields=[
                    "sku_rank",
                    "sku_name",
                    "sku_name_en",
                    "sku_transaction_amount",
                    "transaction_amount",
                    "top3_sku_transaction_amount",
                    "top3_sku_transaction_amount_share_pct",
                    "sku_category_note",
                ],
                confidence="medium",
                source_path=SOURCE_PATH,
                supporting_source_paths=[TOP_SKUS_BY_AMOUNT_SOURCE_PATH],
                limitations=TOP3_SKU_LIMITATIONS,
            )
        )

        facts.append(
            make_fact(
                row=row,
                entity_id=entity_id,
                slot="single_metric_attribution_guard",
                value=(
                    f"Store {store_id}'s {period_text} operating profile combines visibility, "
                    f"entry, transaction, conversion, activity, and listed-SKU evidence. "
                    f"The comparison scope is {row['comparison_scope_flag']}, with diagnostic "
                    f"notes: {row['comparison_limit_notes']}."
                ),
                observed_values={
                    "transaction_amount": row["transaction_amount"],
                    "transaction_orders": row["transaction_orders"],
                    "average_order_value": row["average_order_value"],
                    "store_average_rank": row["store_average_rank"],
                    "entry_conversion_rate_pct": row["entry_conversion_rate_pct"],
                    "order_conversion_rate_pct": row["order_conversion_rate_pct"],
                    "search_entry_share_pct": row["search_entry_share_pct"],
                    "activity_order_share_pct": row["activity_order_share_pct"],
                    "activity_cost_ratio_pct": row["activity_cost_ratio_pct"],
                    "top3_sku_transaction_amount_share_pct": row["top3_sku_transaction_amount_share_pct"],
                    "comparison_scope_flag": row["comparison_scope_flag"],
                    "comparison_limit_notes": row["comparison_limit_notes"],
                },
                calculation=(
                    "comparison_limit_notes are derived from activity involvement and top-3 SKU concentration checks "
                    "in the Demo 2 SQL output"
                ),
                source_fields=[
                    "transaction_amount",
                    "transaction_orders",
                    "average_order_value",
                    "store_average_rank",
                    "entry_conversion_rate_pct",
                    "order_conversion_rate_pct",
                    "search_entry_share_pct",
                    "activity_order_share_pct",
                    "activity_cost_ratio_pct",
                    "top3_sku_transaction_amount_share_pct",
                    "comparison_scope_flag",
                    "comparison_limit_notes",
                ],
                confidence="high",
                limitations=ATTRIBUTION_GUARD_LIMITATIONS,
            )
        )
    return facts


def main(root: Path = ROOT) -> int:
    facts = build_facts(root)
    payload = json.dumps(facts, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    output_path = root / OUTPUT_PATH
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=output_path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
        if output_path.exists():
            temporary.chmod(stat.S_IMODE(output_path.stat().st_mode))
        os.replace(temporary, output_path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print(f"Wrote {len(facts)} facts to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
