from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rac.src.demo2_csv_grounding import (
    resolve_demo2_record,
    supports_demo2_record,
)
from rac.src.store_a_csv_grounding import (
    SOURCE_PATH as STORE_A_SOURCE_PATH,
    resolve_store_a_record,
    supports_store_a_record,
)


FACTOR_KEYWORDS: dict[str, list[str]] = {
    "search_exposure": [
        "search",
        "search exposure",
        "search entry",
        "search average rank",
        "搜索",
        "曝光",
        "搜索曝光",
        "搜索入店",
        "平均排名"
    ],
    "entry_conversion": [
        "entry",
        "entry conversion",
        "entry users",
        "入店",
        "入店转化",
        "入店人数",
        "入店次数"
    ],
    "order_conversion": [
        "order conversion",
        "order users",
        "order times",
        "下单",
        "下单转化",
        "下单人数",
        "下单次数"
    ],
    "promotion_intensity": [
        "promotion",
        "activity",
        "activity orders",
        "activity cost",
        "merchant subsidy",
        "platform subsidy",
        "活动",
        "活动订单",
        "活动成本",
        "商家补贴",
        "平台补贴"
    ],
    "transaction_orders": [
        "transaction_orders",
        "transaction orders",
    ],
    "same_reporting_period": [
        "same reporting period",
        "same period",
        "period",
        "reporting window",
        "March 2026",
        "2026-03",
        "2026-03-01",
        "2026-03-31",
        "同一周期",
        "统计周期"
    ],
    "store_type": [
        "store_type",
        "store type",
        "self-operated",
        "partner",
        "门店类型",
        "自营",
        "合作"
    ],
    "order_volume": [
        "order_volume",
        "order volume",
        "transaction_orders",
        "transaction orders",
        "orders",
        "成交订单量",
    ],
    "transaction_amount": [
        "transaction_amount",
        "transaction amount",
        "transaction",
        "成交金额",
        "成交",
        "金额"
    ],
    "activity_intensity": [
        "activity",
        "activity_intensity",
        "activity_order_share_pct",
        "activity_cost_ratio_pct",
        "activity cost",
        "activity orders",
        "活动",
        "活动成本",
        "活动订单",
        "活动营业总额"
    ],
    "region_context": [
        "region",
        "region_type",
        "region type",
        "market",
        "business district",
        "商圈",
        "区域",
        "地区"
    ],
    "competition": [
        "competition",
        "competitor",
        "competitor context",
        "price war",
        "current limitation",
        "future evidence",
        "not currently structured",
        "竞争",
        "竞对",
        "价格战"
    ],
    "sku_structure": [
        "sku",
        "top_sku",
        "top3_sku",
        "top sku",
        "sku structure",
        "top3_sku_transaction_amount",
        "商品",
        "产品",
        "销量",
        "销售额"
    ],
    "repeated_reporting_windows": [
        "repeated",
        "repeated reporting windows",
        "multi-period",
        "multiple periods",
        "future work",
        "before implementation",
        "not implemented",
        "多周期",
        "重复周期",
        "未来"
    ],
    "activity_orders": [
        "activity_orders",
        "activity orders",
        "activity order",
        "活动订单",
        "活动订单数"
    ],
    "activity_cost": [
        "activity_cost",
        "activity cost",
        "活动成本"
    ],
    "merchant_subsidy": [
        "merchant_subsidy",
        "merchant subsidy",
        "商家补贴"
    ],
    "platform_subsidy": [
        "platform_subsidy",
        "platform subsidy",
        "平台补贴"
    ],
    "payment_conversion": [
        "payment_conversion",
        "payment conversion",
        "payment users",
        "payment amount",
        "支付转化",
        "支付人数",
        "支付金额"
    ],
    "sku_margin_structure": [
        "margin",
        "sku margin",
        "sku",
        "毛利",
        "利润",
        "商品"
    ],
    "competitor_context": [
        "competitor",
        "competition",
        "price",
        "竞对",
        "竞争",
        "价格"
    ],
    "typed_memory": [
        "typed memory",
        "memory",
        "structured memory",
        "fact",
        "记忆",
        "结构化"
    ],
    "evidence_packets": [
        "evidence",
        "evidence packet",
        "source",
        "claim supported",
        "证据",
        "来源"
    ],
    "hypotheses": [
        "hypothesis",
        "hypotheses",
        "competing",
        "假设",
        "竞争解释"
    ],
    "belief_records": [
        "belief",
        "belief update",
        "confidence",
        "validity",
        "信念",
        "置信度"
    ],
    "confidence": [
        "confidence",
        "uncertainty",
        "置信度",
        "不确定"
    ],
    "limitations": [
        "limitation",
        "limitations",
        "cannot be concluded",
        "boundary",
        "限制",
        "边界"
    ],
    "retrieval_trace": [
        "retrieval",
        "trace",
        "source",
        "retrieval trace",
        "检索",
        "追踪",
        "来源"
    ],
    "active_state_filtering": [
        "active",
        "state",
        "filter",
        "freshness",
        "deprecated",
        "stale",
        "活跃",
        "过期"
    ]
}



