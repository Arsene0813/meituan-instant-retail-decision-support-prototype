from __future__ import annotations

import csv
from decimal import Decimal, InvalidOperation
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from rac.src.grounded_pipeline import run_grounded_pipeline, save_grounded_outputs
from rac.src.demo2_csv_grounding import (
    FACTOR_RECORD_SPECS as DEMO2_FACTOR_RECORD_SPECS,
    PERIOD_MONTH as DEMO2_PERIOD_MONTH,
    STORE_IDS as DEMO2_STORE_IDS,
)
from rac.src.local_evidence_resolver import SOURCE_FACTOR_KEYWORDS
from rac.src.store_a_csv_grounding import (
    FACTOR_FIELDS as STORE_A_FACTOR_FIELDS,
    PERIOD_MONTHS as STORE_A_PERIOD_MONTHS,
    SOURCE_PATH as STORE_A_SOURCE_PATH,
)


REQUIRED_REPORT_SECTIONS = [
    "# Grounded RAC Report",
    "## 1. Direct Answer",
    "## 2. Question Type",
    "## 3. Factor Weights",
    "### 3a. How Factor Weights Are Generated",
    "### 3b. Factor Weights Used in This Report",
    "## 4. Local Evidence Grounding",
    "### Evidence Checks",
    "## 5. Competing Hypotheses",
    "## 6. Critic Findings",
    "## 7. Claim and Definition Check",
    "## 8. Final Judgment",
    "## 9. Evidence-Routing Coverage",
    "## 10. What Cannot Be Concluded",
    "## 11. Review-State Update",
]

FORBIDDEN_POSITIVE_CLAIMS = [
    "Search exposure caused the March-to-April increases in transaction amount and transaction orders",
    "Search exposure alone explains the March-to-April increases in transaction amount and transaction orders",
    "The system proves causality",
    "The system has live Meituan backend access",
    "Stores B-F are fully comparable",
    "Demo 2 implements a pairwise comparability gate",
    "Region type is a strong market classifier",
    "The system proves which store is better",
    "Activity cost ratio is ROI",
    "The system updates neural network weights",
    "The system has a true autonomous world model",
    "The system solves causal inference automatically"
]

ALLOWED_GROUNDING_STATUSES = {
    "record_matched",
    "keyword_matched",
    "boundary_matched",
    "source_found_no_keyword_match"
}

MIN_SNIPPET_CHARS = 8

CROSS_STORE_REQUIRED_BOUNDARY_SOURCES = {
    "competition": "retail_ops/COMPARABILITY_GATE_V0.md",
}


def fail(message: str) -> None:
    raise SystemExit(f"[RAC report-contract quality gate failed] {message}")


def load_eval_cases() -> list[dict[str, Any]]:
    path = ROOT / "rac" / "eval" / "rac_eval_cases.json"
    return json.loads(path.read_text(encoding="utf-8"))


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def validate_report_sections(report: str) -> list[str]:
    issues: list[str] = []

    for section in REQUIRED_REPORT_SECTIONS:
        if section not in report:
            issues.append(f"missing report section: {section}")

    return issues


def validate_forbidden_claims(report: str) -> list[str]:
    issues: list[str] = []
    normalized_report = normalize(report)

    for claim in FORBIDDEN_POSITIVE_CLAIMS:
        if normalize(claim) in normalized_report:
            issues.append(f"forbidden positive claim found: {claim}")

    return issues


