from __future__ import annotations

import asyncio
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import api.main as api
from api.retail_evidence_scope import DEMO1_WINDOW, DEMO2_WINDOW, RetailWindow, parse_retail_window


ROOT = Path(__file__).resolve().parents[1]


class RetailEvidenceScopeTests(unittest.TestCase):
    def setUp(self):
        self.demo2_facts = api.load_demo2_retail_facts()
        self.demo1_facts = json.loads((ROOT / "retail_ops/outputs/generated_retail_memory_facts.json").read_text())

    def ask_demo2(self, message="Store B transaction amount in March 2026", facts=None, top_k=5):
        facts = self.demo2_facts if facts is None else facts
        before = copy.deepcopy(facts)
        with patch.object(api, "load_demo2_retail_facts", return_value=facts):
            result = asyncio.run(api.chat_retail_ops_demo2_kb(api.RetailOpsDemo2KbReq(message=message, top_k=top_k)))
        self.assertEqual(facts, before)
        return result

    def ask_demo1(self, facts, message="Store A exposure", mode="scroll", scores=None, top_k=5):
        points = [{"score": score, "payload": fact} for fact, score in zip(facts, scores or [0.99] * len(facts))]
        async def scroll_slot(*, slot, **kwargs):
            return [point for point in points if point["payload"].get("slot") == slot] if mode == "scroll" else []
        with patch.object(api, "qdrant_scroll_retail_slot", AsyncMock(side_effect=scroll_slot)), patch.object(api, "qdrant_query_retail", AsyncMock(return_value=points)):
            return asyncio.run(api.chat_retail_ops_kb(api.RetailOpsKbReq(message=message, top_k=top_k)))

    def assert_no_evidence(self, result):
        self.assertFalse(result["supported"])
        self.assertEqual(result["facts"], [])

    def test_month_without_year_uses_declared_fixture_year_and_keeps_scope(self):
        for month in ("4月", "四月", "April", "Apr", "2026年四月", "2026 April", "２０２６年４月"):
            with self.subTest(month=month), patch.object(api, "load_demo2_retail_facts") as load:
                result = asyncio.run(api.chat_retail_ops_demo2_kb(api.RetailOpsDemo2KbReq(message="B店成交金额 " + month)))
                self.assert_no_evidence(result)
                load.assert_not_called()
        for month in ("3月", "三月", "March", "Mar", "2026年三月", "2026 March", "2026-03", "2026/3"):
            with self.subTest(month=month):
                result = self.ask_demo2("B店成交金额 " + month)
                self.assertTrue(result["supported"])
                self.assertEqual({fact["period_label"] for fact in result["facts"]}, {"2026-03"})

    def test_no_time_uses_endpoint_default_but_year_or_relative_time_does_not(self):
        self.assertEqual(parse_retail_window("Store A exposure", DEMO1_WINDOW), DEMO1_WINDOW)
        self.assertEqual(parse_retail_window("What may affect entry?", DEMO2_WINDOW), DEMO2_WINDOW)
        self.assertEqual(parse_retail_window("Review activity before deciding", DEMO2_WINDOW), DEMO2_WINDOW)
        for time in ("2026", "last month", "this month", "latest", "上个月", "今年", "最近", "Q1", "全年", "since March 2026"):
            with self.subTest(time=time):
                self.assert_no_evidence(self.ask_demo2("Store B transaction amount " + time))

    def test_partial_month_and_incomplete_range_notation_cannot_default_to_full_month(self):
        for time in ("三月上旬", "3月下旬", "March 2026 week 1", "first half of March", "3/15", "2-3月", "二月到4月上旬"):
            with self.subTest(time=time), self.assertRaises(ValueError):
                parse_retail_window(time, DEMO2_WINDOW)

    def test_explicit_range_keeps_exact_boundaries(self):
        for value in ("February to April 2026", "2026年2月至4月", "二月到四月", "2026-02_to_2026-04", "2026/02/01 到 2026/04/30"):
            with self.subTest(value=value):
                self.assertEqual(parse_retail_window(value, DEMO1_WINDOW), DEMO1_WINDOW)
        for value in ("March and April 2026", "2026年3月和4月", "2026-03-01 to 2026-04-30"):
            with self.subTest(value=value):
                self.assertEqual(parse_retail_window(value, DEMO1_WINDOW), RetailWindow("2026-03-01", "2026-04-30"))
        for value in ("February and April 2026", "March or April 2026", "2026-03-01 to 2026-03-31 and 2026年3月15日",
                      "2026-03-01 to 2026-03-31 and March 15, 2026", "2026-03 and 2027", "2026年13月"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_retail_window(value, DEMO1_WINDOW)

    def test_well_formed_fact_from_wrong_window_is_not_relabelled(self):
        for fact in self.demo2_facts:
            if fact["entity_id"] == "store_B":
                fact.update(period_start="2026-04-01", period_end="2026-04-30", period_label="2026-04")
        self.assert_no_evidence(self.ask_demo2())

    def test_mixed_windows_select_matching_facts_before_top_k(self):
        april = copy.deepcopy(self.demo2_facts)
        for fact in april:
            fact.update(period_start="2026-04-01", period_end="2026-04-30", period_label="2026-04")
        result = self.ask_demo2(facts=april + self.demo2_facts, top_k=2)
        self.assertTrue(result["supported"])
        self.assertEqual(len(result["facts"]), 2)
        self.assertEqual({fact["period_label"] for fact in result["facts"]}, {"2026-03"})

    def test_fact_dates_label_and_granularity_must_agree(self):
        changes = (
            {"period_label": "2026-04"}, {"period_start": "2026-03-02"},
            {"period_end": "2026-03-30"}, {"period_end": "2026-03-32"},
            {"period_granularity": "day"}, {"period_granularity": "month_range"},
            {"period_month": "2026-04"}, {"period_label": None},
        )
        for change in changes:
            facts = copy.deepcopy(self.demo2_facts)
            next(fact for fact in facts if fact["entity_id"] == "store_B" and fact["slot"] == "transaction_conversion_profile").update(change)
            with self.subTest(change=change):
                self.assert_no_evidence(self.ask_demo2(facts=facts))

    def test_missing_each_required_store_slot_blocks_partial_comparison(self):
        message = "Compare Stores B and C transaction amounts and activity"
        for store in ("store_B", "store_C"):
            for slot in ("transaction_conversion_profile", "activity_lever_profile"):
                facts = [fact for fact in self.demo2_facts if (fact["entity_id"], fact["slot"]) != (store, slot)]
                with self.subTest(store=store, slot=slot):
                    self.assert_no_evidence(self.ask_demo2(message, facts, top_k=10))
        result = self.ask_demo2(message, top_k=4)
        self.assertTrue(result["supported"])
        self.assertEqual({(fact["entity_id"], fact["slot"]) for fact in result["facts"]},
                         {(store, slot) for store in ("store_B", "store_C")
                          for slot in ("transaction_conversion_profile", "activity_lever_profile")})

    def test_single_store_missing_requested_slot_is_not_supported_by_guard_alone(self):
        for slot in ("transaction_conversion_profile", "single_metric_attribution_guard"):
            facts = [fact for fact in self.demo2_facts if not (fact["entity_id"] == "store_B" and fact["slot"] == slot)]
            with self.subTest(slot=slot):
                self.assert_no_evidence(self.ask_demo2(facts=facts))

    def test_single_store_top_k_cannot_truncate_required_slots(self):
        result = self.ask_demo2(top_k=1)
        self.assert_no_evidence(result)
        self.assertIn("top_k", result["answer"])
        with patch.object(api, "qdrant_scroll_retail_slot", AsyncMock()) as scroll:
            result = asyncio.run(api.chat_retail_ops_kb(api.RetailOpsKbReq(message="Store A exposure", top_k=1)))
        self.assert_no_evidence(result)
        scroll.assert_not_awaited()

    def test_duplicate_active_key_requires_version_selection(self):
        duplicated = copy.deepcopy(next(fact for fact in self.demo2_facts if fact["entity_id"] == "store_B" and fact["slot"] == "transaction_conversion_profile"))
        for amount in (duplicated["observed_values"]["transaction_amount"], 1):
            duplicated["observed_values"]["transaction_amount"] = amount
            with self.subTest(amount=amount):
                self.assert_no_evidence(self.ask_demo2(facts=self.demo2_facts + [duplicated]))

    def test_empty_statement_or_trace_cannot_fill_a_required_slot(self):
        changes = ({"value": ""}, {"value": None}, {"observed_values": {}},
                   {"source_fields": []}, {"source_path": None}, {"calculation": ""},
                   {"lineage_path": ""}, {"limitations": []}, {"confidence": "low"},
                   {"confidence": []}, {"confidence": {"level": "high"}},
                   {"slot": ["transaction_conversion_profile"]}, {"slot": None})
        for change in changes:
            facts = copy.deepcopy(self.demo2_facts)
            next(fact for fact in facts if fact["entity_id"] == "store_B" and fact["slot"] == "transaction_conversion_profile").update(change)
            with self.subTest(change=change):
                self.assert_no_evidence(self.ask_demo2(facts=facts))

    def test_null_and_zero_values_are_returned_without_backfill_or_new_metric_rules(self):
        for value in (None, 0):
            facts = copy.deepcopy(self.demo2_facts)
            source = next(fact for fact in facts if fact["entity_id"] == "store_B" and fact["slot"] == "transaction_conversion_profile")
            source["observed_values"]["transaction_amount"] = value
            with self.subTest(value=value):
                result = self.ask_demo2(facts=facts)
                self.assertTrue(result["supported"])
                returned = next(fact for fact in result["facts"] if fact["slot"] == "transaction_conversion_profile")
                self.assertEqual(returned["observed_values"], source["observed_values"])

    def test_demo1_range_fact_does_not_answer_a_narrower_month_request(self):
        facts = [fact for fact in self.demo1_facts if fact["slot"] in {"visibility_entry_profile", "single_metric_attribution_guard"}]
        for mode in ("scroll", "vector"):
            with self.subTest(mode=mode):
                self.assertTrue(self.ask_demo1(facts, "Store A exposure from February to April 2026", mode)["supported"])
                self.assert_no_evidence(self.ask_demo1(facts, "Store A exposure in March 2026", mode))

    def test_both_demo1_paths_check_returned_slot_period_and_coverage(self):
        facts = [fact for fact in self.demo1_facts if fact["slot"] in {"visibility_entry_profile", "single_metric_attribution_guard"}]
        for mode in ("scroll", "vector"):
            with self.subTest(mode=mode):
                self.assert_no_evidence(self.ask_demo1(facts[:1], mode=mode))
                changed = copy.deepcopy(facts)
                changed[0]["period_label"] = "2026-03"
                self.assert_no_evidence(self.ask_demo1(changed, mode=mode))
                wrong_slots = [fact for fact in self.demo1_facts if fact["slot"] == "activity_lever_profile"]
                self.assert_no_evidence(self.ask_demo1(wrong_slots, mode=mode))

    def test_vector_threshold_applies_to_every_required_fact(self):
        facts = [fact for fact in self.demo1_facts if fact["slot"] in {"visibility_entry_profile", "single_metric_attribution_guard"}]
        for score in (0.1, None, float("nan"), float("inf"), True):
            with self.subTest(score=score):
                self.assert_no_evidence(self.ask_demo1(facts, mode="vector", scores=[0.99, score]))
        self.assertTrue(self.ask_demo1(facts, mode="vector", scores=[0.8, 0.9])["supported"])

    def test_vector_score_does_not_choose_between_duplicate_active_versions(self):
        facts = [fact for fact in self.demo1_facts if fact["slot"] in {"visibility_entry_profile", "single_metric_attribution_guard"}]
        self.assert_no_evidence(self.ask_demo1(facts + [copy.deepcopy(facts[0])], mode="vector", scores=[0.99, 0.99, 0.1]))

    def test_comparative_fact_keeps_dictionary_permitted_baseline_periods(self):
        facts = copy.deepcopy(self.demo2_facts)
        source = next(fact for fact in facts if fact["entity_id"] == "store_B" and fact["slot"] == "transaction_conversion_profile")
        source["observed_values"] = {
            "2026-02": {"transaction_amount": 1},
            "2026-03": {"transaction_amount": 2},
        }
        result = self.ask_demo2(facts=facts)
        self.assertTrue(result["supported"])
        returned = next(fact for fact in result["facts"] if fact["slot"] == source["slot"])
        self.assertEqual(returned["observed_values"], source["observed_values"])
        self.assertEqual(returned["period_label"], "2026-03")

    def test_qdrant_requests_filter_the_declared_window(self):
        response = unittest.mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"result": {"points": []}}
        client = AsyncMock()
        client.post.return_value = response
        manager = AsyncMock()
        manager.__aenter__.return_value = client
        with patch.object(api.httpx, "AsyncClient", return_value=manager), patch.object(api, "ollama_embed", AsyncMock(return_value=[0.1])):
            asyncio.run(api.qdrant_scroll_retail_slot("store_a", "visibility_entry_profile", window=DEMO1_WINDOW))
            response.json.return_value = {"result": []}
            asyncio.run(api.qdrant_query_retail("exposure", "store_a", window=DEMO1_WINDOW))
        for call in client.post.call_args_list:
            must = {entry["key"]: entry["match"]["value"] for entry in call.kwargs["json"]["filter"]["must"]}
            for field in ("period_start", "period_end", "period_label", "period_granularity"):
                self.assertEqual(must[field], getattr(DEMO1_WINDOW, field))

    def test_file_loader_works_from_other_working_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            env = dict(os.environ, PYTHONPATH=str(ROOT))
            completed = subprocess.run([sys.executable, "-c", "from api.main import load_demo2_retail_facts; print(len(load_demo2_retail_facts()))"], cwd=temporary, env=env, capture_output=True, text=True, check=True)
            self.assertEqual(completed.stdout.strip(), "25")

    def test_flat_docker_module_layout_has_its_scope_module_and_fact_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for line in (ROOT / "api/Dockerfile").read_text().splitlines():
                if not line.startswith("COPY "):
                    continue
                _, source, destination = line.split()
                destination = root / destination.removeprefix("/app/")
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / source, destination)
            completed = subprocess.run([sys.executable, "-c", "from main import load_demo2_retail_facts; print(len(load_demo2_retail_facts()))"], cwd=root, capture_output=True, text=True, check=True)
            self.assertEqual(completed.stdout.strip(), "25")


if __name__ == "__main__":
    unittest.main()