# Canonical definitions and explicit evidence boundaries use narrower
# source-specific anchors. General keywords apply only within registered routes.
SOURCE_FACTOR_KEYWORDS: dict[
    tuple[str, str],
    list[str],
] = {
    (
        "region_context",
        "retail_ops/data/DATA_DICTIONARY.md",
    ): [
        "### `region_type`",
    ],
    (
        "activity_orders",
        "retail_ops/data/DATA_DICTIONARY.md",
    ): [
        "### `activity_orders`",
    ],
    (
        "activity_cost",
        "retail_ops/data/DATA_DICTIONARY.md",
    ): [
        "### `activity_cost`",
    ],
    (
        "merchant_subsidy",
        "retail_ops/data/DATA_DICTIONARY.md",
    ): [
        "### `merchant_subsidy_amount`",
    ],
    (
        "platform_subsidy",
        "retail_ops/data/DATA_DICTIONARY.md",
    ): [
        "### `platform_subsidy_amount`",
    ],
    (
        "order_conversion",
        "retail_ops/data/DATA_DICTIONARY.md",
    ): [
        "### `order_conversion_rate_pct`",
    ],
    (
        "payment_conversion",
        "retail_ops/data/DATA_DICTIONARY.md",
    ): [
        "### `payment_conversion_rate_pct`",
    ],
    (
        "competition",
        "retail_ops/COMPARABILITY_GATE_V0.md",
    ): [
        "Competition context | Not currently structured",
    ],
    (
        "sku_margin_structure",
        "retail_ops/COMPARABILITY_GATE_V0.md",
    ): [
        "margin-aware structure",
    ],
    (
        "competitor_context",
        "retail_ops/COMPARABILITY_GATE_V0.md",
    ): [
        "competitor reaction",
        "competitor density",
        "price pressure",
    ],
}

STRATEGIC_SOURCE_OVERRIDES: dict[str, list[dict[str, str]]] = {
    "sku_margin_structure": [
        {
            "source_path": "retail_ops/COMPARABILITY_GATE_V0.md",
            "grounding_role": "boundary_evidence",
        }
    ],
    "competitor_context": [
        {
            "source_path": "retail_ops/COMPARABILITY_GATE_V0.md",
            "grounding_role": "boundary_evidence",
        }
    ],
}