def validate_evidence_review(state: dict[str, Any]) -> list[str]:
    """Check operands and directions independently of report wording."""
    issues: list[str] = []
    review = state.get("evidence_review", {})
    if review.get("method") != "registered_local_evidence_rules_v1":
        issues.append("missing registered evidence-review method")
    if review.get("confidence_method") != "not_estimated":
        issues.append("grounded confidence must remain explicitly unestimated")
    records = [*state.get("hypotheses", []), state.get("belief_update", {})]
    if any(record.get("confidence") is not None for record in records):
        issues.append("grounded review contains an estimated numerical confidence")

    kind = state.get("question_type")
    expected_scope = (
        {"store_ids": ["A"], "period_start": "2026-03-01", "period_end": "2026-04-30"}
        if kind == "causal_diagnostic" else
        {"store_ids": list(DEMO2_STORE_IDS), "period_start": "2026-03-01", "period_end": "2026-03-31"}
        if kind == "comparability_judgment" else
        {"store_ids": [], "period_start": None, "period_end": None}
    )
    if review.get("scope") != expected_scope:
        issues.append("evidence review does not preserve its registered store/window scope")

    packets = state.get("grounded_evidence", {}).get("resolved_packets", [])
    by_factor = {packet.get("factor_id"): packet for packet in packets}
    if len(by_factor) != len(packets):
        issues.append("duplicate resolved evidence factors")
    expected_ids = {
        str(packet["factor_id"]) + "/" + field
        for packet in packets
        for field in packet.get("evidence_fields", ["document"])
    }
    checks = review.get("checks", [])
    actual_ids = [check.get("check_id") for check in checks]
    if len(actual_ids) != len(set(actual_ids)) or set(actual_ids) != expected_ids:
        issues.append("evidence checks do not exactly cover registered factor fields/routes")

    for check in checks:
        label = str(check.get("check_id", ""))
        packet = by_factor.get(check.get("factor_id"))
        if packet is None:
            issues.append(label + ": check has no resolved factor")
            continue
        if (check.get("evidence_id") != packet.get("evidence_id")
                or check.get("source_path") != packet.get("source_path")):
            issues.append(label + ": check does not refer to its selected source packet")
        if "evidence_fields" not in packet:
            matched = packet.get("grounding_status") in {"keyword_matched", "boundary_matched"}
            if (check.get("field") is not None or check.get("operation") != "document_route"
                    or check.get("operands") != []
                    or check.get("status") != ("supported" if matched else "missing")
                    or check.get("result") != ("matched" if matched else "unresolved")):
                issues.append(label + ": document route is misrepresented as observed data")
            continue

        field = check.get("field")
        if field not in packet["evidence_fields"]:
            issues.append(label + ": field was not selected by the registered route")
            continue
        expected_operands = [
            {"row_key": item["row_key"],
             "value": item["values"].get(field) if str(item["values"].get(field) or "").strip() else None}
            for item in packet.get("evidence_values", [])
        ]
        if check.get("operands") != expected_operands:
            issues.append(label + ": operands differ from the independently selected source values")
        operation = "compare" if kind == "causal_diagnostic" else "record_values"
        if check.get("operation") != operation:
            issues.append(label + ": incorrect operation for the registered review")
        matched = packet.get("grounding_status") == "record_matched"
        complete = matched and bool(expected_operands) and all(
            operand["value"] is not None for operand in expected_operands
        )
        expected_status = (
            "supported" if complete else
            "missing" if matched or packet.get("grounding_status") == "source_missing" else "invalid"
        )
        expected_result = "recorded" if complete else "unresolved"
        if complete and operation == "compare":
            try:
                if len(expected_operands) != 2:
                    raise ValueError("comparison requires two records")
                before, after = (Decimal(operand["value"]) for operand in expected_operands)
                if not before.is_finite() or not after.is_finite():
                    raise ValueError("non-finite operand")
                expected_result = "increased" if after > before else "decreased" if after < before else "unchanged"
            except (InvalidOperation, TypeError, ValueError):
                issues.append(label + ": comparison accepted invalid numerical operands")
                continue
        if check.get("status") != expected_status or check.get("result") != expected_result:
            issues.append(label + ": result/status contradicts the selected operands")
    return issues


def validate_store_a_record_shape(
    row: dict[str, Any],
    *,
    index: int,
) -> list[str]:
    issues = []
    factor_id = str(row.get("factor_id", ""))

    if factor_id not in STORE_A_FACTOR_FIELDS:
        return [
            f"row {index} unexpected record "
            f"factor: {factor_id}"
        ]

    if row.get("source_path") != STORE_A_SOURCE_PATH:
        issues.append(
            f"row {index} unexpected record source"
        )

    if (
        row.get("grounding_role")
        != "quantitative_evidence"
    ):
        issues.append(
            f"row {index} unexpected record role"
        )

    if (
        row.get("line_range") != "n/a"
        or str(row.get("snippet", "")).strip()
    ):
        issues.append(
            f"row {index} record evidence "
            "claims text-line grounding"
        )

    expected_fields = list(
        STORE_A_FACTOR_FIELDS[factor_id]
    )
    expected_keys = [
        {
            "store_id": "A",
            "period_month": month,
        }
        for month in STORE_A_PERIOD_MONTHS
    ]

    if row.get("evidence_fields") != expected_fields:
        issues.append(
            f"row {index} field contract mismatch"
        )

    values = row.get("evidence_values", [])
    scope = row.get("record_scope", {})

    if [
        item.get("row_key", {})
        for item in values
        if isinstance(item, dict)
    ] != expected_keys:
        issues.append(
            f"row {index} selected keys mismatch"
        )

    if (
        scope.get("key_fields")
        != ["store_id", "period_month"]
        or scope.get("row_count") != 2
        or scope.get("row_keys") != expected_keys
    ):
        issues.append(
            f"row {index} record scope mismatch"
        )

    return issues



