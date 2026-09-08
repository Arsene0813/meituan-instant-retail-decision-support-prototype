from __future__ import annotations

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from rac.src.demo2_csv_grounding import (
    FACTOR_RECORD_SPECS as DEMO2_FACTOR_RECORD_SPECS,
    PERIOD_MONTH as DEMO2_PERIOD_MONTH,
    STORE_IDS as DEMO2_STORE_IDS,
)
from rac.src.local_evidence_resolver import (
    SOURCE_FACTOR_KEYWORDS,
    STRATEGIC_SOURCE_OVERRIDES,
    resolve_evidence_packet,
    resolve_state_evidence,
)
from rac.src.evidence_review import build_review_plan
from rac.src.store_a_csv_grounding import (
    FACTOR_FIELDS as STORE_A_FACTOR_FIELDS,
    PERIOD_MONTHS as STORE_A_PERIOD_MONTHS,
    SOURCE_PATH as STORE_A_SOURCE_PATH,
)


def fail(message: str) -> None:
    raise SystemExit(f"[RAC local evidence resolver validation failed] {message}")


def load_eval_cases() -> list[dict]:
    path = ROOT / "rac" / "eval" / "rac_eval_cases.json"
    return json.loads(path.read_text(encoding="utf-8"))


PROMOTION_BOUNDARY_FACTORS = {
    "sku_margin_structure",
    "competitor_context",
}


def validate_promotion_boundary_routing(
    case_id: str,
    packets: list[dict],
) -> None:
    if case_id != "rac_promotion_strategy_001":
        return

    packets_by_factor = {
        packet["factor_id"]: packet
        for packet in packets
    }

    missing = sorted(
        PROMOTION_BOUNDARY_FACTORS
        - set(packets_by_factor)
    )

    if missing:
        fail(
            "Promotion case is missing factors: "
            f"{missing}"
        )

    actual_boundary = {
        factor_id
        for factor_id, packet in packets_by_factor.items()
        if packet["grounding_status"] == "boundary_matched"
    }

    if actual_boundary != PROMOTION_BOUNDARY_FACTORS:
        fail(
            "Promotion boundary routing mismatch. "
            f"Expected {sorted(PROMOTION_BOUNDARY_FACTORS)}, "
            f"found {sorted(actual_boundary)}"
        )

    for factor_id in sorted(PROMOTION_BOUNDARY_FACTORS):
        role = packets_by_factor[factor_id].get(
            "grounding_role"
        )

        if role != "boundary_evidence":
            fail(
                f"{factor_id} must use boundary_evidence, "
                f"found {role}"
            )



def validate_source_factor_contract() -> None:
    """Check the canonical anchors used by high-risk RAC factors."""
    dictionary = (
        "retail_ops/data/DATA_DICTIONARY.md"
    )
    comparability_gate = (
        "retail_ops/COMPARABILITY_GATE_V0.md"
    )

    expected_anchors = {
        (
            "region_context",
            dictionary,
        ): "### `region_type`",
        (
            "activity_orders",
            dictionary,
        ): "### `activity_orders`",
        (
            "activity_cost",
            dictionary,
        ): "### `activity_cost`",
        (
            "merchant_subsidy",
            dictionary,
        ): "### `merchant_subsidy_amount`",
        (
            "platform_subsidy",
            dictionary,
        ): "### `platform_subsidy_amount`",
        (
            "order_conversion",
            dictionary,
        ): "### `order_conversion_rate_pct`",
        (
            "payment_conversion",
            dictionary,
        ): "### `payment_conversion_rate_pct`",
        (
            "competition",
            comparability_gate,
        ): (
            "Competition context "
            "| Not currently structured"
        ),
        (
            "sku_margin_structure",
            comparability_gate,
        ): "margin-aware structure",
    }

    for key, expected_anchor in (
        expected_anchors.items()
    ):
        actual_anchors = (
            SOURCE_FACTOR_KEYWORDS.get(
                key,
                [],
            )
        )

        if expected_anchor not in actual_anchors:
            fail(
                "Source-factor anchor mismatch "
                f"for {key}: expected "
                f"{expected_anchor!r}, "
                f"found {actual_anchors!r}"
            )

    for factor_id in sorted(
        PROMOTION_BOUNDARY_FACTORS
    ):
        overrides = (
            STRATEGIC_SOURCE_OVERRIDES.get(
                factor_id,
                [],
            )
        )

        if len(overrides) != 1:
            fail(
                f"{factor_id} must have exactly "
                "one strategic source override, "
                f"found {overrides!r}"
            )

        override = overrides[0]

        if (
            override.get("source_path")
            != comparability_gate
        ):
            fail(
                f"{factor_id} must use "
                f"{comparability_gate}, found "
                f"{override.get('source_path')}"
            )

        if (
            override.get("grounding_role")
            != "boundary_evidence"
        ):
            fail(
                f"{factor_id} must use "
                "boundary_evidence, found "
                f"{override.get('grounding_role')}"
            )


