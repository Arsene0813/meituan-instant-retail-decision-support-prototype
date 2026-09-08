from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

import api.main as api
from api.retail_entity_scope import extract_retail_entity_ids


class RetailEntityScopeTests(unittest.TestCase):
    def demo2(self, message, entity_id=None):
        return asyncio.run(api.chat_retail_ops_demo2_kb(
            api.RetailOpsDemo2KbReq(message=message, entity_id=entity_id)
        ))

    def test_unknown_stores_and_invalid_parameters_stop_before_loading(self):
        cases = [
            ("Store G transaction amount", "store_b"),
            ("Store B2 transaction amount", "store_b"),
            ("store_B_extra transaction amount", "store_b"),
            ("Compare Stores G and H transaction amounts", None),
            ("Compare Store B and Store G transaction amounts", None),
            ("Compare Stores B-G transaction amounts", None),
            ("Compare Stores B and C transaction amounts", "store_z"),
            ("Compare Stores B and C transaction amounts", "store_d"),
            ("G店成交金额", "store_b"),
            ("门店42成交金额", "store_b"),
            ("跨店比较成交金额", "store_b"),
        ]
        for message, entity in cases:
            with self.subTest(message=message, entity=entity), patch.object(api, "load_demo2_retail_facts") as load:
                result = self.demo2(message, entity)
                self.assertFalse(result["supported"])
                self.assertEqual(result["facts"], [])
                load.assert_not_called()

    def test_demo1_unknown_store_stops_before_retrieval(self):
        for message in ("Store G exposure", "门店B曝光", "store_A_extra exposure", "Store 42 exposure"):
            with self.subTest(message=message), patch.object(api, "qdrant_scroll_retail_slot", new_callable=AsyncMock) as scroll, patch.object(api, "qdrant_query_retail", new_callable=AsyncMock) as vector:
                result = asyncio.run(api.chat_retail_ops_kb(api.RetailOpsKbReq(message=message)))
                self.assertFalse(result["supported"])
                self.assertEqual(result["facts"], [])
                scroll.assert_not_awaited()
                vector.assert_not_awaited()

    def test_unique_explicit_store_resolves_without_parameter(self):
        for message in ("Store B transaction amount", "store_B transaction_amount", "StoreB transaction amount", "门店B的成交金额", "B店成交金额"):
            with self.subTest(message=message):
                result = self.demo2(message)
                self.assertTrue(result["supported"])
                self.assertEqual({fact["entity_id"] for fact in result["facts"]}, {"store_B"})
        self.assertFalse(self.demo2("transaction amount")["supported"])

    def test_named_store_sets_and_ranges_remain_exact(self):
        for message, expected in (
            ("Compare Stores B-D transaction amounts", {"store_B", "store_C", "store_D"}),
            ("Compare Stores B, C and F transaction amounts", {"store_B", "store_C", "store_F"}),
            ("比较B店和C店成交金额", {"store_B", "store_C"}),
            ("Compare Stores B-F transaction amounts", {"store_B", "store_C", "store_D", "store_E", "store_F"}),
        ):
            with self.subTest(message=message):
                result = self.demo2(message)
                self.assertTrue(result["supported"])
                self.assertEqual({fact["entity_id"] for fact in result["facts"]}, expected)
        for message in ("Review store billing metrics", "Review storefront layout", "Review sub-field definition"):
            self.assertEqual(extract_retail_entity_ids(message), [])

    def test_missing_requested_store_does_not_return_partial_comparison(self):
        facts = [fact for fact in api.load_demo2_retail_facts() if fact["entity_id"] != "store_C"]
        with patch.object(api, "load_demo2_retail_facts", return_value=facts):
            result = self.demo2("Compare Stores B and C transaction amounts")
        self.assertFalse(result["supported"])
        self.assertEqual(result["facts"], [])

    def test_inconsistent_fact_metadata_is_not_relabelled_or_returned(self):
        source = api.load_demo2_retail_facts()
        for change in (
            {"entity_id_norm": "store_c"}, {"store_id": "C"},
            {"entity_id": "store_b"}, {"entity_id": None},
            {"is_active": False}, {"is_active": 1},
            {"domain": "other"}, {"kind": "structured_fact"},
            {"type": "other"},
        ):
            facts = copy.deepcopy(source)
            for fact in facts:
                if fact["entity_id"] == "store_B":
                    fact.update(change)
            saved = copy.deepcopy(facts)
            with self.subTest(change=change), patch.object(api, "load_demo2_retail_facts", return_value=facts):
                result = self.demo2("Store B transaction amount", "store_b")
                self.assertFalse(result["supported"])
                self.assertEqual(result["facts"], [])
                self.assertEqual(facts, saved)

    def test_qdrant_scroll_and_vector_results_recheck_identity(self):
        path = Path(__file__).resolve().parents[1] / "retail_ops/outputs/generated_retail_memory_facts.json"
        facts = [item for item in json.loads(path.read_text())
                 if item["slot"] in {"visibility_entry_profile", "single_metric_attribution_guard"}]
        for mode in ("scroll", "vector"):
            for change, supported in (({}, True), ({"entity_id": "store_B"}, False), ({"entity_id_norm": "store_b"}, False)):
                points = [{"score": 0.99, "payload": dict(fact, **change)} for fact in facts]
                async def scroll_slot(*, slot, **kwargs):
                    return [point for point in points if point["payload"]["slot"] == slot] if mode == "scroll" else []
                scroll = AsyncMock(side_effect=scroll_slot)
                vector = AsyncMock(return_value=points)
                with self.subTest(mode=mode, change=change), patch.object(api, "qdrant_scroll_retail_slot", scroll), patch.object(api, "qdrant_query_retail", vector):
                    result = asyncio.run(api.chat_retail_ops_kb(api.RetailOpsKbReq(message="Store A exposure")))
                self.assertEqual(result["supported"], supported)
                if not supported:
                    self.assertEqual(result["facts"], [])


if __name__ == "__main__":
    unittest.main()
