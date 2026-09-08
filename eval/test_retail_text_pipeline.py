"""Synthetic reviewed Chinese uploads through publication, queries, RAC and HTTP."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import httpx

from api.retail_range_api import create_app
from retail_ops.ingestion import batch_store, range_analysis, source_query
from retail_ops.ingestion.publication import publish


ROOT = Path(__file__).resolve().parents[1]
DATASET = "store_period_panel_metrics"
RANKS = "demo2_top_skus_by_sales_volume"
SCOPE = {
    "scope_version": "1", "timezone": "Asia/Shanghai",
    "selection_conditions": "Synthetic all products, channels and order statuses; no non-date filters.",
    "reviewed_by": "synthetic-text-operator",
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def declaration(store="B", day="2026-09-07", block_line=1, dataset=DATASET):
    return {"store": store, "start": day, "end": day,
            "block_line": block_line, "dataset": dataset}


def daily_text(day="2026-09-07", amount="100", orders="10", store="B"):
    return (f"店铺：{store}\n时间范围{day}至{day}\n成交金额{amount}\n成交订单量{orders}\n").encode()


class RetailTextPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.db = self.base / "batches.sqlite3"
        self.directory = self.base / "publications"
        self.registry_path = self.base / "registry.json"
        self.registry = {"registry_version": "3", "bindings": [], "uploads": []}
        self.counter = 0

    def register(self, data, selections):
        """Declare operator-known source scope independently of the text parser."""
        self.counter += 1
        document_id = "synthetic-text-document-" + str(self.counter)
        upload_ids = []
        for index, selection in enumerate(selections):
            store = selection["store"]
            binding_id = "synthetic-text-binding-" + store
            if not any(row["binding_id"] == binding_id for row in self.registry["bindings"]):
                identity = {"source_system": "meituan_merchant_backend",
                            "source_account_id": "synthetic-text-account",
                            "source_store_id": "synthetic-text-store-" + store}
                raw_identity = json.dumps(identity).encode()
                identity_path = "identity-" + store + ".json"
                (self.base / identity_path).write_bytes(raw_identity)
                self.registry["bindings"].append({**identity, "binding_id": binding_id,
                    "store_id": store, "dataset_ids": [DATASET, RANKS],
                    "identity_evidence_path": identity_path,
                    "identity_evidence_sha256": sha(raw_identity),
                    "reviewed_by": "synthetic-text-operator"})
            upload_id = document_id + "-upload-" + str(index + 1)
            upload_ids.append(upload_id)
            self.registry["uploads"].append({
                "upload_id": upload_id, "binding_id": binding_id,
                "dataset_id": selection["dataset"], "document_id": document_id,
                "source_block_line": selection["block_line"],
                "period_start": selection["start"], "period_end": selection["end"],
                "file_sha256": sha(data), "source_page": "synthetic-text-source",
                "extracted_at": "2026-09-08T00:00:00+00:00", "mapping_version": "manual_text_v3",
                "aggregation_scope": dict(SCOPE)})
        self.registry_path.write_text(json.dumps(self.registry), encoding="utf-8")
        return document_id, upload_ids

    def receive(self, data, selections=None, predecessors=None):
        document_id, uploads = self.register(data, selections or [declaration()])
        supersedes = {uploads[index]: previous for index, previous in (predecessors or {}).items()}
        result = batch_store.receive_document(ROOT, self.db, self.registry_path, document_id, data,
                                               supersedes=supersedes)
        self.assertEqual(result["status"], "validated", result)
        self.assertEqual(len(result["batches"]), len(uploads))
        return result

    @staticmethod
    def batch(result, store="B", dataset=DATASET):
        return next(row for row in result["batches"]
                    if row["registration"]["binding"]["store_id"] == store
                    and row["metadata"]["dataset_id"] == dataset)

    def publish(self, *results):
        batches = [row["batch_id"] for result in results for row in result["batches"]]
        return publish(ROOT, self.db, self.directory, batches, source_records=True)["publication_id"]

    def query(self, publication_id, *, stores=None, start="2026-09-07", end="2026-09-07",
              dataset=DATASET, fields=None):
        return source_query.query_publication(ROOT, self.directory, publication_id, dataset,
            stores or ["B"], start, end, fields or ["transaction_amount", "transaction_orders"])

    def review(self, publication_id, *, stores=None, start="2026-09-07", end="2026-09-07",
               baseline=None, fields=None):
        return range_analysis.analyze_publication_range(ROOT, self.directory, publication_id,
            DATASET, stores or ["B"], start, end, fields or ["transaction_amount", "transaction_orders"],
            baseline_start=baseline, baseline_end=baseline)

    @staticmethod
    def store(result, store="B"):
        return next(row for row in result["stores"] if row["store_id"] == store)

    def post(self, endpoint, publication_id, **extra):
        body = {"publication_id": publication_id, "dataset_id": DATASET,
                "store_ids": ["B"], "period_start": "2026-09-07", "period_end": "2026-09-07",
                "fields": ["transaction_amount", "transaction_orders"], **extra}
        async def request():
            app = create_app(directory=self.directory, root=ROOT)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://text-test") as client:
                return await client.post(endpoint, json=body)
        response = asyncio.run(request())
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def persistent_state(self):
        return {str(path.relative_to(self.base)): sha(path.read_bytes()) if path.is_file() else None
                for path in sorted(self.base.rglob("*"))}

    def assert_locator(self, source, data, block_line, *, item_ordinal=None):
        self.assertEqual(source["file_sha256"], sha(data))
        locator = source["source_locator"]
        self.assertEqual(locator["locator_version"], "manual_text_locator_v1")
        self.assertEqual(locator["source_block_line"], block_line)
        self.assertEqual(locator["item_ordinal"], item_ordinal)
        spans = [locator["store"], locator["window"], *[item["span"] for item in locator["values"]]]
        lines = data.decode("utf-8-sig").splitlines()
        for span in spans:
            text = lines[span["line"] - 1][span["column_start"] - 1:span["column_end"] - 1]
            self.assertEqual(text, span["text"])
        self.assertEqual(source["source_line_end"], max(span["line"] for span in spans))
        return locator

    def test_bom_crlf_document_routes_stores_and_original_lineage_through_http_and_rac(self):
        data = ("\ufeff店铺：B\r\n时间范围2026-09-07至2026-09-07\r\n成交金额100.50\r\n"
                "成交订单量0\r\n搜索曝光人数\r\n店铺：C\r\n时间范围2026-09-07至2026-09-07\r\n"
                "成交金额20\r\n成交订单量2\r\n搜索曝光人数30\r\n").encode()
        intake = self.receive(data, [declaration(), declaration("C", block_line=6)])
        publication_id = self.publish(intake)
        fields = ["transaction_amount", "transaction_orders", "search_exposure_users"]
        result = self.review(publication_id, stores=["B", "C"], fields=fields)
        b, c = self.store(result["query"]), self.store(result["query"], "C")
        self.assertEqual(b["metrics"]["transaction_amount"]["value"], "100.50")
        self.assertEqual(b["metrics"]["transaction_orders"]["value"], 0)
        self.assertIsNone(b["metrics"]["search_exposure_users"]["value"])
        self.assertEqual(c["metrics"]["transaction_amount"]["value"], "20")
        self.assertEqual(c["metrics"]["search_exposure_users"]["value"], 30)
        for store, line in (("B", 1), ("C", 6)):
            row = self.store(result["query"], store)["records"][0]
            self.assertIsNone(row["record"]["period_month"])
            self.assertEqual(row["source"]["batch_id"], self.batch(intake, store)["batch_id"])
            locator = self.assert_locator(row["source"], data, line)
            self.assertTrue(any("transaction_amount" in value["fields"] for value in locator["values"]))
            packet = next(item for item in result["rac"]["evidence_packets"]
                          if item["store_id"] == store and item["field"] == "transaction_amount")
            self.assertEqual(packet["records"][0]["source"], row["source"])
        http = self.post("/retail/review", publication_id, store_ids=["B", "C"], fields=fields)
        self.assertEqual(http, result)

    def test_text_revision_updates_direction_hypotheses_belief_and_report(self):
        previous = self.receive(daily_text("2026-09-06", "50", "5"), [declaration(day="2026-09-06")])
        first = self.receive(daily_text())
        old_id = self.publish(previous, first)
        old = self.review(old_id, baseline="2026-09-06")
        replacement = self.receive(daily_text(amount="20", orders="2"),
                                   predecessors={0: self.batch(first)["batch_id"]})
        new_id = self.publish(previous, replacement)
        new = self.review(new_id, baseline="2026-09-06")
        for result, direction in ((old, "increased"), (new, "decreased")):
            check = next(item for item in result["rac"]["checks"]
                         if item["operation"] == "compare" and item["field"] == "transaction_amount")
            self.assertEqual(check["result"], direction)
            self.assertIn(check["check_id"] + "/" + direction,
                          result["rac"]["belief_update"]["accepted_hypothesis_ids"])
        self.assertNotEqual(old["rac"]["hypotheses"], new["rac"]["hypotheses"])
        self.assertNotEqual(old["rac"]["belief_update"], new["rac"]["belief_update"])
        self.assertNotEqual(old["rac"]["final_report"], new["rac"]["final_report"])
        self.assertEqual(self.review(old_id, baseline="2026-09-06"), old)
        self.assertEqual(self.store(new["query"])["records"][0]["source"]["batch_id"], self.batch(replacement)["batch_id"])

    def test_null_revision_keeps_zero_and_does_not_fill_from_earlier_text(self):
        first = self.receive(daily_text(amount="100.50", orders="7"))
        old_id = self.publish(first)
        revised_data = daily_text(amount="", orders="0")
        second = self.receive(revised_data, predecessors={0: self.batch(first)["batch_id"]})
        new_id = self.publish(second)
        result = self.review(new_id)
        store = self.store(result["query"])
        self.assertIsNone(store["metrics"]["transaction_amount"]["value"])
        self.assertEqual(store["metrics"]["transaction_orders"]["value"], 0)
        self.assertIsNone(store["records"][0]["record"]["transaction_amount"])
        self.assertEqual(result["rac"]["belief_update"]["status"], "tentative")
        self.assert_locator(store["records"][0]["source"], revised_data, 1)
        self.assertEqual(self.store(self.query(old_id))["metrics"]["transaction_amount"]["value"], "100.50")

    def test_text_decimal_and_big_integer_survive_query_and_http_without_float_conversion(self):
        amount = "12345678901234567890.12345678901234567890"
        orders = 9007199254740993
        data = daily_text(amount=amount, orders=str(orders))
        publication_id = self.publish(self.receive(data))
        direct = self.query(publication_id)
        http = self.post("/retail/query", publication_id)
        self.assertEqual(http, direct)
        metrics = self.store(http)["metrics"]
        self.assertEqual(metrics["transaction_amount"]["value"], amount)
        self.assertEqual(metrics["transaction_orders"]["value"], orders)
        self.assertIs(type(metrics["transaction_orders"]["value"]), int)
        result = self.review(publication_id)
        packet = next(item for item in result["rac"]["evidence_packets"] if item["field"] == "transaction_orders")
        self.assertEqual(packet["value"], orders)
        self.assert_locator(packet["records"][0]["source"], data, 1)

    def test_wrong_store_date_or_unknown_label_quarantines_entire_document(self):
        valid_b = daily_text()
        cases = {
            "wrong_store": valid_b + daily_text(store="D"),
            "wrong_day": valid_b + daily_text(day="2026-09-08", store="C"),
            "unknown_label": valid_b + daily_text(store="C") + "成交金额预测值999\n".encode(),
        }
        for label, data in cases.items():
            with self.subTest(label=label):
                document_id, _ = self.register(data, [declaration(), declaration("C", block_line=5)])
                result = batch_store.receive_document(ROOT, self.db, self.registry_path, document_id, data)
                self.assertEqual(result["status"], "quarantined", result)
                self.assertTrue(result["errors"])
                self.assertEqual(result["batches"], [])
                quarantine_id = result["quarantine_batch_id"]
                self.assertTrue(quarantine_id)
                self.assertEqual(batch_store.read_batch(self.db, quarantine_id)["status"], "quarantined")
                self.assertEqual(batch_store.read_batch_artifacts(self.db, quarantine_id)["data"], data)
                with sqlite3.connect(self.db) as connection:
                    self.assertEqual(connection.execute("SELECT count(*) FROM batches WHERE status='validated'").fetchone()[0], 0)
                with self.assertRaises(ValueError):
                    publish(ROOT, self.db, self.directory, [quarantine_id], source_records=True)
                self.assertFalse(self.directory.exists())

    def test_text_queries_and_rac_http_preserve_database_and_publication_files(self):
        first = self.receive(daily_text("2026-09-06", "50", "5"), [declaration(day="2026-09-06")])
        second = self.receive(daily_text())
        publication_id = self.publish(first, second)
        before = self.persistent_state()
        direct = self.review(publication_id, baseline="2026-09-06")
        query = self.post("/retail/query", publication_id)
        review = self.post("/retail/review", publication_id,
                           baseline={"period_start": "2026-09-06", "period_end": "2026-09-06"})
        self.assertEqual(query, direct["query"])
        self.assertEqual(review, direct)
        self.assertEqual(self.review(publication_id, baseline="2026-09-06"), direct)
        self.assertEqual(self.persistent_state(), before)

    def test_rankings_only_document_has_no_synthetic_store_panel(self):
        data = ("店铺：B\n时间范围2026-09-07至2026-09-07\n"
                "商品销量top3交易商品：示例商品甲（销量45），示例商品乙（销量33），示例商品乙（销量20）\n").encode()
        result = self.receive(data, [declaration(dataset=RANKS)])
        self.assertEqual({row["metadata"]["dataset_id"] for row in result["batches"]}, {RANKS})
        publication_id = self.publish(result)
        ranked = self.query(publication_id, dataset=RANKS, fields=["sku_name", "sales_volume"])
        records = sorted(self.store(ranked)["records"], key=lambda row: row["record"]["sku_rank"])
        self.assertEqual([row["record"]["sku_rank"] for row in records], [1, 2, 3])
        self.assertEqual([row["record"]["sales_volume"] for row in records], [45, 33, 20])
        self.assertEqual([row["record"]["sku_name"] for row in records], ["示例商品甲", "示例商品乙", "示例商品乙"])
        for rank, row in enumerate(records, 1):
            self.assert_locator(row["source"], data, 1, item_ordinal=rank)
        panel = self.query(publication_id)
        self.assertEqual(self.store(panel)["records"], [])
        self.assertIsNone(self.store(panel)["metrics"]["transaction_amount"]["value"])


if __name__ == "__main__":
    unittest.main()