def validate_broad_terms_do_not_ground_boundary() -> None:
    """Broad words alone must not produce a boundary match."""
    dictionary = (
        "retail_ops/data/DATA_DICTIONARY.md"
    )
    comparability_gate = (
        "retail_ops/COMPARABILITY_GATE_V0.md"
    )

    with TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)

        boundary_path = (
            root
            / comparability_gate
        )

        boundary_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        # These words previously risked creating broad
        # textual matches. None is a sufficient margin
        # or competitor-context boundary by itself.
        boundary_path.write_text(
            (
                "sku price market fact "
                "source state\n"
            ),
            encoding="utf-8",
        )

        for factor_id in sorted(
            PROMOTION_BOUNDARY_FACTORS
        ):
            packet = {
                "factor_id": factor_id,
                "evidence_id": (
                    f"evidence_{factor_id}"
                ),
                "source_type": "markdown",
                "source_path": dictionary,
                "claim_supported": (
                    "Negative semantic-grounding "
                    f"test for {factor_id}."
                ),
                "limitations": [],
            }

            resolved = resolve_evidence_packet(
                packet,
                root=root,
                question_type=(
                    "strategic_recommendation"
                ),
            )

            if (
                resolved.get("source_path")
                != comparability_gate
            ):
                fail(
                    f"{factor_id} did not route to "
                    f"{comparability_gate}: "
                    f"{resolved!r}"
                )

            if (
                resolved.get("grounding_role")
                != "boundary_evidence"
            ):
                fail(
                    f"{factor_id} did not retain "
                    "boundary_evidence role: "
                    f"{resolved!r}"
                )

            if (
                resolved.get("grounding_status")
                == "boundary_matched"
            ):
                fail(
                    "Broad terms incorrectly produced "
                    "a boundary match for "
                    f"{factor_id}: {resolved!r}"
                )

            if (
                resolved.get("grounding_status")
                != "source_found_no_keyword_match"
            ):
                fail(
                    f"{factor_id} expected "
                    "source_found_no_keyword_match, "
                    f"found "
                    f"{resolved.get('grounding_status')}"
                )