def validate_demo2_record_shape(
    row: dict[str, Any],
    *,
    index: int,
) -> list[str]:
    issues = []
    factor_id = str(row.get("factor_id", ""))
    spec = DEMO2_FACTOR_RECORD_SPECS.get(
        factor_id
    )

    if spec is None:
        return [
            f"row {index} unexpected Demo 2 "
            f"record factor: {factor_id}"
        ]

    if (
        row.get("source_path")
        != spec.source_path
    ):
        issues.append(
            f"row {index} unexpected "
            "Demo 2 record source"
        )

    if (
        row.get("grounding_role")
        != spec.grounding_role
    ):
        issues.append(
            f"row {index} unexpected "
            "Demo 2 record role"
        )

    if (
        row.get("line_range") != "n/a"
        or str(row.get("snippet", "")).strip()
    ):
        issues.append(
            f"row {index} Demo 2 record "
            "claims text-line grounding"
        )

    expected_fields = list(spec.fields)
    expected_keys = [
        (
            {
                "store_id": store_id,
                "period_month": (
                    DEMO2_PERIOD_MONTH
                ),
            }
            if "period_month"
            in spec.key_fields
            else {
                "store_id": store_id
            }
        )
        for store_id in DEMO2_STORE_IDS
    ]

    if (
        row.get("evidence_fields")
        != expected_fields
    ):
        issues.append(
            f"row {index} Demo 2 "
            "field contract mismatch"
        )

    values = row.get(
        "evidence_values",
        [],
    )
    actual_keys = [
        item.get("row_key", {})
        for item in values
        if isinstance(item, dict)
    ]
    scope = row.get("record_scope", {})

    if actual_keys != expected_keys:
        issues.append(
            f"row {index} Demo 2 "
            "selected keys mismatch"
        )

    if (
        scope.get("key_fields")
        != list(spec.key_fields)
        or scope.get("row_count")
        != len(DEMO2_STORE_IDS)
        or scope.get("row_keys")
        != expected_keys
    ):
        issues.append(
            f"row {index} Demo 2 "
            "record scope mismatch"
        )

    source_path = ROOT / spec.source_path

    with source_path.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as handle:
        source_rows = list(
            csv.DictReader(handle)
        )

    source_by_key = {
        tuple(
            source_row[field]
            for field in spec.key_fields
        ): source_row
        for source_row in source_rows
        if (
            source_row.get("store_id")
            in DEMO2_STORE_IDS
        )
        and (
            "period_month"
            not in spec.key_fields
            or source_row.get("period_month")
            == DEMO2_PERIOD_MONTH
        )
    }

    for item in values:
        row_key = item.get("row_key", {})
        key = tuple(
            str(row_key.get(field, ""))
            for field in spec.key_fields
        )
        source_row = source_by_key.get(key)
        selected_values = item.get(
            "values",
            {},
        )

        if source_row is None:
            issues.append(
                f"row {index} Demo 2 "
                f"source key missing: {key!r}"
            )
            continue

        if (
            set(selected_values)
            != set(spec.fields)
        ):
            issues.append(
                f"row {index} Demo 2 "
                "selected fields mismatch"
            )
            continue

        for field in spec.fields:
            if (
                str(selected_values[field])
                != str(source_row[field])
            ):
                issues.append(
                    f"row {index} Demo 2 "
                    "source-value mismatch for "
                    f"{key!r}, {field}"
                )

    return issues