COMPARABILITY_SOURCE_OVERRIDES: dict[str, list[dict[str, str]]] = {
    "same_reporting_period": [
        {
            "source_path": "retail_ops/outputs/demo2_cross_store_comparability_output.csv",
            "grounding_role": "context_evidence"
        },
        {
            "source_path": "retail_ops/data/demo2_source_notes.md",
            "grounding_role": "context_evidence"
        }
    ],
    "store_type": [
        {
            "source_path": "retail_ops/outputs/demo2_cross_store_comparability_output.csv",
            "grounding_role": "context_evidence"
        },
        {
            "source_path": "retail_ops/data/demo2_source_notes.md",
            "grounding_role": "context_evidence"
        }
    ],
    "order_volume": [
        {
            "source_path": "retail_ops/outputs/demo2_cross_store_comparability_output.csv",
            "grounding_role": "quantitative_evidence"
        }
    ],
    "transaction_amount": [
        {
            "source_path": "retail_ops/outputs/demo2_cross_store_comparability_output.csv",
            "grounding_role": "quantitative_evidence"
        }
    ],
    "activity_intensity": [
        {
            "source_path": "retail_ops/outputs/demo2_cross_store_comparability_output.csv",
            "grounding_role": "quantitative_evidence"
        }
    ],
    "region_context": [
        {
            "source_path": "retail_ops/outputs/demo2_cross_store_comparability_output.csv",
            "grounding_role": "context_evidence"
        },
        {
            "source_path": "retail_ops/data/DATA_DICTIONARY.md",
            "grounding_role": "definition_evidence"
        },
        {
            "source_path": "retail_ops/data/demo2_source_notes.md",
            "grounding_role": "context_evidence"
        }
    ],
    "competition": [
        {
            "source_path": "retail_ops/COMPARABILITY_GATE_V0.md",
            "grounding_role": "boundary_evidence"
        }
    ],
    "sku_structure": [
        {
            "source_path": "retail_ops/outputs/demo2_cross_store_comparability_output.csv",
            "grounding_role": "product_mix_evidence"
        },
        {
            "source_path": "retail_ops/data/demo2_top_skus_by_sales_volume.csv",
            "grounding_role": "source_table_evidence"
        },
        {
            "source_path": "retail_ops/data/demo2_top_skus_by_transaction_amount.csv",
            "grounding_role": "source_table_evidence"
        }
    ],
    "repeated_reporting_windows": [
        {
            "source_path": "retail_ops/outputs/repeated_window_panel_summary_output.csv",
            "grounding_role": "quantitative_evidence"
        },
        {
            "source_path": "retail_ops/outputs/store_period_panel_coverage_output.csv",
            "grounding_role": "coverage_evidence"
        },
        {
            "source_path": "retail_ops/data/store_period_panel_metrics.csv",
            "grounding_role": "source_table_evidence"
        },
        {
            "source_path": "retail_ops/COMPARABILITY_GATE_V0.md",
            "grounding_role": "boundary_evidence"
        }
    ]
}


@dataclass
class EvidenceSnippet:
    line_start: int
    line_end: int
    matched_terms: list[str]
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "line_start": self.line_start,
            "line_end": self.line_end,
            "matched_terms": self.matched_terms,
            "text": self.text
        }


def infer_factor_id(packet: dict[str, Any]) -> str:
    if "factor_id" in packet:
        return str(packet["factor_id"])

    evidence_id = str(packet.get("evidence_id", ""))
    prefix = "evidence_"

    if evidence_id.startswith(prefix):
        return evidence_id[len(prefix):]

    return evidence_id


def read_source_text(root: Path, source_path: str) -> tuple[Path, str | None]:
    path = (root / source_path).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Evidence source is outside the declared project root.")

    if not path.exists() or not path.is_file():
        return path, None

    return path, path.read_text(encoding="utf-8")


def find_keyword_snippets(
    text: str,
    keywords: list[str],
    *,
    context_radius: int = 1,
    max_snippets: int = 3
) -> list[EvidenceSnippet]:
    lines = text.splitlines()
    lowered_keywords = [keyword.lower() for keyword in keywords]
    snippets: list[EvidenceSnippet] = []
    used_line_indexes: set[int] = set()

    for index, line in enumerate(lines):
        lowered_line = line.lower()
        matched_terms = [
            keyword for keyword, lowered_keyword in zip(keywords, lowered_keywords)
            if lowered_keyword in lowered_line
        ]

        if not matched_terms:
            continue

        if index in used_line_indexes:
            continue

        start = max(0, index - context_radius)
        end = min(len(lines), index + context_radius + 1)
        snippet_lines = lines[start:end]
        snippet_text = "\n".join(snippet_lines).strip()

        if not snippet_text:
            continue

        for used in range(start, end):
            used_line_indexes.add(used)

        snippets.append(
            EvidenceSnippet(
                line_start=start + 1,
                line_end=end,
                matched_terms=matched_terms,
                text=snippet_text
            )
        )

        if len(snippets) >= max_snippets:
            break

    return snippets


