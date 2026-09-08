"""Recompute a transient RAC state from verified date-range query evidence.

The application supplies queries from one pinned publication. This module is
not an intake route: clients and models cannot supply evidence to the API.
All direction rules describe selected values, not causes or store quality.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, localcontext
from itertools import combinations
import json
from pathlib import Path
import re

from jsonschema import Draft202012Validator

from retail_ops.ingestion.contracts import load_dataset_contracts
from retail_ops.ingestion.intake_registry import _unique
from retail_ops.ingestion.preview import KEYS, SCHEMAS
from retail_ops.ingestion.publication import PUBLICATION_PATTERN
from retail_ops.ingestion.source_query import _range, _store_result, _validate_entries


ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = "rac/contracts/range_review.v1.json"
SCHEMA_PATH = "rac/schemas/range_cognition_state.v1.schema.json"
METHOD = "registered_range_evidence_rules_v1"
COMPARISON_FIELDS = {
    "transaction_amount", "transaction_orders", "exposure_users", "exposure_times",
    "search_exposure_users", "search_entry_users", "entry_users", "entry_times",
    "entry_conversion_rate_pct", "order_users", "order_conversion_rate_pct",
    "activity_orders", "activity_cost", "merchant_subsidy_amount",
    "platform_subsidy_amount", "activity_cost_ratio_pct",
}
PRIMARY_FIELDS = {"transaction_amount", "transaction_orders"}
EXPECTED_POLICY = {
    "review_contract_version": "1", "method": METHOD,
    "accepted_grain": "store_period",
    "comparison_fields": sorted(COMPARISON_FIELDS),
    "primary_fields": sorted(PRIMARY_FIELDS),
    "default_fields": sorted(PRIMARY_FIELDS),
    "scope_comparison_fields": ["source_system", "source_page", "scope_version", "timezone", "selection_conditions"],
    "same_store_identity_fields": ["binding_id", "source_account_id", "source_store_id"],
    "reviewer_identity": "preserve_in_lineage_exclude_from_semantic_comparison",
    "mixed_summary_basis_fields": sorted(PRIMARY_FIELDS),
    "comparison": "exact_difference_and_direction_of_selected_values",
    "unequal_window_lengths": "retain_lengths_no_normalization",
    "other_fields": "preserve_source_values_without_direction_rules",
    "confidence": "not_estimated",
    "query_storage": "none",
}


def _policy(root: Path) -> None:
    if json.loads((root / POLICY_PATH).read_text(encoding="utf-8"), object_pairs_hook=_unique) != EXPECTED_POLICY:
        raise ValueError("range RAC policy differs from the registered evidence rules")


def default_review_fields(root: Path = ROOT) -> list[str]:
    """Default to transaction observations; expand only selected factors."""
    _policy(Path(root))
    return sorted(PRIMARY_FIELDS)


def _validated_query(query: dict, root: Path) -> dict:
    """Reconcile summaries and coverage with the query's selected source rows."""
    if not isinstance(query, dict) or set(query) != {
        "publication_id", "dataset_id", "period_start", "period_end", "fields", "stores"
    }:
        raise ValueError("range RAC requires a complete source query result")
    if not isinstance(query["publication_id"], str) or not re.fullmatch(PUBLICATION_PATTERN, query["publication_id"]):
        raise ValueError("range RAC requires a publication ID")
    contracts = load_dataset_contracts(root)
    dataset = query["dataset_id"]
    if not isinstance(dataset, str) or dataset not in contracts or dataset not in SCHEMAS:
        raise ValueError("range RAC requires a registered dataset")
    contract = contracts[dataset]
    if contract.grain != "store_period":
        raise ValueError("range RAC currently reviews store-period datasets; ranked records remain queryable")
    first, last = _range(query["period_start"], query["period_end"])
    fields = query["fields"]
    allowed = SCHEMAS[dataset] - KEYS - set(contract.key_fields)
    if (not isinstance(fields, list) or not fields or
            any(not isinstance(field, str) or field not in allowed for field in fields) or
            fields != sorted(set(fields))):
        raise ValueError("range RAC fields differ from the registered query fields")
    stores = query["stores"]
    if not isinstance(stores, list) or not stores:
        raise ValueError("range RAC requires selected stores")
    store_ids = []
    projected = set(fields) | set(contract.key_fields) | {"period_month"}
    for store in stores:
        if (not isinstance(store, dict) or not isinstance(store.get("store_id"), str) or
                not store["store_id"] or store["store_id"] != store["store_id"].strip()):
            raise ValueError("invalid range RAC store")
        store_ids.append(store["store_id"])
        entries = []
        for key in ("records", "overlapping_records"):
            if not isinstance(store.get(key), list):
                raise ValueError("range RAC requires source records and overlap records")
            for item in store[key]:
                if (not isinstance(item, dict) or set(item) != {"record", "source"} or
                        not isinstance(item["record"], dict) or set(item["record"]) != projected or
                        item["record"].get("store_id") != store["store_id"]):
                    raise ValueError("range RAC source projection or store identity differs")
                # Unrequested fields are never used by the review. Complete the
                # shape only to reuse canonical type/window/lineage validation.
                record = dict.fromkeys(SCHEMAS[dataset])
                record.update(item["record"])
                entries.append({"dataset_id": dataset, "record": record, "source": item["source"]})
        _validate_entries(entries, contracts)
        if any(item["record"]["period_start"] > last.isoformat() or
               item["record"]["period_end"] < first.isoformat() for item in entries):
            raise ValueError("range RAC source is outside the selected query")
        # source_query returns contained and crossing rows separately. Re-sort
        # their union to reproduce the SQL's ordering before reconciliation.
        entries.sort(key=lambda item: (
            item["record"]["period_start"], item["record"]["period_end"],
            item["source"]["batch_id"], item["source"]["source_line_end"],
            json.dumps(item, ensure_ascii=False, sort_keys=True),
        ))
        expected = _store_result(store["store_id"], entries, fields, contract, first, last)
        if store != expected:
            raise ValueError("range RAC query summaries or coverage do not match the selected source records")
    if store_ids != sorted(set(store_ids)):
        raise ValueError("range RAC stores must be unique and sorted")
    return query


