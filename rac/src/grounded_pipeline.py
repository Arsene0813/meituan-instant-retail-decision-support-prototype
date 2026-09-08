from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from rac.src.local_evidence_resolver import resolve_state_evidence
from rac.src.evidence_review import build_review_plan, recompute_review
from rac.src.mock_pipeline import (
    build_factor_weighting_explanation,
    join_sentence_fragments,
    slugify,
)
from rac.src.state_validation import validate_cognition_state


def markdown_escape(value: object) -> str:
    text = str(value)
    text = text.replace("|", "\\|")
    text = text.replace("\n", "<br>")
    return text


def compact_snippet(
    text: str,
    *,
    matched_terms: list[str] | None = None,
    max_chars: int = 320,
) -> str:
    """Compact a snippet without hiding its matched evidence term."""
    compacted = re.sub(
        r"\s+",
        " ",
        text,
    ).strip()

    if len(compacted) <= max_chars:
        return compacted

    normalized_terms = [
        term.strip().lower()
        for term in (matched_terms or [])
        if term.strip()
    ]

    lowered = compacted.lower()

    matched_positions = [
        lowered.find(term)
        for term in normalized_terms
        if lowered.find(term) >= 0
    ]

    if not matched_positions:
        return (
            compacted[
                : max_chars - 3
            ].rstrip()
            + "..."
        )

    focus_position = min(
        matched_positions
    )

    content_chars = max(
        1,
        max_chars - 6,
    )

    context_before = min(
        96,
        content_chars // 3,
    )

    start = max(
        0,
        focus_position - context_before,
    )

    end = min(
        len(compacted),
        start + content_chars,
    )

    if end - start < content_chars:
        start = max(
            0,
            end - content_chars,
        )

    prefix = "..." if start > 0 else ""
    suffix = (
        "..."
        if end < len(compacted)
        else ""
    )

    return (
        prefix
        + compacted[start:end].strip()
        + suffix
    )


