from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, NamedTuple

from rac.src.csv_evidence_validation import (
    MONTH_KEYS,
    canonical_rows,
    exact_output_number,
    read_csv,
    require_fields,
    validate_month_rows,
)
from retail_ops.ingestion import preview
from retail_ops.scripts.generate_demo2_retail_memory_facts import (
    DERIVED_DECIMALS,
    DIAGNOSTIC_FIELDS,
)


SNAPSHOT_SOURCE_PATH = (
    "retail_ops/outputs/"
    "demo2_cross_store_comparability_output.csv"
)
REPEATED_WINDOW_SOURCE_PATH = (
    "retail_ops/outputs/"
    "repeated_window_panel_summary_output.csv"
)
STORE_IDS = ("B", "C", "D", "E", "F")
PERIOD_MONTH = "2026-03"


class RecordSpec(NamedTuple):
    source_path: str
    grounding_role: str
    key_fields: tuple[str, ...]
    fields: tuple[str, ...]


FACTOR_RECORD_SPECS = {
    "same_reporting_period": RecordSpec(
        SNAPSHOT_SOURCE_PATH,
        "context_evidence",
        ("store_id", "period_month"),
        (
            "period_start",
            "period_end",
            "period_month",
        ),
    ),
    "store_type": RecordSpec(
        SNAPSHOT_SOURCE_PATH,
        "context_evidence",
        ("store_id", "period_month"),
        ("store_type",),
    ),
    "order_volume": RecordSpec(
        SNAPSHOT_SOURCE_PATH,
        "quantitative_evidence",
        ("store_id", "period_month"),
        ("transaction_orders",),
    ),
    "transaction_amount": RecordSpec(
        SNAPSHOT_SOURCE_PATH,
        "quantitative_evidence",
        ("store_id", "period_month"),
        ("transaction_amount",),
    ),
    "activity_intensity": RecordSpec(
        SNAPSHOT_SOURCE_PATH,
        "quantitative_evidence",
        ("store_id", "period_month"),
        (
            "activity_orders",
            "activity_order_share_pct",
            "activity_cost",
            "activity_cost_ratio_pct",
        ),
    ),
    "region_context": RecordSpec(
        SNAPSHOT_SOURCE_PATH,
        "context_evidence",
        ("store_id", "period_month"),
        ("region_type",),
    ),
    "sku_structure": RecordSpec(
        SNAPSHOT_SOURCE_PATH,
        "product_mix_evidence",
        ("store_id", "period_month"),
        (
            "top3_sku_transaction_amount",
            "top3_sku_transaction_amount_share_pct",
        ),
    ),
    "repeated_reporting_windows": RecordSpec(
        REPEATED_WINDOW_SOURCE_PATH,
        "quantitative_evidence",
        ("store_id",),
        (
            "observed_month_count",
            "feb_transaction_amount",
            "mar_transaction_amount",
            "apr_transaction_amount",
            "feb_transaction_orders",
            "mar_transaction_orders",
            "apr_transaction_orders",
        ),
    ),
}



def validate_snapshot(headers, rows):
    require_fields(headers, MONTH_KEYS)
    if set(headers) - DIAGNOSTIC_FIELDS:
        raise ValueError("Demo 2 diagnostic output contains unregistered fields")
    validate_month_rows(rows)
    for index, row in enumerate(rows, 2):
        for field in headers:
            try:
                if field in preview.DECIMALS | DERIVED_DECIMALS:
                    exact_output_number(row[field])
                elif field in preview.COUNTS | preview.RANKS | preview.TEXT | preview.PERIOD:
                    preview._value(field, row[field])
            except (ValueError, ArithmeticError) as exc:
                raise ValueError(f"row {index}: {field}: {exc}") from exc