def validate_store_a_grounding(
    case_id: str,
    state: dict[str, Any],
) -> list[str]:
    issues = []
    rows = state.get("grounded_evidence_rows", [])
    record_rows = [
        row
        for row in rows
        if row.get("grounding_status")
        == "record_matched"
        and row.get("source_path")
        == STORE_A_SOURCE_PATH
    ]

    if case_id != "rac_store_a_attribution_001":
        if record_rows:
            issues.append(
                f"{case_id} unexpectedly used "
                "record grounding"
            )
        return issues

    by_factor = {
        row.get("factor_id"): row
        for row in record_rows
    }

    if (
        len(record_rows) != len(STORE_A_FACTOR_FIELDS)
        or set(by_factor) != set(STORE_A_FACTOR_FIELDS)
    ):
        issues.append(
            "Store A record factor mismatch"
        )
        return issues

    summary = state.get(
        "grounded_evidence",
        {},
    ).get("summary", {})

    if summary.get("record_matched_count") != len(STORE_A_FACTOR_FIELDS):
        issues.append(
            "Store A summary record count mismatch"
        )

    source_path = ROOT / STORE_A_SOURCE_PATH

    with source_path.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as handle:
        reader = csv.DictReader(handle)
        headers = reader.fieldnames or []
        source_rows = list(reader)

    required_fields = {
        "store_id",
        "period_month",
        *{
            field
            for fields in STORE_A_FACTOR_FIELDS.values()
            for field in fields
        },
    }

    missing_fields = sorted(
        required_fields - set(headers)
    )

    if missing_fields:
        issues.append(
            "Store A source missing fields: "
            + ", ".join(missing_fields)
        )
        return issues

    selected_source_rows = [
        row
        for row in source_rows
        if (
            row["store_id"] == "A"
            and row["period_month"]
            in STORE_A_PERIOD_MONTHS
        )
    ]
    source_by_key = {}
    duplicate_keys = set()

    for source_row in selected_source_rows:
        key = (
            source_row["store_id"],
            source_row["period_month"],
        )

        if key in source_by_key:
            duplicate_keys.add(key)
        else:
            source_by_key[key] = source_row

    expected_keys = {
        ("A", month)
        for month in STORE_A_PERIOD_MONTHS
    }

    if duplicate_keys or set(source_by_key) != expected_keys:
        issues.append(
            "Store A source row-key contract failed"
        )
        return issues

    for factor_id, row in by_factor.items():
        expected_fields = set(
            STORE_A_FACTOR_FIELDS[factor_id]
        )

        for item in row["evidence_values"]:
            key = (
                str(item["row_key"]["store_id"]),
                str(item["row_key"]["period_month"]),
            )
            values = item.get("values", {})

            if set(values) != expected_fields:
                issues.append(
                    f"{factor_id} selected fields mismatch"
                )
                continue

            for field in expected_fields:
                if (
                    str(values[field])
                    != str(source_by_key[key][field])
                ):
                    issues.append(
                        f"{factor_id} source-value "
                        f"mismatch for {key!r}, {field}"
                    )

    for fragment in [
        f"Record matched packets: {len(STORE_A_FACTOR_FIELDS)}",
        (
            "records: store_id=A; "
            "period_month=2026-03, 2026-04; rows=2"
        ),
        f"record_matched_packets = {len(STORE_A_FACTOR_FIELDS)}",
    ]:
        if fragment not in state.get(
            "final_report",
            "",
        ):
            issues.append(
                "Store A report missing: "
                + fragment
            )

    return issues


def validate_grounded_rows(rows: list[dict[str, Any]]) -> tuple[list[str], dict[str, int]]:
    issues: list[str] = []
    status_counts: dict[str, int] = {}

    if not rows:
        issues.append("no grounded evidence rows")
        return issues, status_counts

    for index, row in enumerate(rows):
        status = row.get("grounding_status", "")
        status_counts[status] = status_counts.get(status, 0) + 1

        factor_id = str(row.get("factor_id", ""))
        source_path = str(row.get("source_path", ""))
        line_range = str(row.get("line_range", ""))
        snippet = str(row.get("snippet", ""))
        grounding_role = str(row.get("grounding_role", ""))

        if not factor_id:
            issues.append(f"row {index} missing factor_id")

        if not source_path:
            issues.append(f"row {index} missing source_path")
        else:
            absolute_path = ROOT / source_path
            if not absolute_path.exists():
                issues.append(f"row {index} source file does not exist: {source_path}")

        if not grounding_role:
            issues.append(f"row {index} missing grounding_role")

        if status not in ALLOWED_GROUNDING_STATUSES:
            issues.append(f"row {index} invalid grounding_status: {status}")

        if status == "record_matched":
            if source_path == STORE_A_SOURCE_PATH:
                issues.extend(
                    validate_store_a_record_shape(
                        row,
                        index=index,
                    )
                )
            else:
                issues.extend(
                    validate_demo2_record_shape(
                        row,
                        index=index,
                    )
                )
            continue

        if not re.match(r"^\d+-\d+$", line_range):
            issues.append(f"row {index} invalid line_range: {line_range}")

        if len(snippet.strip()) < MIN_SNIPPET_CHARS:
            issues.append(f"row {index} snippet too short for factor {factor_id}")

        required_anchors = SOURCE_FACTOR_KEYWORDS.get(
            (
                factor_id,
                source_path,
            )
        )

        if required_anchors:
            normalized_snippet = normalize(
                snippet
            )

            anchor_matched = any(
                normalize(anchor)
                in normalized_snippet
                for anchor in required_anchors
            )

            if (
                status
                == "source_found_no_keyword_match"
            ):
                issues.append(
                    f"row {index} used fallback "
                    "context for anchored factor "
                    f"{factor_id}"
                )

            if not anchor_matched:
                issues.append(
                    f"row {index} semantic anchor "
                    f"mismatch for {factor_id}: "
                    f"expected one of "
                    f"{required_anchors!r}"
                )

    return issues, status_counts


