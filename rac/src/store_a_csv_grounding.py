from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from rac.src.csv_evidence_validation import canonical_rows, require_fields


SOURCE_PATH = "retail_ops/data/store_a_monthly_metrics.csv"
PERIOD_MONTHS = ("2026-03", "2026-04")

FACTOR_FIELDS: dict[str, tuple[str, ...]] = {
    "search_exposure": (
        "search_exposure_users",
        "search_average_rank",
        "search_entry_users",
    ),
    "entry_conversion": (
        "exposure_users",
        "entry_users",
        "entry_conversion_rate_pct",
    ),
    "order_conversion": (
        "entry_users",
        "order_users",
        "order_conversion_rate_pct",
    ),
    "promotion_intensity": (
        "activity_original_transaction_amount",
        "activity_orders",
        "activity_cost",
        "merchant_subsidy_amount",
        "platform_subsidy_amount",
        "activity_cost_ratio_pct",
    ),
    "transaction_orders": ("transaction_orders",),
    "transaction_amount": ("transaction_amount",),
}


def supports_store_a_record(
    question_type: str | None,
    factor_id: str,
    source_path: str = SOURCE_PATH,
) -> bool:
    return (
        question_type == "causal_diagnostic"
        and factor_id in FACTOR_FIELDS
        and source_path == SOURCE_PATH
    )


def resolve_store_a_record(
    packet: dict[str, Any],
    *,
    factor_id: str,
    root: Path,
) -> dict[str, Any]:
    path = root / SOURCE_PATH
    fields = FACTOR_FIELDS[factor_id]

    result: dict[str, Any] = {
        "factor_id": factor_id,
        "evidence_id": packet.get("evidence_id"),
        "source_type": "csv",
        "source_path": SOURCE_PATH,
        "source_exists": path.is_file(),
        "grounding_role": "quantitative_evidence",
        "grounding_status": "source_missing",
        "snippets": [],
        "record_scope": {},
        "evidence_fields": list(fields),
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
                "Deterministic Store A CSV record "
                "selection only."
            ),
            (
                "Selected values describe the "
                "observed records; they do not "
                "establish a single cause for the "
                "March-to-April changes."
            ),
        ],
    }

    if not path.is_file():
        result["absolute_path_checked"] = str(path)
        return result

    try:
        headers, rows = canonical_rows(root, "store_a_monthly_metrics")
        require_fields(headers, fields)
    except (OSError, UnicodeError, csv.Error, ValueError, ArithmeticError) as exc:
        result["grounding_status"] = "record_contract_error"
        result["record_contract_errors"] = [str(exc)]
        return result

    selected = [
        row
        for row in rows
        if (
            row["store_id"] == "A"
            and row["period_month"] in PERIOD_MONTHS
        )
    ]

    by_month: dict[str, dict[str, str]] = {}
    duplicates: set[str] = set()

    for row in selected:
        month = row["period_month"]

        if month in by_month:
            duplicates.add(month)
        else:
            by_month[month] = row

    missing_months = [
        month
        for month in PERIOD_MONTHS
        if month not in by_month
    ]

    if duplicates or missing_months:
        errors = []

        if duplicates:
            errors.append(
                "Duplicate Store A periods: "
                + ", ".join(sorted(duplicates))
            )

        if missing_months:
            errors.append(
                "Missing Store A periods: "
                + ", ".join(missing_months)
            )

        result["grounding_status"] = "record_contract_error"
        result["record_contract_errors"] = errors
        return result

    evidence_values = [
        {
            "row_key": {
                "store_id": "A",
                "period_month": month,
            },
            "values": {
                field: by_month[month][field]
                for field in fields
            },
        }
        for month in PERIOD_MONTHS
    ]

    result.update(
        {
            "grounding_status": "record_matched",
            "record_scope": {
                "key_fields": [
                    "store_id",
                    "period_month",
                ],
                "row_count": len(evidence_values),
                "row_keys": [
                    item["row_key"]
                    for item in evidence_values
                ],
            },
            "evidence_values": evidence_values,
        }
    )

    return result