def _packet(query, store, field, window_id):
    metric = store["metrics"][field]
    return {"evidence_id": f"{window_id}/{store['store_id']}/{field}",
            "field": field, "store_id": store["store_id"], "window_id": window_id,
            "period_start": query["period_start"], "period_end": query["period_end"],
            "value": metric["value"], "basis": metric["basis"], "reason": metric["reason"],
            "coverage": deepcopy(metric["coverage"]), "ambiguities": list(store["ambiguities"]),
            "records": [{"record": {key: value for key, value in item["record"].items()
                                     if key in KEYS or key == field},
                         "source": deepcopy(item["source"])} for item in store["records"]],
            "overlapping_records": [{"record": {key: value for key, value in item["record"].items()
                                                 if key in KEYS or key == field},
                                     "source": deepcopy(item["source"])} for item in store["overlapping_records"]]}


def _supported(packet):
    return packet["value"] is not None and not packet["ambiguities"] and packet["basis"] in {
        "reported_window", "sum_non_overlapping_days"
    }


def _scope(packet, same_store):
    signatures = set()
    for item in packet["records"]:
        source = item["source"]
        scope = source["aggregation_scope"]
        if scope is None:
            return None
        signature = (source["source_system"], source["source_page"], scope["scope_version"],
                     scope["timezone"], scope["selection_conditions"])
        if same_store:
            signature += (source["binding_id"], source["source_account_id"], source["source_store_id"])
        signatures.add(signature)
    return next(iter(signatures)) if len(signatures) == 1 else None


def _difference(left, right):
    numbers = [Decimal(str(left)), Decimal(str(right))]
    if any(not value.is_finite() for value in numbers):
        raise ValueError("range RAC cannot compare non-finite values")
    integer_digits = max(max(value.adjusted() + 1, 1) for value in numbers)
    fraction_digits = max(max(-value.as_tuple().exponent, 0) for value in numbers)
    with localcontext() as context:
        context.prec = integer_digits + fraction_digits + 2
        return format(numbers[1] - numbers[0], "f")