def validate_factor_evidence_status(
    state: dict[str, Any],
) -> list[str]:
    """Keep factor status aligned with its evidence route."""
    issues: list[str] = []

    checks = state.get("evidence_review", {}).get("checks", [])
    checks_by_factor: dict[str, list[dict[str, Any]]] = {}
    for check in checks:
        checks_by_factor.setdefault(str(check.get("factor_id", "")), []).append(check)

    for factor_weight in state.get(
        "factor_weights",
        [],
    ):
        factor_id = str(
            factor_weight.get("factor_id", "")
        )
        actual_status = str(
            factor_weight.get(
                "evidence_status",
                "",
            )
        )
        factor_checks = checks_by_factor.get(factor_id, [])
        if not factor_checks:
            issues.append("factor weight has no registered evidence checks: " + factor_id)
            continue
        expected_status = (
            "partially_supported"
            if any(check.get("status") == "supported" for check in factor_checks)
            else "missing"
        )

        if actual_status != expected_status:
            issues.append(
                "factor evidence-status mismatch: "
                f"{factor_id}; "
                f"expected={expected_status}; "
                f"actual={actual_status}"
            )

    return issues


def validate_cross_store_grounding(state: dict[str, Any]) -> list[str]:
    issues: list[str] = []

    if state.get("question_type") != "comparability_judgment":
        return issues

    rows = {
        row.get("factor_id"): row
        for row in state.get("grounded_evidence_rows", [])
    }

    summary = state.get("grounded_evidence", {}).get("summary", {})
    fallback_count = summary.get("fallback_count", 0)

    if fallback_count != 0:
        issues.append(
            "cross-store fallback_count "
            f"must be zero: {fallback_count}"
        )

    required_routes = {
        factor_id: (
            spec.source_path,
            spec.grounding_role,
        )
        for factor_id, spec
        in DEMO2_FACTOR_RECORD_SPECS.items()
    }

    for factor_id, (
        expected_source,
        expected_role,
    ) in required_routes.items():
        row = rows.get(factor_id)

        if not row:
            issues.append(
                "cross-store missing factor row: "
                f"{factor_id}"
            )
            continue

        if (
            row.get("source_path")
            != expected_source
        ):
            issues.append(
                "cross-store factor "
                f"{factor_id} expected source "
                f"{expected_source}, got "
                f"{row.get('source_path')}"
            )

        if (
            row.get("grounding_role")
            != expected_role
        ):
            issues.append(
                "cross-store factor "
                f"{factor_id} expected "
                f"{expected_role} role, got "
                f"{row.get('grounding_role')}"
            )

        if (
            row.get("grounding_status")
            != "record_matched"
        ):
            issues.append(
                "cross-store factor "
                f"{factor_id} expected "
                "record_matched status, got "
                f"{row.get('grounding_status')}"
            )

    if (
        summary.get("record_matched_count")
        != len(DEMO2_FACTOR_RECORD_SPECS)
    ):
        issues.append(
            "cross-store record summary "
            "count mismatch"
        )

    for factor_id, expected_source in CROSS_STORE_REQUIRED_BOUNDARY_SOURCES.items():
        row = rows.get(factor_id)

        if not row:
            issues.append(f"cross-store missing boundary factor row: {factor_id}")
            continue

        if row.get("source_path") != expected_source:
            issues.append(
                f"cross-store boundary factor {factor_id} expected source {expected_source}, "
                f"got {row.get('source_path')}"
            )

        if row.get("grounding_role") != "boundary_evidence":
            issues.append(
                f"cross-store boundary factor {factor_id} expected boundary_evidence role, "
                f"got {row.get('grounding_role')}"
            )

        if row.get("grounding_status") != "boundary_matched":
            issues.append(
                f"cross-store boundary factor {factor_id} expected boundary_matched status, "
                f"got {row.get('grounding_status')}"
            )

    report = state.get("final_report", "")

    required_report_phrases = [
        "same-period diagnostic review",
        "Direct comparability remains unresolved."
    ]

    for phrase in required_report_phrases:
        if phrase not in report:
            issues.append(f"cross-store report missing boundary phrase: {phrase}")

    return issues


