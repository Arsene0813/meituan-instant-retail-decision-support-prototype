from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.main import RetailOpsDemo2KbReq, chat_retail_ops_demo2_kb  # noqa: E402


RESULT_PATH = (
    ROOT
    / "eval"
    / "retail_decision_support_eval_results"
    / "eval_retail_demo2_endpoint_behavior_result.txt"
)


def stringify(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True).lower()


def require_supported(name: str, result: dict[str, Any]) -> None:
    if result.get("supported") is not True:
        raise AssertionError(f"{name}: expected supported=True, got {result}")


def require_refusal(name: str, result: dict[str, Any]) -> None:
    if result.get("supported") is not False:
        raise AssertionError(f"{name}: expected supported=False, got {result}")


def require_demo2_endpoint_metadata(
    name: str,
    result: dict[str, Any],
) -> None:
    expected_metadata = {
        "demo_scope": "demo2_same_period_b_f_diagnostic",
        "retrieval_mode": "not_used",
        "selection_mode": "deterministic_entity_slot_filter",
    }

    for field_name, expected_value in expected_metadata.items():
        if result.get(field_name) != expected_value:
            raise AssertionError(
                f"{name}: expected {field_name}={expected_value!r}, "
                f"got {result.get(field_name)!r}"
            )

    facts = result.get("facts", [])

    if not facts:
        raise AssertionError(
            f"{name}: expected at least one supported fact"
        )

    non_null_scores = [
        fact.get("score")
        for fact in facts
        if fact.get("score") is not None
    ]

    if non_null_scores:
        raise AssertionError(
            f"{name}: deterministic Demo 2 facts must use "
            f"score=None, got {non_null_scores}"
        )


def require_slot(name: str, result: dict[str, Any], expected_slot: str) -> None:
    slots = [fact.get("slot") for fact in result.get("facts", [])]
    if expected_slot not in slots:
        raise AssertionError(
            f"{name}: expected slot {expected_slot!r}, got slots={slots}"
        )


def require_all_slots(name: str, result: dict[str, Any], expected_slot: str) -> None:
    slots = [fact.get("slot") for fact in result.get("facts", [])]
    if not slots:
        raise AssertionError(f"{name}: expected facts, got none")

    bad_slots = [slot for slot in slots if slot != expected_slot]
    if bad_slots:
        raise AssertionError(
            f"{name}: expected all returned slots to be {expected_slot!r}, "
            f"got slots={slots}"
        )


def require_contains(
    name: str,
    result: dict[str, Any],
    required_terms: list[str],
) -> None:
    text = stringify(result)
    missing = [term for term in required_terms if term.lower() not in text]
    if missing:
        raise AssertionError(
            f"{name}: missing required terms {missing}; result={result}"
        )


async def ask(
    message: str,
    entity_id: str | None = None,
    top_k: int = 5,
) -> dict[str, Any]:
    req = RetailOpsDemo2KbReq(
        message=message,
        entity_id=entity_id,
        top_k=top_k,
    )
    return await chat_retail_ops_demo2_kb(req)


