"""Registered RAC reviews computed after evidence selection.

Metric values retain the source text. Decimal is used only to compare the
provided values; these rules do not fill missing backend measurements.
"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import re
from typing import Any

from api.retail_entity_scope import extract_retail_entity_ids
from api.retail_evidence_scope import RetailWindow, parse_retail_window
from rac.src.mock_pipeline import analyze_question, expand_factors, route_evidence, weight_factors


METHOD = "registered_local_evidence_rules_v1"
CAUSAL_WINDOW = RetailWindow("2026-03-01", "2026-04-30")
COMPARISON_WINDOW = RetailWindow("2026-03-01", "2026-03-31")


def build_review_plan(question: str) -> dict[str, Any]:
    """Bind the current four review families before resolving any evidence."""
    analysis = analyze_question(question)
    kind = analysis["question_type"]
    entities = set(extract_retail_entity_ids(question))
    # Both 'March-to-April' and 'March to April' declare the same range.
    time_text = re.sub(r"(?i)-to-", " to ", question)
    if kind in {"causal_diagnostic", "comparability_judgment"}:
        window = CAUSAL_WINDOW if kind == "causal_diagnostic" else COMPARISON_WINDOW
        expected = {"store_a"} if kind == "causal_diagnostic" else {"store_" + s for s in "bcdef"}
        if entities != expected or parse_retail_window(time_text, window) != window:
            raise ValueError("The question does not match this registered RAC store/window scope.")
        scope = {"store_ids": [item.removeprefix("store_").upper() for item in sorted(expected)],
                 "period_start": window.period_start, "period_end": window.period_end}
    else:
        # Definition/design reviews have no selected business records. A
        # store-specific action question must not silently become a checklist.
        if entities or re.search(r"\d{4}|\b(?:january|february|march|april|may|june|july|august|september|october|november|december)\b|月", question, re.I):
            raise ValueError("This RAC review has no registered store-specific data scope.")
        if parse_retail_window(time_text, COMPARISON_WINDOW) != COMPARISON_WINDOW:
            raise ValueError("This document review does not select a reporting window.")
        if kind == "technical_design" and not ("rac" in question.lower() and "memory" in question.lower()):
            raise ValueError("No registered RAC review was found for this question.")
        scope = {"store_ids": [], "period_start": None, "period_end": None}
    factors = deepcopy(expand_factors(kind))
    if kind == "causal_diagnostic":
        amount = next(factor for factor in expand_factors("comparability_judgment")
                      if factor["factor_id"] == "transaction_amount")
        factors.append(deepcopy(amount))
        next(f for f in factors if f["factor_id"] == "transaction_orders")["description"] = (
            "Records the backend same-day paid and not-cancelled order count for the selected window.")
    for factor in factors:
        if factor["factor_id"] == "repeated_reporting_windows":
            factor["description"] = "Checks the registered panel records and their saved summary for repeated-window review."
    packets = route_evidence(kind, factors)
    for packet in packets:
        packet["limitations"] = ["Source files are read from the local review root.",
                                 "This packet covers only its registered factor route."]
    return {"question": question, "question_type": kind, "domain": analysis["domain"],
            "factors": factors, "factor_weights": weight_factors(factors),
            "evidence_packets": packets,
            "evidence_review": {"method": METHOD, "scope": scope,
                                "confidence_method": "not_estimated", "checks": []}}


def _record_checks(packet: dict, kind: str) -> list[dict]:
    checks = []
    rows = packet.get("evidence_values", [])
    matched = packet["grounding_status"] == "record_matched"
    for field in packet.get("evidence_fields", []):
        operands = [{"row_key": item["row_key"],
                     "value": item["values"].get(field) if str(item["values"].get(field) or "").strip() else None}
                    for item in rows]
        complete = matched and bool(operands) and all(item["value"] is not None for item in operands)
        status = "supported" if complete else ("missing" if matched or packet["grounding_status"] == "source_missing" else "invalid")
        operation = "compare" if kind == "causal_diagnostic" else "record_values"
        if operation == "compare" and complete:
            if len(operands) != 2:
                raise ValueError("A comparison requires exactly two independently selected records.")
            baseline, current = (Decimal(item["value"]) for item in operands)
            if not baseline.is_finite() or not current.is_finite():
                raise ValueError("Non-finite source values cannot enter a RAC comparison.")
            result = "increased" if current > baseline else "decreased" if current < baseline else "unchanged"
            claim = (f"{field} {result} from {operands[0]['value']} to {operands[1]['value']} "
                     "(2026-03 to 2026-04).")
        elif complete:
            result = "recorded"
            values = "; ".join(str(item["row_key"]["store_id"]) + "=" + str(item["value"]) for item in operands)
            claim = f"{field}: {values}."
        else:
            result = "unresolved"
            claim = f"{field}: the selected records do not provide a complete valid observation."
        checks.append({"check_id": packet["factor_id"] + "/" + field,
                       "factor_id": packet["factor_id"], "evidence_id": packet["evidence_id"],
                       "source_path": packet["source_path"], "field": field,
                       "operation": operation, "status": status, "result": result,
                       "operands": operands, "claim": claim})
    return checks


def build_checks(grounded: dict, kind: str) -> list[dict]:
    checks = []
    for packet in grounded["resolved_packets"]:
        if "evidence_fields" in packet:
            checks.extend(_record_checks(packet, kind))
            continue
        matched = packet["grounding_status"] in {"keyword_matched", "boundary_matched"}
        checks.append({"check_id": packet["factor_id"] + "/document",
                       "factor_id": packet["factor_id"], "evidence_id": packet["evidence_id"],
                       "source_path": packet["source_path"], "field": None,
                       "operation": "document_route", "status": "supported" if matched else "missing",
                       "result": "matched" if matched else "unresolved", "operands": [],
                       "claim": (f"{packet['factor_id']}: registered {packet['grounding_role']} anchor "
                                 + ("matched." if matched else "was not resolved."))})
    return checks


def _hypothesis(hypothesis_id, claim, factors, status, weaknesses):
    return {"hypothesis_id": hypothesis_id, "claim": claim, "confidence": None,
            "supporting_factors": factors, "weaknesses": weaknesses, "status": status}


def _causal_review(state, checks):
    by_field = {item["field"]: item for item in checks}
    outcomes = [by_field[field] for field in ("transaction_amount", "transaction_orders")]
    complete = all(item["status"] == "supported" for item in outcomes)
    observation = " ".join(item["claim"] for item in outcomes)
    search = by_field["search_exposure_users"]
    aligned = (complete and search["status"] == "supported"
               and search["result"] != "unchanged"
               and all(item["result"] == search["result"] for item in outcomes))
    search_complete = complete and search["status"] == "supported"
    activity = [item for item in checks if item["factor_id"] == "promotion_intensity"]
    activity_complete = all(item["status"] == "supported" for item in activity)
    activity_changed = any(item["result"] in {"increased", "decreased"} for item in activity)
    hypotheses = [
        _hypothesis("H1", observation, ["transaction_amount", "transaction_orders"],
                    "strong" if complete else "unsupported", ["These are within-store observations in the selected windows."]),
        _hypothesis("H2", "Search exposure and both transaction measures move in the same direction.",
                    ["search_exposure", "transaction_amount", "transaction_orders"],
                    "plausible" if aligned else "rejected" if search_complete else "unsupported",
                    [search["claim"], "Co-movement alone does not identify a search-only cause."]),
        _hypothesis("H3", "Activity participation remains an alternative explanation to investigate.",
                    ["promotion_intensity", "entry_conversion", "order_conversion"],
                    "plausible" if activity_complete and activity_changed else "weak" if activity_complete else "unsupported",
                    [" ".join(item["claim"] for item in activity),
                     "The selected activity metrics do not isolate campaign effects."]),
    ]
    claim = observation + " The selected observations do not isolate search exposure as the sole cause."
    validity = ["Store A, 2026-03-01 through 2026-04-30.",
                "Recompute these checks after any source record changes."]
    limitations = ["No comparison group or intervention evidence is supplied by these records."]
    return hypotheses, {"belief_id": "store_a_march_april_attribution", "claim": claim,
                        "confidence": None, "status": "active" if complete and all(item["status"] == "supported" for item in checks) else "tentative",
                        "validity_conditions": validity, "limitations": limitations}


def _comparison_review(state, checks):
    by_id = {item["check_id"]: item for item in checks}
    period = [item for item in checks if item["factor_id"] == "same_reporting_period"]
    same_period = bool(period) and all(item["status"] == "supported" for item in period)
    amount = by_id["transaction_amount/transaction_amount"]
    orders = by_id["order_volume/transaction_orders"]
    recorded = amount["status"] == orders["status"] == "supported"
    ready = same_period and recorded and all(item["status"] == "supported" for item in checks
                                            if item["operation"] == "record_values"
                                            and item["factor_id"] != "repeated_reporting_windows")
    missing = sorted({item["field"] or item["factor_id"] for item in checks if item["status"] != "supported"})
    repeated = by_id["repeated_reporting_windows/observed_month_count"]
    observation = " ".join([amount["claim"], orders["claim"], repeated["claim"]])
    organization = ("Stores B-F have verified March 2026 records for same-period diagnostic review."
                    if ready else "The selected records do not yet support complete B-F same-period diagnostic review.")
    hypotheses = [
        _hypothesis("H1", organization + " " + observation,
                    ["same_reporting_period", "transaction_amount", "order_volume", "repeated_reporting_windows"],
                    "strong" if ready else "unsupported", ["Only the stated fields and reporting windows were checked."]),
        _hypothesis("H2", "The current evidence establishes direct comparability for an operating decision.",
                    [factor["factor_id"] for factor in state["factors"]], "unsupported",
                    ["A question-specific pairwise gate is not implemented.", "Region type remains weak context."]),
    ]
    claim = organization + " " + observation + " Direct comparability remains unresolved."
    if missing:
        claim += " Unresolved evidence: " + ", ".join(missing) + "."
    return hypotheses, {"belief_id": "stores_b_f_same_period_not_directly_comparable", "claim": claim,
                        "confidence": None, "status": "active" if ready and not missing else "tentative",
                        "validity_conditions": ["B-F, March 2026; repeated evidence uses the registered February-April panel.",
                                                "Recompute after source or saved-summary changes."],
                        "limitations": ["Same-period diagnostic review is not a pairwise comparability gate.",
                                        "Region type remains weak context."]}


def _document_review(state, checks):
    matched = [item["factor_id"] for item in checks if item["status"] == "supported"]
    missing = [item["factor_id"] for item in checks if item["status"] != "supported"]
    factors = [factor["factor_id"] for factor in state["factors"]]
    strategic = state["question_type"] == "strategic_recommendation"
    focus = "promotion-review checklist" if strategic else "connection between RAC review states and typed memory"
    claim = f"The registered local documents cover the {focus}: " + ", ".join(matched or ["no resolved requirements"]) + "."
    if missing:
        claim += " Unresolved document routes: " + ", ".join(missing) + "."
    limitation = ("No store/window-specific promotion observations were selected."
                  if strategic else "Document anchors establish the design scope; they do not verify a running integration.")
    hypotheses = [
        _hypothesis("H1", claim, factors, "strong" if not missing else "unsupported", [limitation]),
        _hypothesis("H2", "The current document routes alone establish " + ("a promotion action." if strategic else "a running memory integration."),
                    factors, "unsupported", [limitation]),
    ]
    return hypotheses, {"belief_id": "promotion_changes_require_multi_factor_check" if strategic else "rac_should_layer_above_existing_memory",
                        "claim": claim, "confidence": None, "status": "active" if not missing else "tentative",
                        "validity_conditions": ["Only the registered definition/design routes are used."],
                        "limitations": [limitation]}


def recompute_review(state: dict[str, Any]) -> None:
    """Replace every decision-bearing field using the selected evidence."""
    kind = state["question_type"]
    checks = build_checks(state["grounded_evidence"], kind)
    state["evidence_review"]["checks"] = checks
    for weight in state["factor_weights"]:
        relevant = [item for item in checks if item["factor_id"] == weight["factor_id"]]
        weight["evidence_status"] = ("partially_supported" if any(item["status"] == "supported" for item in relevant) else "missing")
    by_factor = {packet["factor_id"]: packet for packet in state["grounded_evidence"]["resolved_packets"]}
    for packet in state["evidence_packets"]:
        factor_id = packet["evidence_id"].removeprefix("evidence_")
        resolved = by_factor[factor_id]
        packet["source_path"] = resolved["source_path"]
        packet["source_type"] = "csv" if "evidence_fields" in resolved else "markdown"
        packet["claim_supported"] = " ".join(item["claim"] for item in checks if item["factor_id"] == factor_id)
    if kind == "causal_diagnostic":
        hypotheses, belief = _causal_review(state, checks)
    elif kind == "comparability_judgment":
        hypotheses, belief = _comparison_review(state, checks)
    else:
        hypotheses, belief = _document_review(state, checks)
    state["hypotheses"], state["belief_update"] = hypotheses, belief
    unresolved = [item for item in checks if item["status"] != "supported"]
    findings = [{"issue": item["claim"], "severity": "high" if item["status"] == "invalid" else "medium",
                 "recommendation": "Check the indicated source record or registered document route."} for item in unresolved]
    if kind == "causal_diagnostic":
        findings.append({"issue": "Alternative explanations have not been isolated by the selected observations.",
                         "severity": "high", "recommendation": "Use aligned intervention and comparison evidence to test causal explanations."})
        outcome_checks = [item for item in checks if item["field"] in {"transaction_amount", "transaction_orders"}]
        premise_rejected = (bool(re.search(r"\bincrease[sd]?\b|\bgrowth\b", state["question"], re.I))
                            and any(item["status"] == "supported" and item["result"] != "increased" for item in outcome_checks))
    else:
        premise_rejected = False
        findings.append({"issue": ("Shared reporting dates do not establish direct comparability."
                                   if kind == "comparability_judgment" else
                                   "The selected document anchors describe review requirements, not measured operating outcomes."),
                         "severity": "medium", "recommendation": "Use observations and tests appropriate to the proposed decision."})
    if premise_rejected:
        findings.insert(0, {"issue": "The question's premise that both transaction measures increased is contradicted by the selected values.",
                            "severity": "high", "recommendation": "Use the recomputed directions shown in the evidence checks."})
    state["critic_findings"] = findings
    state["fact_check"] = {"status": "pass_with_warnings" if unresolved or premise_rejected else "pass",
                           "unsupported_claims": [findings[0]["issue"]] if premise_rejected else [],
                           "definition_conflicts": []}
    state["final_report"] = ""