def validate_state(case: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    case_id = case["case_id"]
    issues: list[str] = []

    report = state.get("final_report", "")
    grounded = state.get("grounded_evidence", {})
    summary = grounded.get("summary", {})
    rows = state.get("grounded_evidence_rows", [])

    issues.extend(validate_report_sections(report))
    issues.extend(validate_forbidden_claims(report))
    issues.extend(validate_evidence_review(state))

    row_issues, row_status_counts = validate_grounded_rows(rows)
    issues.extend(row_issues)

    issues.extend(
        validate_factor_evidence_status(state)
    )

    issues.extend(validate_cross_store_grounding(state))
    issues.extend(
        validate_store_a_grounding(
            case_id,
            state,
        )
    )

    factor_count = len(state.get("factors", []))
    factor_weight_count = len(state.get("factor_weights", []))
    hypothesis_count = len(state.get("hypotheses", []))
    critic_count = len(state.get("critic_findings", []))
    limitation_count = len(state.get("belief_update", {}).get("limitations", []))

    if factor_count == 0:
        issues.append("no factors found")

    if factor_weight_count != factor_count:
        issues.append(
            f"factor weight count mismatch: factors={factor_count}, "
            f"weights={factor_weight_count}"
        )

    if hypothesis_count < 2:
        issues.append("fewer than 2 hypotheses")

    if limitation_count == 0:
        issues.append("no belief limitations")

    if summary.get("source_missing_count") != 0:
        issues.append(f"source_missing_count is not zero: {summary.get('source_missing_count')}")

    if summary.get("total_packets") != len(rows):
        issues.append(
            f"packet/row mismatch: total_packets={summary.get('total_packets')}, "
            f"rows={len(rows)}"
        )

    supported_count = (
        summary.get("record_matched_count", 0)
        + summary.get("keyword_matched_count", 0)
        + summary.get("boundary_matched_count", 0)
    )

    if supported_count == 0:
        issues.append(
            "zero record, keyword, or "
            "boundary matched packets"
        )

    if (
        summary.get("record_matched_count", 0)
        != row_status_counts.get(
            "record_matched",
            0,
        )
    ):
        issues.append(
            "record summary/row count mismatch"
        )

    required_report_contract_phrases = [
        'Deterministic local-file review',
        'fixed review-priority buckets assigned by explicit rules',
        '| Hypothesis | Confidence',
        'Unsupported claims detected by current rules',
        'Definition conflicts detected by current rules',
        'The judgment is bounded by the cited local evidence',
        'Packet composition:',
        'Routing coverage score:',
        'routing_coverage_score =',
        'record_or_keyword_route_rate',
        'resolved_or_boundary_route_rate',
        '`partially_supported` means at least one selected observation',
        'Score inputs (contract fields):',
        'Score contract:',
        'The score summarizes route resolution under the current rules.',
        'Alternative weights are a formula sensitivity check; the report judgment is produced separately.',
        'Reading the score:',
        'Read the score as coverage rather than evidence strength, causal validity, decision quality, or business impact.',
    ]

    for phrase in required_report_contract_phrases:
        if phrase not in report:
            issues.append(
                "report missing concise contract phrase: "
                f"{phrase}"
            )

    legacy_defensive_phrases = [
        'It does not call an LLM, vector database, or live backend service.',
        'not by a learned model',
        'not by direct calculation from observed metric tables',
        'They are not learned probabilities, calibrated likelihoods',
        'Unsupported claims: none',
        'Definition conflicts: none',
        'but it does not prove causality or replace a full retrieval system.',
        'Formula limitation:',
        'Future sensitivity check:',
        'They are not learned parameters, optimized thresholds, calibrated probabilities, or business-performance predictors.',
        'This is a deterministic evidence-routing coverage score for the current local report.',
        'It is not a learned probability, Bayesian posterior, causal confidence score, or business-success probability.',
        'It does not measure evidence strength, conclusion correctness, decision quality, or business impact',
    ]

    for phrase in legacy_defensive_phrases:
        if phrase in report:
            issues.append(
                "legacy defensive report phrase found: "
                f"{phrase}"
            )

    for row in state.get("factor_weights", []):
        if "weight_bucket" not in row:
            issues.append(f"factor weight row missing weight_bucket: {row.get('factor_id')}")
        if "weighting_method" not in row:
            issues.append(f"factor weight row missing weighting_method: {row.get('factor_id')}")
        if "weight_source" not in row:
            issues.append(f"factor weight row missing weight_source: {row.get('factor_id')}")

    for column in [
        "Source Locator",
        "Evidence Fields",
        "Selected Values",
    ]:
        if column not in report:
            issues.append(
                "report does not expose "
                f"{column} column"
            )

    if "Matched Terms" in report:
        issues.append("report exposes raw matched terms instead of curated evidence fields")

    if "Missing source files: 0" not in report:
        issues.append("report does not explicitly show Missing source files: 0")

    passed = len(issues) == 0

    return {
        "case_id": case_id,
        "question_type": state.get("question_type"),
        "passed": passed,
        "issues": issues,
        "metrics": {
            "factor_count": factor_count,
            "factor_weight_count": factor_weight_count,
            "hypothesis_count": hypothesis_count,
            "critic_count": critic_count,
            "limitation_count": limitation_count,
            "grounded_row_count": len(rows),
            "total_packets": summary.get("total_packets", 0),
            "record_matched_count": summary.get(
                "record_matched_count",
                0,
            ),
            "keyword_matched_count": summary.get("keyword_matched_count", 0),
            "boundary_matched_count": summary.get("boundary_matched_count", 0),
            "fallback_count": summary.get("fallback_count", 0),
            "source_missing_count": summary.get("source_missing_count", 0),
            "row_status_counts": row_status_counts
        }
    }


def write_markdown_summary(results: list[dict[str, Any]], output_path: Path) -> None:
    total_cases = len(results)
    passed_cases = sum(1 for result in results if result["passed"])
    failed_cases = total_cases - passed_cases

    total_packets = sum(result["metrics"]["total_packets"] for result in results)
    total_record = sum(
        result["metrics"]["record_matched_count"]
        for result in results
    )
    total_keyword = sum(result["metrics"]["keyword_matched_count"] for result in results)
    total_boundary = sum(result["metrics"]["boundary_matched_count"] for result in results)
    total_fallback = sum(result["metrics"]["fallback_count"] for result in results)
    total_missing = sum(result["metrics"]["source_missing_count"] for result in results)

    lines: list[str] = []

    lines.append("# RAC Report-Contract Summary")
    lines.append("")
    lines.append(
        "This file is generated by "
        "rac/scripts/validate_grounded_quality_gate.py."
    )
    lines.append("")
    lines.append("## Contract Scope")
    lines.append("")
    lines.append(
        "A contract pass means the case satisfied the checks "
        "implemented by the current deterministic rule set: "
        "report structure, source traceability, structured CSV "
        "value checks, semantic anchors, and selected claim "
        "boundaries. It does not establish "
        "causal validity, decision quality, or business impact."
    )
    lines.append("")
    lines.append("## Overall Result")
    lines.append("")
    lines.append(f"- Total cases: {total_cases}")
    lines.append(
        f"- Report-contract passed cases: {passed_cases}"
    )
    lines.append(
        f"- Report-contract failed cases: {failed_cases}"
    )
    lines.append(f"- Total grounded packets: {total_packets}")
    lines.append(
        f"- Record matched packets: {total_record}"
    )
    lines.append(f"- Keyword matched packets: {total_keyword}")
    lines.append(f"- Boundary matched packets: {total_boundary}")
    lines.append(f"- Fallback packets: {total_fallback}")
    lines.append(f"- Missing source files: {total_missing}")
    lines.append("")
    lines.append("## Case Results")
    lines.append("")
    lines.append(
        "| Case | Contract Pass | Factors | Hypotheses "
        "| Critic Findings | Grounded Rows | Record Matched "
        "| Keyword Matched | Boundary Matched | Fallback "
        "| Missing Sources |"
    )
    lines.append(
        "|---|---|---:|---:|---:|---:|"
        "---:|---:|---:|---:|---:|"
    )

    for result in results:
        metrics = result["metrics"]
        lines.append(
            f"| {result['case_id']} "
            f"| {result['passed']} "
            f"| {metrics['factor_count']} "
            f"| {metrics['hypothesis_count']} "
            f"| {metrics['critic_count']} "
            f"| {metrics['grounded_row_count']} "
            f"| {metrics['record_matched_count']} "
            f"| {metrics['keyword_matched_count']} "
            f"| {metrics['boundary_matched_count']} "
            f"| {metrics['fallback_count']} "
            f"| {metrics['source_missing_count']} |"
        )

    lines.append("")
    lines.append("## Store A Record Grounding Requirement")
    lines.append("")
    lines.append(
        "For rac_store_a_attribution_001, "
        f"the gate requires {len(STORE_A_FACTOR_FIELDS)} factor-specific evidence packets covering the two Store A records "
        "for 2026-03 and 2026-04, using canonical "
        "fields whose selected values equal the "
        "source CSV."
    )
    lines.append("")
    lines.append("## Cross-Store Grounding Requirement")
    lines.append("")
    lines.append(
        "For rac_cross_store_comparability_001, "
        "the gate checks these record routes "
        "and their selected CSV values:"
    )
    lines.append("")
    lines.append("- fallback_count = 0")

    for factor_id, spec in (
        DEMO2_FACTOR_RECORD_SPECS.items()
    ):
        fields = ", ".join(spec.fields)
        lines.append(
            f"- `{factor_id}` -> "
            f"`{spec.source_path}` as "
            f"`{spec.grounding_role}`; "
            f"fields: `{fields}`"
        )

    lines.append(
        "- `competition` -> "
        "`retail_ops/COMPARABILITY_GATE_V0.md` "
        "as `boundary_evidence`"
    )
    lines.append("")
    lines.append("## Report-Contract Issues")
    lines.append("")

    any_issue = False

    for result in results:
        if not result["issues"]:
            continue

        any_issue = True
        lines.append(f"### {result['case_id']}")
        lines.append("")

        for issue in result["issues"]:
            lines.append(f"- {issue}")

        lines.append("")

    if not any_issue:
        lines.append(
            "No report-contract issues were detected "
            "by the current rule set."
        )
        lines.append("")

    output_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    cases = load_eval_cases()
    output_dir = ROOT / "rac" / "outputs"
    output_dir.mkdir(parents=True, exist_ok=True)

    if not cases:
        fail("No eval cases found")

    results: list[dict[str, Any]] = []

    for case in cases:
        state = run_grounded_pipeline(case["question"], root=ROOT)
        save_grounded_outputs(state, output_dir, case["case_id"])

        result = validate_state(case, state)
        results.append(result)

    summary = {
        "validation_scope": (
            "Current deterministic report-contract checks: "
            "report structure, source traceability, structured "
            "CSV value checks, semantic anchors, and selected "
            "claim boundaries."
        ),
        "pass_interpretation": (
            "A passed case satisfies the current rule set; "
            "it does not establish causal validity, decision "
            "quality, or business impact."
        ),
        "total_cases": len(results),
        "passed_cases": sum(
            1
            for result in results
            if result["passed"]
        ),
        "failed_cases": sum(
            1
            for result in results
            if not result["passed"]
        ),
        "results": results,
    }

    json_path = output_dir / "grounded_quality_summary.json"
    md_path = output_dir / "grounded_quality_summary.md"

    json_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    write_markdown_summary(results, md_path)

    failed = [result for result in results if not result["passed"]]

    if failed:
        for result in failed:
            print(f"[FAIL] {result['case_id']}")

            for issue in result["issues"]:
                print(f"  - {issue}")

        fail(f"{len(failed)} grounded quality case(s) failed")

    total_packets = sum(result["metrics"]["total_packets"] for result in results)
    total_record = sum(
        result["metrics"]["record_matched_count"]
        for result in results
    )
    total_keyword = sum(result["metrics"]["keyword_matched_count"] for result in results)
    total_boundary = sum(result["metrics"]["boundary_matched_count"] for result in results)
    total_fallback = sum(result["metrics"]["fallback_count"] for result in results)
    total_missing = sum(result["metrics"]["source_missing_count"] for result in results)

    print("[OK] RAC report-contract quality gate passed")
    print(f"[OK] Cases checked: {len(results)}")
    print(f"[OK] Total grounded packets: {total_packets}")
    print(
        f"[OK] Record matched packets: {total_record}"
    )
    print(f"[OK] Keyword matched packets: {total_keyword}")
    print(f"[OK] Boundary matched packets: {total_boundary}")
    print(f"[OK] Fallback packets: {total_fallback}")
    print(f"[OK] Missing source files: {total_missing}")
    print(f"[OK] Summary JSON: {json_path}")
    print(f"[OK] Summary MD: {md_path}")


if __name__ == "__main__":
    main()