def _label(packet):
    return f"{packet['store_id']}店 {packet['period_start']}—{packet['period_end']}"


def _record_check(packet):
    supported = _supported(packet)
    return {"check_id": "record/" + packet["evidence_id"], "field": packet["field"],
            "operation": "record_values", "evidence_ids": [packet["evidence_id"]],
            "status": "supported" if supported else "unresolved", "result": "recorded" if supported else "unresolved",
            "difference": None, "reasons": [] if supported else [packet["reason"]],
            "claim": (f"{_label(packet)} {packet['field']}：{packet['value']}。" if supported else
                      f"{_label(packet)} {packet['field']} 暂无完整区间值（{packet['reason']}）。")}


def _comparison(left, right):
    field = left["field"]
    reasons = []
    if field not in COMPARISON_FIELDS:
        reasons.append("source_values_only")
    if not _supported(left) or not _supported(right):
        reasons.extend(f"{packet['window_id']}/{packet['store_id']}: {packet['reason']}"
                       for packet in (left, right) if not _supported(packet))
    same_store = left["store_id"] == right["store_id"]
    left_scope, right_scope = _scope(left, same_store), _scope(right, same_store)
    if left_scope is None or right_scope is None:
        reasons.append("comparison_scope_unreviewed")
    elif left_scope != right_scope:
        reasons.append("different_comparison_scopes")
    if left["basis"] != right["basis"] and field not in PRIMARY_FIELDS:
        reasons.append("different_summary_bases")
    result, difference = "unresolved", None
    if not reasons:
        difference = _difference(left["value"], right["value"])
        delta = Decimal(difference)
        result = "increased" if delta > 0 else "decreased" if delta < 0 else "unchanged"
    direction = {"increased": "高于", "decreased": "低于", "unchanged": "等于"}
    delta_unit = " 个百分点" if field.endswith("_pct") else "（原指标单位）"
    claim = (f"{_label(right)} {field}（{right['value']}）{direction[result]}"
             f"{_label(left)}（{left['value']}），差值 {difference}{delta_unit}。" if not reasons else
             f"{field}：{_label(left)} 与 {_label(right)} 的区间比较待核对（{'；'.join(reasons)}）。")
    return {"check_id": f"compare/{left['evidence_id']}/{right['evidence_id']}",
            "field": field, "operation": "compare", "evidence_ids": [left["evidence_id"], right["evidence_id"]],
            "status": "unresolved" if reasons else "supported", "result": result,
            "difference": difference, "reasons": reasons, "claim": claim}


def _hypothesis(identifier, claim, status, checks, limitations=None):
    return {"hypothesis_id": identifier, "claim": claim, "status": status, "confidence": None,
            "check_ids": [item["check_id"] for item in checks],
            "evidence_ids": sorted({value for item in checks for value in item["evidence_ids"]}),
            "limitations": limitations or []}