def fallback_snippet(text: str, *, max_lines: int = 8) -> list[EvidenceSnippet]:
    lines = [line for line in text.splitlines() if line.strip()]

    if not lines:
        return []

    snippet = "\n".join(lines[:max_lines]).strip()

    if not snippet:
        return []

    return [
        EvidenceSnippet(
            line_start=1,
            line_end=min(max_lines, len(lines)),
            matched_terms=[],
            text=snippet
        )
    ]


def candidate_sources_for_packet(
    packet: dict[str, Any],
    *,
    question_type: str | None
) -> list[dict[str, str]]:
    factor_id = infer_factor_id(packet)

    if supports_store_a_record(
        question_type,
        factor_id,
    ):
        return [
            {
                "source_path": STORE_A_SOURCE_PATH,
                "grounding_role": "quantitative_evidence",
            }
        ]

    if (
        question_type == "strategic_recommendation"
        and factor_id in STRATEGIC_SOURCE_OVERRIDES
    ):
        return STRATEGIC_SOURCE_OVERRIDES[factor_id]

    if (
        question_type == "comparability_judgment"
        and factor_id in COMPARABILITY_SOURCE_OVERRIDES
    ):
        return COMPARABILITY_SOURCE_OVERRIDES[factor_id]

    if question_type == "strategic_recommendation" and factor_id in {
        "activity_orders", "activity_cost", "merchant_subsidy",
        "platform_subsidy", "order_conversion", "payment_conversion",
    }:
        return [{"source_path": "retail_ops/data/DATA_DICTIONARY.md",
                 "grounding_role": "definition_evidence"}]
    if question_type == "technical_design" and factor_id in {
        "typed_memory", "evidence_packets", "hypotheses", "belief_records",
        "confidence", "limitations", "retrieval_trace", "active_state_filtering",
    }:
        return [{"source_path": "rac/README.md", "grounding_role": "design_evidence"}]
    raise ValueError(f"Unregistered RAC evidence route: {question_type}/{factor_id}.")


def keywords_for_source(
    factor_id: str,
    source_path: str,
) -> list[str]:
    """Return strict anchors for registered source-factor pairs."""
    strict_keywords = SOURCE_FACTOR_KEYWORDS.get(
        (factor_id, source_path)
    )

    if strict_keywords is not None:
        return strict_keywords

    return FACTOR_KEYWORDS.get(
        factor_id,
        [factor_id.replace("_", " ")],
    )

def resolve_single_source(
    packet: dict[str, Any],
    *,
    factor_id: str,
    source_path: str,
    grounding_role: str,
    root: Path
) -> dict[str, Any]:
    keywords = keywords_for_source(
        factor_id,
        source_path,
    )
    absolute_path, text = read_source_text(root, source_path)

    resolved: dict[str, Any] = {
        "factor_id": factor_id,
        "evidence_id": packet.get("evidence_id"),
        "source_type": packet.get("source_type"),
        "source_path": source_path,
        "source_exists": text is not None,
        "grounding_role": grounding_role,
        "keywords_used": keywords,
        "grounding_status": "source_missing",
        "snippets": [],
        "original_claim_supported": packet.get("claim_supported"),
        "original_limitations": packet.get("limitations", []),
        "resolver_limitations": [
            "Local text matching only.",
            "No semantic embedding retrieval is used in this step.",
            "No causality is inferred from matched snippets."
        ]
    }

    if text is None:
        resolved["absolute_path_checked"] = str(absolute_path)
        resolved["resolver_limitations"].append("Source file does not exist.")
        return resolved

    snippets = find_keyword_snippets(text, keywords)

    if snippets:
        if grounding_role == "boundary_evidence":
            resolved["grounding_status"] = "boundary_matched"
        else:
            resolved["grounding_status"] = "keyword_matched"

        resolved["snippets"] = [snippet.to_dict() for snippet in snippets]
        return resolved

    fallback = fallback_snippet(text)

    if fallback:
        resolved["grounding_status"] = (
            "source_found_no_keyword_match"
        )
        resolved["resolver_limitations"].append(
            "No registered factor anchor matched; "
            "fallback context was returned."
        )

        if grounding_role == "boundary_evidence":
            resolved["resolver_limitations"].append(
                "A boundary source is not treated as matched "
                "without an explicit boundary anchor."
            )

        resolved["snippets"] = [
            snippet.to_dict()
            for snippet in fallback
        ]
        return resolved

    resolved["grounding_status"] = "source_empty"
    resolved["resolver_limitations"].append("Source file exists but no readable text was found.")
    return resolved