def build_grounded_evidence_rows(
    resolver_result: dict[str, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    for packet in resolver_result["resolved_packets"]:
        common = {
            "factor_id": packet["factor_id"],
            "evidence_id": packet["evidence_id"],
            "source_path": packet["source_path"],
            "grounding_role": packet.get(
                "grounding_role",
                "unknown",
            ),
            "grounding_status": packet[
                "grounding_status"
            ],
        }

        if (
            packet["grounding_status"]
            == "record_matched"
        ):
            rows.append(
                {
                    **common,
                    "line_range": "n/a",
                    "matched_terms": [],
                    "snippet": "",
                    "record_scope": packet[
                        "record_scope"
                    ],
                    "evidence_fields": packet[
                        "evidence_fields"
                    ],
                    "evidence_values": packet[
                        "evidence_values"
                    ],
                }
            )
            continue

        snippets = packet.get("snippets", [])

        if not snippets:
            rows.append(
                {
                    **common,
                    "line_range": "n/a",
                    "matched_terms": [],
                    "snippet": "",
                }
            )
            continue

        first = snippets[0]
        line_start = first.get("line_start")
        line_end = first.get("line_end")
        line_range = (
            f"{line_start}-{line_end}"
            if line_start and line_end
            else "n/a"
        )

        rows.append(
            {
                **common,
                "line_range": line_range,
                "matched_terms": first.get(
                    "matched_terms",
                    [],
                ),
                "snippet": compact_snippet(
                    first.get("text", ""),
                    matched_terms=first.get(
                        "matched_terms",
                        [],
                    ),
                ),
            }
        )

    return rows


def format_record_scope(
    record_scope: dict[str, Any],
) -> str:
    row_keys = record_scope.get("row_keys", [])

    if not row_keys:
        return ""

    stores = sorted(
        {
            str(row["store_id"])
            for row in row_keys
            if row.get("store_id")
        }
    )
    months = sorted(
        {
            str(row["period_month"])
            for row in row_keys
            if row.get("period_month")
        }
    )

    parts = []

    if stores:
        parts.append(
            "store_id=" + ", ".join(stores)
        )

    if months:
        parts.append(
            "period_month=" + ", ".join(months)
        )

    parts.append(
        "rows="
        + str(
            record_scope.get(
                "row_count",
                len(row_keys),
            )
        )
    )

    return "; ".join(parts)


def format_evidence_values(
    evidence_values: list[dict[str, Any]],
) -> str:
    rendered = []

    for item in evidence_values:
        key_text = ", ".join(
            f"{field}={value}"
            for field, value
            in item["row_key"].items()
        )
        value_text = ", ".join(
            f"{field}={value}"
            for field, value
            in item["values"].items()
        )
        rendered.append(
            f"{key_text}: {value_text}"
        )

    return "<br>".join(rendered)


def calculate_routing_coverage_score(
    state: dict[str, Any],
) -> dict[str, Any]:
    grounded = state.get("grounded_evidence", {})
    summary = (
        grounded.get("summary", {})
        if isinstance(grounded, dict)
        else {}
    )

    total_packets = int(
        summary.get("total_packets", 0)
    )
    record_matched_packets = int(
        summary.get("record_matched_count", 0)
    )
    keyword_matched_packets = int(
        summary.get("keyword_matched_count", 0)
    )
    boundary_matched_packets = int(
        summary.get("boundary_matched_count", 0)
    )
    fallback_packets = int(
        summary.get("fallback_count", 0)
    )
    missing_source_files = int(
        summary.get("source_missing_count", 0)
    )

    if total_packets == 0:
        record_or_keyword_route_rate = 0.0
        resolved_or_boundary_route_rate = 0.0
    else:
        record_or_keyword_route_rate = (
            record_matched_packets
            + keyword_matched_packets
        ) / total_packets
        resolved_or_boundary_route_rate = (
            record_matched_packets
            + keyword_matched_packets
            + boundary_matched_packets
        ) / total_packets

    no_missing_source_file_score = (
        1.0 if missing_source_files == 0 else 0.0
    )
    no_fallback_score = (
        1.0 if fallback_packets == 0 else 0.0
    )

    score = (
        0.45 * record_or_keyword_route_rate
        + 0.25 * resolved_or_boundary_route_rate
        + 0.15 * no_missing_source_file_score
        + 0.15 * no_fallback_score
    )

    return {
        "score": max(0.0, min(1.0, score)),
        "total_packets": total_packets,
        "record_matched_packets": (
            record_matched_packets
        ),
        "keyword_matched_packets": (
            keyword_matched_packets
        ),
        "boundary_matched_packets": (
            boundary_matched_packets
        ),
        "fallback_packets": fallback_packets,
        "missing_source_files": missing_source_files,
        "record_or_keyword_route_rate": record_or_keyword_route_rate,
        "resolved_or_boundary_route_rate": (
            resolved_or_boundary_route_rate
        ),
        "no_missing_source_file_score": (
            no_missing_source_file_score
        ),
        "no_fallback_score": no_fallback_score,
    }


def write_grounded_final_report(state: dict[str, Any]) -> str:
    grounded = state["grounded_evidence"]
    summary = grounded["summary"]
    rows = build_grounded_evidence_rows(grounded)

    factor_by_id = {factor["factor_id"]: factor for factor in state["factors"]}

    lines: list[str] = []

    lines.append("# Grounded RAC Report")
    lines.append("")
    lines.append("## 1. Direct Answer")
    lines.append("")
    lines.append(state["belief_update"]["claim"])
    lines.append("")
    lines.append(
        "Deterministic local-file review; routing scores summarize "
        "route resolution under the current rules."
    )
    lines.append("")

    lines.append("## 2. Question Type")
    lines.append("")
    lines.append(f"- Question type: {state['question_type']}")
    lines.append(f"- Domain: {state['domain']}")
    lines.append("")

    lines.append("## 3. Factor Weights")
    lines.append("")
    weighting_explanation = build_factor_weighting_explanation(
        state["question_type"],
        state["factor_weights"],
    )
    lines.append("### 3a. How Factor Weights Are Generated")
    lines.append("")
    lines.append(
        "Factor weights are fixed review-priority buckets assigned "
        "by explicit rules in `rac/src/mock_pipeline.py`. They "
        "order review attention within the current evidence scope."
    )
    lines.append("")
    lines.append("| Bucket | Weight | Rule | Factors in This Report |")
    lines.append("|---|---:|---|---|")
    for bucket in ["high", "medium", "default"]:
        members = weighting_explanation["bucket_members"].get(bucket, [])
        lines.append(
            "| "
            + markdown_escape(bucket)
            + " | "
            + f"{weighting_explanation['bucket_values'][bucket]:.2f}"
            + " | "
            + markdown_escape(weighting_explanation["bucket_reasons"][bucket])
            + " | "
            + markdown_escape(", ".join(members) if members else "none")
            + " |"
        )
    lines.append("")
    lines.append("Weighting boundary:")
    lines.append("")
    lines.append(
        "- Use these values only to order review attention "
        "within the current evidence scope."
    )
    lines.append("")
    lines.append("### 3b. Factor Weights Used in This Report")
    lines.append("")
    lines.append("| Decision Factor ID | Weight | Bucket | Evidence Status | Why It Matters |")
    lines.append("|---|---:|---|---|---|")

    for item in state["factor_weights"]:
        factor = factor_by_id[item["factor_id"]]
        lines.append(
            "| "
            + markdown_escape(factor["factor_id"])
            + " | "
            + f"{item['weight']:.2f}"
            + " | "
            + markdown_escape(item.get("weight_bucket", "unknown"))
            + " | "
            + markdown_escape(item["evidence_status"])
            + " | "
            + markdown_escape(item["weight_reason"])
            + " |"
        )

    lines.append("")
    lines.append(
        "`partially_supported` means at least one selected observation "
        "or registered document/boundary route is available for the factor. "
        "Read the evidence checks to distinguish values from context."
    )
    lines.append("")
    lines.append("## 4. Local Evidence Grounding")
    lines.append("")
    lines.append(
        f"- Total evidence packets: "
        f"{summary['total_packets']}"
    )
    lines.append(
        f"- Record matched packets: "
        f"{summary.get('record_matched_count', 0)}"
    )
    lines.append(
        f"- Keyword matched packets: "
        f"{summary['keyword_matched_count']}"
    )
    lines.append(
        f"- Boundary matched packets: "
        f"{summary.get('boundary_matched_count', 0)}"
    )
    lines.append(
        f"- Fallback packets: "
        f"{summary['fallback_count']}"
    )
    lines.append(
        f"- Missing source files: "
        f"{summary['source_missing_count']}"
    )
    lines.append("")
    lines.append(
        "For CSV evidence, `Source Locator` "
        "shows the selected record scope and "
        "`Selected Values` shows values read "
        "from those records. For Markdown "
        "evidence, the locator remains a local "
        "line-range pointer."
    )
    lines.append(
        "`Decision Factor ID` is an internal RAC "
        "review identifier. The field column shows "
        "canonical project fields where available "
        "and labels unresolved requirements explicitly."
    )
    lines.append("")
    lines.append(
        "| Decision Factor ID | Source | Evidence Type "
        "| Status | Source Locator "
        "| Canonical Evidence Fields / Requirement "
        "| Selected Values |"
    )
    lines.append(
        "|---|---|---|---|---|---|---|"
    )

    report_evidence_fields = {
        "same_reporting_period": "period_start, period_end, period_month",
        "store_type": "store_type",
        "order_volume": "transaction_orders",
        "transaction_amount": "transaction_amount",
        "activity_intensity": "activity_orders, activity_order_share_pct, activity_cost, activity_cost_ratio_pct",
        "region_context": "region_type",
        "competition": "competition-context requirement in comparability contract",
        "sku_structure": "top3_sku_transaction_amount, top3_sku_transaction_amount_share_pct, SKU source tables",
        "repeated_reporting_windows": "repeated period_month records in store_period_panel_metrics",

        # Store A diagnostic factors.
        "search_exposure": (
            "search_exposure_users, search_entry_users, "
            "search_average_rank"
        ),
        "entry_conversion": (
            "entry_conversion_rate_pct, entry_users, "
            "exposure_users"
        ),
        "order_conversion": (
            "order_conversion_rate_pct, order_users, "
            "entry_users"
        ),
        "promotion_intensity": (
            "activity_orders, activity_cost, "
            "activity_original_transaction_amount"
        ),
        "transaction_orders": "transaction_orders",

        # Promotion-review factors.
        "activity_orders": "activity_orders",
        "activity_cost": "activity_cost",
        "merchant_subsidy": "merchant_subsidy_amount",
        "platform_subsidy": "platform_subsidy_amount",
        "payment_conversion": (
            "payment_conversion_rate_pct, payment_users, "
            "order_users"
        ),
        "sku_margin_structure": (
            "required SKU margin context; "
            "unavailable in current evidence"
        ),
        "competitor_context": (
            "required competitor context; "
            "unavailable in current evidence"
        ),

        # Technical-design requirements.
        "typed_memory": "memory schema requirement",
        "evidence_packets": (
            "source_path, claim_supported, limitations"
        ),
        "hypotheses": "hypothesis records",
        "belief_records": "belief update schema",
        "confidence": "confidence field",
        "limitations": "limitations field",
        "retrieval_trace": "source metadata",
        "active_state_filtering": (
            "active flag, freshness policy"
        ),
    }

    for row in rows:
        factor_id = row["factor_id"]
        evidence_type = row[
            "grounding_role"
        ]
        evidence_fields = (
            ", ".join(
                row.get("evidence_fields", [])
            )
            or report_evidence_fields.get(
                factor_id,
                ", ".join(row["matched_terms"]),
            )
        )

        record_scope = format_record_scope(
            row.get("record_scope", {})
        )
        source_locator = (
            "records: " + record_scope
            if record_scope
            else "lines " + row["line_range"]
        )
        selected_values = (
            format_evidence_values(
                row.get("evidence_values", [])
            )
            or "n/a"
        )

        lines.append(
            "| "
            + markdown_escape(factor_id)
            + " | "
            + markdown_escape(row["source_path"])
            + " | "
            + markdown_escape(evidence_type)
            + " | "
            + markdown_escape(row["grounding_status"])
            + " | "
            + markdown_escape(source_locator)
            + " | "
            + markdown_escape(evidence_fields)
            + " | "
            + markdown_escape(selected_values)
            + " |"
        )

    lines.append("")
    lines.append("")

    lines.append("## 5. Competing Hypotheses")
    lines.append("")
    lines.append(
        "Hypothesis statements and statuses are recomputed from the selected "
        "evidence. Confidence is unknown where no calibrated estimate is available."
    )
    lines.append("")
    lines.append(
        "| Hypothesis | Confidence "
        "| Status | Weakness |"
    )
    lines.append("|---|---:|---|---|")

    for hypothesis in state["hypotheses"]:
        lines.append(
            "| "
            + markdown_escape(hypothesis["claim"])
            + " | "
            + ("unknown" if hypothesis["confidence"] is None else f"{hypothesis['confidence']:.2f}")
            + " | "
            + markdown_escape(hypothesis["status"])
            + " | "
            + markdown_escape(
                join_sentence_fragments(
                    hypothesis["weaknesses"]
                )
            )
            + " |"
        )

    lines.append("")
    lines.append("## 6. Critic Findings")
    lines.append("")

    for finding in state["critic_findings"]:
        lines.append(
            f"- [{finding['severity']}] {finding['issue']} "
            f"Recommendation: {finding['recommendation']}"
        )

    lines.append("")
    lines.append("## 7. Claim and Definition Check")
    lines.append("")
    lines.append(f"- Status: {state['fact_check']['status']}")
    lines.append("")
    lines.append("### Evidence Checks")
    lines.append("")
    lines.append("| Check | Evidence | Status | Result |")
    lines.append("|---|---|---|---|")
    for check in state["evidence_review"]["checks"]:
        lines.append("| " + " | ".join(markdown_escape(check[key])
                     for key in ("check_id", "evidence_id", "status", "claim")) + " |")
    lines.append("")

    if state["fact_check"]["unsupported_claims"]:
        lines.append(
            "- Unsupported claims detected by current rules:"
        )
        for claim in state["fact_check"]["unsupported_claims"]:
            lines.append(f"  - {claim}")
    else:
        lines.append(
            "- Unsupported claims detected by current rules: none"
        )

    if state["fact_check"]["definition_conflicts"]:
        lines.append(
            "- Definition conflicts detected by current rules:"
        )
        for conflict in state["fact_check"]["definition_conflicts"]:
            lines.append(f"  - {conflict}")
    else:
        lines.append(
            "- Definition conflicts detected by current rules: none"
        )

    lines.append("")
    lines.append("## 8. Final Judgment")
    lines.append("")
    lines.append(state["belief_update"]["claim"])
    lines.append("")
    lines.append(
        "The judgment is bounded by the cited local evidence "
        "and the unresolved requirements recorded above."
    )
    lines.append("")

    coverage_score = calculate_routing_coverage_score(state)
    lines.append("## 9. Evidence-Routing Coverage")
    lines.append("")
    lines.append("Packet composition:")
    lines.append("")
    lines.append(
        f"- Total packets: "
        f"{coverage_score['total_packets']}"
    )
    lines.append(
        f"- Record matched packets: "
        f"{coverage_score['record_matched_packets']}"
    )
    lines.append(
        f"- Keyword matched packets: "
        f"{coverage_score['keyword_matched_packets']}"
    )
    lines.append(
        f"- Boundary matched packets: "
        f"{coverage_score['boundary_matched_packets']}"
    )
    lines.append(
        f"- Fallback packets: "
        f"{coverage_score['fallback_packets']}"
    )
    lines.append(
        f"- Missing source files: "
        f"{coverage_score['missing_source_files']}"
    )
    lines.append("")
    lines.append(
        f"- Routing coverage score: "
        f"{coverage_score['score']:.2f}"
    )
    lines.append(
        "- Read this value as route resolution under the current "
        "rules, not as evidence strength or decision quality."
    )
    lines.append("")
    lines.append("How this score is calculated:")
    lines.append("")
    lines.append("```text")
    lines.append("routing_coverage_score =")
    lines.append("  0.45 * record_or_keyword_route_rate")
    lines.append("+ 0.25 * resolved_or_boundary_route_rate")
    lines.append("+ 0.15 * no_missing_source_file_score")
    lines.append("+ 0.15 * no_fallback_score")
    lines.append("```")
    lines.append("")
    lines.append("Weight rationale:")
    lines.append("")
    lines.append("| Component | Weight | Why |")
    lines.append("|---|---:|---|")
    lines.append(
        "| `record_or_keyword_route_rate` | 0.45 | "
        "Highest priority because record- or keyword-matched local routes should "
        "matter more than boundary-only evidence. |"
    )
    lines.append(
        "| `resolved_or_boundary_route_rate` | 0.25 | "
        "Boundary evidence is valuable because it explicitly "
        "records missing requirements instead of hiding them. |"
    )
    lines.append(
        "| `no_missing_source_file_score` | 0.15 | "
        "Source files must exist, but this is a basic "
        "traceability check rather than evidence strength. |"
    )
    lines.append(
        "| `no_fallback_score` | 0.15 | "
        "Fallback packets indicate unresolved routing and reduce "
        "the current routing score. |"
    )
    lines.append("")
    lines.append("Score contract:")
    lines.append("")
    lines.append(
        "- Component weights are fixed prototype heuristics."
    )
    lines.append(
        "- The score summarizes route resolution "
        "under the current rules."
    )
    lines.append(
        "- Alternative weights are a formula sensitivity check; "
        "the report judgment is produced separately."
    )
    lines.append("")
    lines.append("Score inputs (contract fields):")
    lines.append("")
    lines.append(
        f"- total_packets = "
        f"{coverage_score['total_packets']}"
    )
    lines.append(
        f"- record_matched_packets = "
        f"{coverage_score['record_matched_packets']}"
    )
    lines.append(
        f"- keyword_matched_packets = "
        f"{coverage_score['keyword_matched_packets']}"
    )
    lines.append(
        f"- boundary_matched_packets = "
        f"{coverage_score['boundary_matched_packets']}"
    )
    lines.append(
        f"- fallback_packets = "
        f"{coverage_score['fallback_packets']}"
    )
    lines.append(
        f"- missing_source_files = "
        f"{coverage_score['missing_source_files']}"
    )
    lines.append("")
    lines.append("Derived rates and checks:")
    lines.append("")
    lines.append(
        "- record_or_keyword_route_rate = "
        "(record_matched_packets + "
        "keyword_matched_packets) / total_packets = "
        f"{coverage_score['record_or_keyword_route_rate']:.2f}"
    )
    lines.append(
        "- resolved_or_boundary_route_rate = "
        "(record_matched_packets + "
        "keyword_matched_packets + "
        "boundary_matched_packets) / total_packets = "
        f"{coverage_score['resolved_or_boundary_route_rate']:.2f}"
    )
    lines.append(
        f"- no_missing_source_file_score = "
        f"{coverage_score['no_missing_source_file_score']:.2f}"
    )
    lines.append(
        f"- no_fallback_score = "
        f"{coverage_score['no_fallback_score']:.2f}"
    )
    lines.append("")
    lines.append("Reading the score:")
    lines.append("")
    lines.append(
        "- A higher value means more requested evidence routes "
        "were resolved or explicitly bounded."
    )
    lines.append(
        "- Boundary evidence contributes when it documents "
        "a missing requirement."
    )
    lines.append(
        "- Read the score as coverage rather than evidence "
        "strength, causal validity, decision quality, "
        "or business impact."
    )
    lines.append("")
    lines.append("## 10. What Cannot Be Concluded")
    lines.append("")

    for limitation in state["belief_update"]["limitations"]:
        lines.append(f"- {limitation}")

    lines.append("")
    lines.append("## 11. Review-State Update")
    lines.append("")
    lines.append(f"- review_state_id: {state['belief_update']['belief_id']}")
    lines.append(f"- status: {state['belief_update']['status']}")
    lines.append("- validity_conditions:")

    for condition in state["belief_update"]["validity_conditions"]:
        lines.append(f"  - {condition}")

    lines.append("")
    return "\n".join(lines)


def run_grounded_pipeline(question: str, *, root: Path) -> dict[str, Any]:
    state = build_review_plan(question)
    grounded = resolve_state_evidence(state, root=root)
    state["grounded_evidence"] = grounded
    recompute_review(state)
    state["grounded_evidence_rows"] = build_grounded_evidence_rows(grounded)
    state["final_report"] = write_grounded_final_report(state)

    validate_cognition_state(state, root=root)
    return state


def save_grounded_outputs(
    state: dict[str, Any],
    output_dir: Path,
    name: str | None = None
) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)

    slug = name or slugify(state["question"])
    json_path = output_dir / f"grounded_{slug}.json"
    md_path = output_dir / f"grounded_{slug}.md"

    json_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    md_path.write_text(
        state["final_report"],
        encoding="utf-8"
    )

    return {
        "json": str(json_path),
        "markdown": str(md_path)
    }