def _hypotheses(checks, temporal, packet_map):
    hypotheses = []
    observed = [item for item in checks if item["operation"] == "record_values"]
    complete = all(item["status"] == "supported" for item in observed)
    hypotheses.append(_hypothesis("selected_values_complete", "所选字段均有完整、无歧义的区间值。",
                                   "supported" if complete else "contradicted", observed))
    hypotheses.append(_hypothesis("selected_values_have_gaps", "至少一个所选字段仍缺少完整、无歧义的区间值。",
                                   "contradicted" if complete else "supported", observed))
    comparisons = [item for item in checks if item["operation"] == "compare"]
    for item in comparisons:
        # Background data have no registered direction hypothesis. Their values
        # are retained in the same evidence packets and record checks.
        if item["field"] not in COMPARISON_FIELDS:
            continue
        left, right = [packet_map[identifier] for identifier in item["evidence_ids"]]
        for direction, text in (("increased", "高于"), ("decreased", "低于"), ("unchanged", "等于")):
            status = ("unresolved" if item["status"] != "supported" else
                      "supported" if item["result"] == direction else "contradicted")
            hypotheses.append(_hypothesis(item["check_id"] + "/" + direction,
                f"{item['field']}：{_label(right)}的所选值{text}{_label(left)}的所选值。", status, [item]))
    if temporal:
        by_field = {item["field"]: item for item in comparisons}
        groups = (
            ("transaction_measures_align", ["transaction_amount", "transaction_orders"], "成交金额与成交订单量的区间值同向变化。"),
            ("search_transaction_align", ["search_exposure_users", "transaction_amount", "transaction_orders"], "搜索曝光人数与两项成交指标的区间值同向变化。"),
            ("activity_orders_align", ["activity_orders", "transaction_orders"], "活动订单数与成交订单量的区间值同向变化。"),
        )
        for identifier, fields, claim in groups:
            if not all(field in by_field for field in fields):
                continue
            relevant = [by_field[field] for field in fields]
            directions = {item["result"] for item in relevant}
            status = ("unresolved" if any(item["status"] != "supported" for item in relevant) else
                      "supported" if directions in ({"increased"}, {"decreased"}) else "contradicted")
            hypotheses.append(_hypothesis(identifier, claim, status, relevant,
                ["同向变化只描述这些区间值之间的关系；现有记录没有分离各因素的因果作用。"] if identifier != "transaction_measures_align" else []))
    return hypotheses