def status_score(packet: dict[str, Any]) -> int:
    status = packet["grounding_status"]

    if status == "record_matched":
        return 5

    if status == "keyword_matched":
        return 4

    if status == "boundary_matched":
        return 3

    if status == "source_found_no_keyword_match":
        return 1

    return 0


def resolve_evidence_packet(
    packet: dict[str, Any],
    *,
    root: Path,
    question_type: str | None = None
) -> dict[str, Any]:
    factor_id = infer_factor_id(packet)
    candidates = candidate_sources_for_packet(packet, question_type=question_type)

    resolved_candidates = []

    for candidate in candidates:
        source_path = candidate["source_path"]

        if supports_store_a_record(
            question_type,
            factor_id,
            source_path,
        ):
            resolved = resolve_store_a_record(
                packet,
                factor_id=factor_id,
                root=root,
            )
        elif supports_demo2_record(
            question_type,
            factor_id,
            source_path,
        ):
            resolved = resolve_demo2_record(
                packet,
                factor_id=factor_id,
                root=root,
            )
        else:
            resolved = resolve_single_source(
                packet,
                factor_id=factor_id,
                source_path=source_path,
                grounding_role=candidate[
                    "grounding_role"
                ],
                root=root,
            )

        resolved_candidates.append(resolved)

    resolved_candidates.sort(
        key=lambda item: (
            status_score(item),
            1 if item.get("source_exists") else 0
        ),
        reverse=True
    )

    # A registered record route is authoritative, including an error or a
    # missing file. Context snippets cannot fill missing business records.
    record_sources = [item for item in resolved_candidates if
                      supports_store_a_record(question_type, factor_id, item["source_path"])
                      or supports_demo2_record(question_type, factor_id, item["source_path"])]
    best = record_sources[0] if record_sources else resolved_candidates[0]

    if len(resolved_candidates) > 1:
        best["candidate_sources_checked"] = [
            {
                "source_path": item["source_path"],
                "grounding_role": item["grounding_role"],
                "grounding_status": item["grounding_status"],
                "source_exists": item["source_exists"]
            }
            for item in resolved_candidates
        ]

    return best


def resolve_state_evidence(
    state: dict[str, Any],
    *,
    root: Path
) -> dict[str, Any]:
    packets = state.get("evidence_packets", [])
    question_type = state.get("question_type")

    resolved_packets = [
        resolve_evidence_packet(packet, root=root, question_type=question_type)
        for packet in packets
    ]

    status_counts: dict[str, int] = {}
    role_counts: dict[str, int] = {}

    for packet in resolved_packets:
        status = packet["grounding_status"]
        role = packet.get("grounding_role", "unknown")

        status_counts[status] = status_counts.get(status, 0) + 1
        role_counts[role] = role_counts.get(role, 0) + 1

    summary = {
        "question": state.get("question"),
        "question_type": question_type,
        "total_packets": len(resolved_packets),
        "status_counts": status_counts,
        "role_counts": role_counts,
        "source_missing_count": status_counts.get("source_missing", 0),
        "record_matched_count": status_counts.get("record_matched", 0),
        "keyword_matched_count": status_counts.get("keyword_matched", 0),
        "boundary_matched_count": status_counts.get("boundary_matched", 0),
        "fallback_count": status_counts.get("source_found_no_keyword_match", 0)
    }

    return {
        "summary": summary,
        "resolved_packets": resolved_packets
    }