def validate_record_packets(
    case_id: str,
    packets: list[dict],
) -> None:
    record_packets = [
        packet
        for packet in packets
        if packet["grounding_status"]
        == "record_matched"
    ]

    if case_id == "rac_cross_store_comparability_001":
        if not record_packets:
            fail(
                "Demo 2 record grounding is missing"
            )

        if len(record_packets) != len(
            DEMO2_FACTOR_RECORD_SPECS
        ):
            fail(
                "Demo 2 record packet count mismatch"
            )

        by_factor = {
            packet["factor_id"]: packet
            for packet in record_packets
        }

        if set(by_factor) != set(
            DEMO2_FACTOR_RECORD_SPECS
        ):
            fail(
                "Demo 2 record factor mismatch"
            )

        for factor_id, packet in (
            by_factor.items()
        ):
            spec = DEMO2_FACTOR_RECORD_SPECS[
                factor_id
            ]

            if (
                packet["source_path"]
                != spec.source_path
            ):
                fail(
                    f"{factor_id} used "
                    "unexpected source"
                )

            if (
                packet["grounding_role"]
                != spec.grounding_role
            ):
                fail(
                    f"{factor_id} used "
                    "unexpected role"
                )

            if (
                packet["evidence_fields"]
                != list(spec.fields)
            ):
                fail(
                    f"{factor_id} canonical "
                    "fields mismatch"
                )

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

            actual_keys = [
                item["row_key"]
                for item in packet[
                    "evidence_values"
                ]
            ]

            if actual_keys != expected_keys:
                fail(
                    f"{factor_id} record "
                    "keys mismatch"
                )

            if packet["snippets"]:
                fail(
                    f"{factor_id} record "
                    "evidence must not use "
                    "snippets"
                )

        return

    if case_id != "rac_store_a_attribution_001":
        if record_packets:
            fail(
                f"{case_id} unexpectedly used "
                "record grounding"
            )
        return

    by_factor = {
        packet["factor_id"]: packet
        for packet in record_packets
    }

    if set(by_factor) != set(STORE_A_FACTOR_FIELDS):
        fail(
            "Store A record factor mismatch: "
            f"{sorted(by_factor)}"
        )

    for factor_id, packet in by_factor.items():
        if packet["source_path"] != STORE_A_SOURCE_PATH:
            fail(
                f"{factor_id} used unexpected source"
            )

        if packet["grounding_role"] != "quantitative_evidence":
            fail(
                f"{factor_id} used unexpected role"
            )

        if packet["evidence_fields"] != list(
            STORE_A_FACTOR_FIELDS[factor_id]
        ):
            fail(
                f"{factor_id} canonical fields mismatch"
            )

        months = tuple(
            item["row_key"]["period_month"]
            for item in packet["evidence_values"]
        )

        if months != STORE_A_PERIOD_MONTHS:
            fail(
                f"{factor_id} period selection mismatch"
            )

        if packet["snippets"]:
            fail(
                f"{factor_id} record evidence "
                "must not use snippets"
            )


def main() -> None:
    cases = load_eval_cases()

    if not cases:
        fail("No eval cases found")

    validate_source_factor_contract()
    validate_broad_terms_do_not_ground_boundary()

    total_packets = 0
    total_record_matches = 0
    total_keyword_matches = 0
    total_fallbacks = 0

    for case in cases:
        state = build_review_plan(case["question"])
        resolved = resolve_state_evidence(state, root=ROOT)

        summary = resolved["summary"]
        packets = resolved["resolved_packets"]

        validate_promotion_boundary_routing(
            case["case_id"],
            packets,
        )
        validate_record_packets(
            case["case_id"],
            packets,
        )

        if summary["total_packets"] == 0:
            fail(f"{case['case_id']} produced no evidence packets")

        if summary["source_missing_count"] > 0:
            missing = [
                packet["source_path"]
                for packet in packets
                if packet["grounding_status"] == "source_missing"
            ]
            fail(f"{case['case_id']} has missing source files: {missing}")

        for packet in packets:
            if (
                packet["grounding_status"]
                == "record_matched"
            ):
                continue

            if not packet["snippets"]:
                fail(
                    f"{case['case_id']} packet "
                    f"{packet['evidence_id']} "
                    "has no snippets"
                )

            for snippet in packet["snippets"]:
                if not snippet["text"].strip():
                    fail(
                        f"{case['case_id']} packet "
                        f"{packet['evidence_id']} "
                        "has empty snippet text"
                    )

        supported_count = (
            summary.get("record_matched_count", 0)
            + summary["keyword_matched_count"]
            + summary.get("boundary_matched_count", 0)
        )

        if supported_count == 0:
            fail(
                f"{case['case_id']} has zero "
                "supported packets"
            )

        total_packets += summary["total_packets"]
        total_record_matches += summary.get(
            "record_matched_count",
            0,
        )
        total_keyword_matches += summary["keyword_matched_count"]
        total_fallbacks += summary["fallback_count"]

    print(
        "[OK] Source-factor anchor contract passed"
    )
    print(
        "[OK] Broad-term boundary regression test passed"
    )
    print("[OK] RAC local evidence resolver validation passed")
    print(f"[OK] Eval cases checked: {len(cases)}")
    print(f"[OK] Total evidence packets: {total_packets}")
    print(
        f"[OK] Record matched packets: "
        f"{total_record_matches}"
    )
    print(f"[OK] Keyword matched packets: {total_keyword_matches}")
    print(f"[OK] Fallback packets: {total_fallbacks}")


if __name__ == "__main__":
    main()