async def run_checks() -> int:
    passed: list[str] = []
    failed: list[str] = []

    async def run_case(name: str, case_func) -> None:
        try:
            await case_func()
            passed.append(f"[PASS] {name}")
        except AssertionError as exc:
            failed.append(f"[FAIL] {name}: {exc}")

    async def case_store_e_transaction_conversion_profile() -> None:
        name = "Store E transaction-conversion endpoint behavior"
        result = await ask(
            message="For Store E in Demo 2, explain the March 2026 transaction and conversion profile. Do not make a final operating recommendation.",
            entity_id="store_E",
        )
        require_supported(name, result)
        require_demo2_endpoint_metadata(name, result)
        require_slot(name, result, "transaction_conversion_profile")
        require_contains(
            name,
            result,
            [
                "transaction amount",
                "order volume",
                "order conversion rate",
                "payment conversion",
                "average order value",
                "current monthly evidence",
                "read alongside",
            ],
        )

    async def case_store_b_activity_boundary() -> None:
        name = "Store B activity endpoint behavior"
        result = await ask(
            message="Describe Store B activity and subsidy structure in Demo 2.",
            entity_id="store_B",
        )
        require_supported(name, result)
        require_demo2_endpoint_metadata(name, result)
        require_slot(name, result, "activity_lever_profile")


    async def case_unmatched_evidence_refusal() -> None:
        name = "Unmatched evidence-class refusal"
        result = await ask(
            message="What was Store B's employee headcount in March 2026?",
            entity_id="store_B",
        )
        require_refusal(name, result)
        require_contains(
            name,
            result,
            [
                "No supported Demo 2 retail memory fact",
            ],
        )

    async def case_strategy_transfer_refusal() -> None:
        name = "Pairwise strategy-transfer refusal"
        result = await ask(
            message="Can Store B's activity strategy be transferred to Store C?",
            entity_id=None,
        )
        require_refusal(name, result)
        require_contains(
            name,
            result,
            [
                "not pairwise strategy-transfer approval",
                "final operating recommendations",
            ],
        )

    async def case_cross_store_same_period_guardrail() -> None:
        name = "B-F cross-store same-period guardrail endpoint behavior"
        result = await ask(
            message="Compare stores B-F in the same-period Demo 2 diagnostic.",
            entity_id=None,
            top_k=10,
        )
        require_supported(name, result)
        require_demo2_endpoint_metadata(name, result)
        require_all_slots(name, result, "single_metric_attribution_guard")

    async def case_all_48_store_refusal() -> None:
        name = "All-48-store unsupported scope refusal"
        result = await ask(
            message="Can this Demo 2 endpoint summarize all 48 stores?",
            entity_id=None,
        )
        require_refusal(name, result)
        require_contains(
            name,
            result,
            [
                "not all 48 stores",
            ],
        )

    async def case_best_store_recommendation_refusal() -> None:
        name = "Best-store and subsidy recommendation refusal"
        result = await ask(
            message="Which store is best and should receive more subsidy?",
            entity_id=None,
        )
        require_refusal(name, result)
        require_contains(
            name,
            result,
            [
                "not best-store ranking",
                "final operating recommendations",
            ],
        )

    async def case_out_of_demo2_entity_refusal() -> None:
        name = "Out-of-Demo-2 entity refusal"
        result = await ask(
            message="Explain Store A using the Demo 2 endpoint.",
            entity_id="store_A",
        )
        require_refusal(name, result)
        if result.get("facts") != []:
            raise AssertionError(f"{name}: an out-of-scope store must return no facts")

    await run_case(
        "Store E transaction-conversion endpoint behavior",
        case_store_e_transaction_conversion_profile,
    )
    await run_case(
        "Store B activity endpoint behavior",
        case_store_b_activity_boundary,
    )
    await run_case(
        "Unmatched evidence-class refusal",
        case_unmatched_evidence_refusal,
    )
    await run_case(
        "Pairwise strategy-transfer refusal",
        case_strategy_transfer_refusal,
    )
    await run_case(
        "B-F cross-store same-period guardrail endpoint behavior",
        case_cross_store_same_period_guardrail,
    )
    await run_case(
        "All-48-store unsupported scope refusal",
        case_all_48_store_refusal,
    )
    await run_case(
        "Best-store and subsidy recommendation refusal",
        case_best_store_recommendation_refusal,
    )
    await run_case(
        "Out-of-Demo-2 entity refusal",
        case_out_of_demo2_entity_refusal,
    )

    lines: list[str] = []

    if failed:
        lines.append("Retail Demo 2 endpoint behavior evaluation FAILED.")
        lines.extend(failed)
        lines.extend(passed)
        exit_code = 1
    else:
        lines.append("Retail Demo 2 endpoint behavior evaluation passed.")
        lines.extend(passed)
        lines.append("[PASS] Demo 2 endpoint returns file-backed facts for supported Store B-F questions.")
        lines.append("[PASS] Demo 2 endpoint refuses unsupported all-48-store scope.")
        lines.append("[PASS] Demo 2 endpoint refuses best-store or final operating recommendations.")
        lines.append("[PASS] Demo 2 endpoint refuses pairwise strategy-transfer approval.")
        lines.append("[PASS] Demo 2 endpoint preserves same-period diagnostic boundary.")
        exit_code = 0

    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("\n".join(lines))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run_checks()))