def review_range_queries(current: dict, baseline: dict | None = None, *, root: Path = ROOT) -> dict:
    """Build evidence, evaluate rival statements, then update the transient belief."""
    root = Path(root)
    _policy(root)
    current = _validated_query(current, root)
    if baseline is not None:
        baseline = _validated_query(baseline, root)
        for key in ("publication_id", "dataset_id", "fields"):
            if current[key] != baseline[key]:
                raise ValueError(f"range RAC {key} must match across both windows")
        if (len(current["stores"]) != 1 or len(baseline["stores"]) != 1 or
                current["stores"][0]["store_id"] != baseline["stores"][0]["store_id"]):
            raise ValueError("two-window range RAC requires the same single store")
        if baseline["period_end"] >= current["period_start"]:
            raise ValueError("baseline must finish before the current range starts")
    queries = [("baseline", baseline), ("current", current)] if baseline is not None else [("current", current)]
    packets = [_packet(query, store, field, window_id) for window_id, query in queries
               for store in query["stores"] for field in query["fields"]]
    packet_map = {item["evidence_id"]: item for item in packets}
    checks = [_record_check(packet) for packet in packets]
    if baseline is not None:
        store = current["stores"][0]["store_id"]
        pairs = [(f"baseline/{store}", f"current/{store}")]
    else:
        pairs = [(f"current/{left['store_id']}", f"current/{right['store_id']}")
                 for left, right in combinations(current["stores"], 2)]
    for left, right in pairs:
        for field in current["fields"]:
            if field in COMPARISON_FIELDS:
                checks.append(_comparison(packet_map[f"{left}/{field}"], packet_map[f"{right}/{field}"]))
    hypotheses = _hypotheses(checks, baseline is not None, packet_map)
    unresolved = [item for item in checks if item["status"] != "supported"]
    findings = [{"code": "unresolved_evidence", "issue": item["claim"], "evidence_ids": item["evidence_ids"]}
                for item in unresolved]
    windows = [{"window_id": window_id, "period_start": query["period_start"], "period_end": query["period_end"],
                "days": (_range(query["period_start"], query["period_end"])[1] -
                         _range(query["period_start"], query["period_end"])[0]).days + 1}
               for window_id, query in queries]
    if baseline is not None and windows[0]["days"] != windows[1]["days"]:
        findings.append({"code": "different_window_lengths",
            "issue": f"两个区间分别覆盖 {windows[0]['days']} 天和 {windows[1]['days']} 天；差值比较所选区间值，没有换算日均或经营效率。",
            "evidence_ids": [item["evidence_id"] for item in packets]})
    if len(current["stores"]) > 1:
        findings.append({"code": "cross_store_context_unresolved",
            "issue": "同日期下的数值对照尚未核验门店经营环境、活动安排与商品结构是否适合某项经营决策。",
            "evidence_ids": [item["evidence_id"] for item in packets]})
    if any(item["hypothesis_id"] in {"search_transaction_align", "activity_orders_align"} for item in hypotheses):
        findings.append({"code": "causal_effects_unresolved",
            "issue": "搜索、活动与成交的同向检查会随数据重算；这些观察尚未识别原因。",
            "evidence_ids": [item["evidence_id"] for item in packets]})
    comparisons = [item for item in checks if item["operation"] == "compare"]
    # An unresolved comparison must not hide its independently reported values.
    # Fields without direction rules also retain their ordinary record checks.
    compared = {identifier for item in comparisons for identifier in item["evidence_ids"]}
    unresolved_ids = {identifier for item in comparisons if item["status"] != "supported"
                      for identifier in item["evidence_ids"]}
    report_checks = [item for item in checks if item["operation"] == "record_values" and
                     (item["evidence_ids"][0] not in compared or item["evidence_ids"][0] in unresolved_ids)] + comparisons
    report = "\n".join(item["claim"] for item in report_checks)
    contextual = [item["issue"] for item in findings if item["code"] != "unresolved_evidence"]
    if contextual:
        report += "\n" + "\n".join(contextual)
    factors = []
    for field in current["fields"]:
        relevant = [item for item in checks if item["field"] == field]
        statuses = {item["status"] for item in relevant}
        factors.append({"factor_id": field, "priority": "primary" if field in PRIMARY_FIELDS else "context",
            "evidence_status": "supported" if statuses == {"supported"} else
                               "partially_supported" if "supported" in statuses else "missing",
            "check_ids": [item["check_id"] for item in relevant]})
    return {"method": METHOD, "question_type": "factual" if not pairs else "comparability_judgment",
            "domain": "meituan_retail_ops", "confidence_method": "not_estimated",
            "scope": {"publication_id": current["publication_id"], "dataset_id": current["dataset_id"],
                      "store_ids": [store["store_id"] for store in current["stores"]],
                      "fields": list(current["fields"]), "windows": windows},
            "factors": factors, "evidence_packets": packets, "checks": checks, "hypotheses": hypotheses,
            "critic_findings": findings,
            "fact_check": {"status": "pass_with_warnings" if findings else "pass",
                           "unresolved_check_ids": [item["check_id"] for item in unresolved]},
            "belief_update": {"status": "tentative" if unresolved else "active", "confidence": None,
                "claim": report,
                "accepted_hypothesis_ids": [item["hypothesis_id"] for item in hypotheses if item["status"] == "supported"],
                "rejected_hypothesis_ids": [item["hypothesis_id"] for item in hypotheses if item["status"] == "contradicted"],
                "unresolved_hypothesis_ids": [item["hypothesis_id"] for item in hypotheses if item["status"] == "unresolved"],
                "validity_conditions": ["仅针对当前 publication_id、门店、数据集、字段和日期区间。",
                                        "来源版本或查询区间变化后重新检查证据、假设和判断。"]},
            "final_report": report}


def validate_range_state(state: dict, current: dict, baseline: dict | None = None, *, root: Path = ROOT) -> None:
    """Check the declared state shape and every evidence-dependent output."""
    root = Path(root)
    schema = json.loads((root / SCHEMA_PATH).read_text(encoding="utf-8"), object_pairs_hook=_unique)
    Draft202012Validator.check_schema(schema)
    errors = sorted(Draft202012Validator(schema).iter_errors(state), key=lambda error: str(list(error.path)))
    if errors:
        raise ValueError("range RAC state schema validation failed: " + errors[0].message)
    if state != review_range_queries(current, baseline, root=root):
        raise ValueError("range RAC state does not match recomputed evidence, hypotheses and belief")