def reconcile_repeated_records(root, rows):
    # The saved pivot does not carry full monthly date keys. Verify the
    # actual registered panel records before accepting its selected cells.
    panel_headers, panel_rows = canonical_rows(root, "store_period_panel_metrics")
    require_fields(panel_headers, ("transaction_amount", "transaction_orders"))
    periods = {"feb": "2026-02", "mar": "2026-03", "apr": "2026-04"}
    by_store = {
        store_id: {
            row["period_month"]: row
            for row in panel_rows if row["store_id"] == store_id
        }
        for store_id in STORE_IDS
    }
    fields = FACTOR_RECORD_SPECS["repeated_reporting_windows"].fields
    for row in rows:
        store_id = row["store_id"]
        if store_id not in STORE_IDS:
            continue
        source = by_store[store_id]
        if set(source) != set(periods.values()):
            raise ValueError(f"{store_id}: panel must cover the three declared full months")
        for field in fields:
            count = field == "observed_month_count" or field.endswith("_transaction_orders")
            observed = exact_output_number(row[field], count=count)
            if field == "observed_month_count":
                expected = len(source)
            else:
                prefix, canonical_field = field.split("_", 1)
                expected = preview._value(canonical_field, source[periods[prefix]][canonical_field])
            if observed != expected:
                raise ValueError(f"{store_id}: {field} conflicts with current canonical panel")

def supports_demo2_record(
    question_type: str | None,
    factor_id: str,
    source_path: str,
) -> bool:
    spec = FACTOR_RECORD_SPECS.get(factor_id)
    return bool(
        question_type == "comparability_judgment"
        and spec
        and source_path == spec.source_path
    )


def resolve_demo2_record(
    packet: dict[str, Any],
    *,
    factor_id: str,
    root: Path,
) -> dict[str, Any]:
    spec = FACTOR_RECORD_SPECS[factor_id]
    path = root / spec.source_path

    result: dict[str, Any] = {
        "factor_id": factor_id,
        "evidence_id": packet.get("evidence_id"),
        "source_type": "csv",
        "source_path": spec.source_path,
        "source_exists": path.is_file(),
        "grounding_role": spec.grounding_role,
        "grounding_status": "source_missing",
        "snippets": [],
        "record_scope": {},
        "evidence_fields": list(spec.fields),
        "evidence_values": [],
        "original_claim_supported": packet.get(
            "claim_supported"
        ),
        "original_limitations": packet.get(
            "limitations",
            [],
        ),
        "resolver_limitations": [
            (
                "Deterministic B-F CSV record selection "
                "for the declared comparison scope."
            ),
            (
                "Interpretation remains bounded by the "
                "source contract and reporting scope."
            ),
        ],
    }

    if not path.is_file():
        result["absolute_path_checked"] = str(path)
        return result

    try:
        headers, rows = read_csv(root, spec.source_path)
        require_fields(headers, (*spec.key_fields, *spec.fields))
        if spec.source_path == SNAPSHOT_SOURCE_PATH:
            validate_snapshot(headers, rows)
        else:
            reconcile_repeated_records(root, rows)
    except (OSError, UnicodeError, csv.Error, ValueError, ArithmeticError) as exc:
        result["grounding_status"] = "record_contract_error"
        result["record_contract_errors"] = [str(exc)]
        return result

    selected = [
        row
        for row in rows
        if row["store_id"] in STORE_IDS
        and (
            "period_month" not in spec.key_fields
            or row["period_month"] == PERIOD_MONTH
        )
    ]

    by_key: dict[
        tuple[str, ...],
        dict[str, str],
    ] = {}
    duplicates: set[tuple[str, ...]] = set()

    for row in selected:
        key = tuple(
            row[field]
            for field in spec.key_fields
        )

        if key in by_key:
            duplicates.add(key)

        by_key[key] = row

    expected_keys = [
        (
            (store_id, PERIOD_MONTH)
            if "period_month" in spec.key_fields
            else (store_id,)
        )
        for store_id in STORE_IDS
    ]

    missing_keys = [
        key
        for key in expected_keys
        if key not in by_key
    ]

    if duplicates or missing_keys:
        errors = []

        if duplicates:
            errors.append(
                "Duplicate row keys: "
                + str(sorted(duplicates))
            )

        if missing_keys:
            errors.append(
                "Missing row keys: "
                + str(missing_keys)
            )

        result["grounding_status"] = (
            "record_contract_error"
        )
        result["record_contract_errors"] = errors
        return result

    evidence_values = [
        {
            "row_key": dict(
                zip(spec.key_fields, key)
            ),
            "values": {
                field: by_key[key][field]
                for field in spec.fields
            },
        }
        for key in expected_keys
    ]

    result.update(
        {
            "grounding_status": "record_matched",
            "record_scope": {
                "key_fields": list(
                    spec.key_fields
                ),
                "row_count": len(
                    evidence_values
                ),
                "row_keys": [
                    item["row_key"]
                    for item in evidence_values
                ],
            },
            "evidence_values": evidence_values,
        }
    )

    return result
