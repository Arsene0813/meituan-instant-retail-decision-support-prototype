"""Synthetic uploads through publication, range RAC and the read-only HTTP API."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import csv
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from retail_ops.ingestion.batch_store import receive_batch
from retail_ops.ingestion.publication import open_publication, publish
from retail_ops.ingestion import range_analysis, source_query
from api.retail_range_api import create_app


ROOT = Path(__file__).resolve().parents[1]
DATASET = "store_period_panel_metrics"
SCOPE = {
    "scope_version": "1", "timezone": "Asia/Shanghai",
    "selection_conditions": "Synthetic all channels, all products and all statuses; no non-date filters.",
    "reviewed_by": "synthetic-range-reviewer",
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


class RetailRangeAnalysisPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.db = self.base / "batches.sqlite3"
        self.directory = self.base / "publications"
        self.registry_path = self.base / "registry.json"
        self.registry = {"registry_version": "2", "bindings": [], "uploads": []}
        self.counter = 0

    def receive(self, store="B", start="2026-03-01", end=None, *, amount="100",
                orders="10", predecessor=None, scope=SCOPE):
        end = end or start
        self.counter += 1
        binding_id = "synthetic-range-binding-" + store
        if not any(row["binding_id"] == binding_id for row in self.registry["bindings"]):
            identity = {"source_system": "meituan_merchant_backend",
                        "source_account_id": "synthetic-range-account",
                        "source_store_id": "synthetic-range-store-" + store}
            raw_identity = json.dumps(identity).encode()
            filename = "identity-" + store + ".json"
            (self.base / filename).write_bytes(raw_identity)
            self.registry["bindings"].append({**identity, "binding_id": binding_id,
                "store_id": store, "dataset_ids": [DATASET],
                "identity_evidence_path": filename, "identity_evidence_sha256": sha(raw_identity),
                "reviewed_by": "synthetic-range-reviewer"})
        month = start[:7] if (start, end) == source_query.month_dates(start[:7]) else ""
        record = {"store_id": store, "period_start": start, "period_end": end,
                  "period_month": month, "transaction_amount": amount,
                  "transaction_orders": orders, "entry_users": "8"}
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=list(record), lineterminator="\n")
        writer.writeheader()
        writer.writerow(record)
        data = buffer.getvalue().encode()
        upload_id = "synthetic-range-upload-" + str(self.counter)
        receipt = {"upload_id": upload_id, "binding_id": binding_id, "dataset_id": DATASET,
            "period_start": start, "period_end": end, "file_sha256": sha(data),
            "source_page": "synthetic-range-source", "extracted_at": "2026-04-01T00:00:00+00:00",
            "mapping_version": "canonical_csv_v2"}
        if scope is not None:
            receipt["aggregation_scope"] = dict(scope)
        self.registry["uploads"].append(receipt)
        self.registry_path.write_text(json.dumps(self.registry), encoding="utf-8")
        result = receive_batch(ROOT, self.db, self.registry_path, upload_id, data,
                               supersedes_batch_id=predecessor)
        self.assertEqual(result["status"], "validated", result["errors"])
        return result["batch_id"]

    def publish(self, batches):
        return publish(ROOT, self.db, self.directory, batches, source_records=True)["publication_id"]

    def review(self, publication_id, *, stores=None, start="2026-03-02", end="2026-03-02",
               baseline_start="2026-03-01", baseline_end="2026-03-01", fields=None):
        return range_analysis.analyze_publication_range(ROOT, self.directory, publication_id,
            DATASET, stores or ["B"], start, end,
            fields or ["transaction_amount", "transaction_orders"],
            baseline_start=baseline_start, baseline_end=baseline_end)

    @staticmethod
    def store(query, store="B"):
        return next(row for row in query["stores"] if row["store_id"] == store)

    def persistent_state(self):
        return {str(path.relative_to(self.base)): sha(path.read_bytes()) if path.is_file() else None
                for path in sorted(self.base.rglob("*"))}

    def post(self, path, body, *, app=None):
        application = app if app is not None else create_app(directory=self.directory, root=ROOT)
        async def request():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                                         base_url="http://range-test") as client:
                return await client.post(path, json=body)
        return asyncio.run(request())

    @staticmethod
    def body(publication_id, **extra):
        return {"publication_id": publication_id, "dataset_id": DATASET,
                "store_ids": ["B"], "period_start": "2026-03-02", "period_end": "2026-03-02",
                "fields": ["transaction_amount", "transaction_orders"], **extra}

    def test_published_revision_recomputes_rac_and_old_publication_stays_repeatable(self):
        baseline = self.receive(amount="50", orders="5")
        current = self.receive(start="2026-03-02")
        old_id = self.publish([baseline, current])
        before = self.review(old_id)
        changed = self.receive(start="2026-03-02", amount="20", orders="2", predecessor=current)
        new_id = self.publish([baseline, changed])
        after = self.review(new_id)
        self.assertEqual(before["publication_id"], old_id)
        self.assertEqual(after["publication_id"], new_id)
        self.assertEqual(self.store(before["query"])["metrics"]["transaction_amount"]["value"], "100")
        self.assertEqual(self.store(after["query"])["metrics"]["transaction_amount"]["value"], "20")
        self.assertEqual(self.review(old_id), before)
        for state, direction, difference in ((before["rac"], "increased", "50"),
                                             (after["rac"], "decreased", "-30")):
            self.assertEqual(state["scope"]["publication_id"],
                             old_id if direction == "increased" else new_id)
            check = next(item for item in state["checks"]
                         if item["operation"] == "compare" and item["field"] == "transaction_amount")
            self.assertEqual(check["status"], "supported")
            self.assertEqual(check["result"], direction)
            self.assertEqual(check["difference"], difference)
            accepted = next(item for item in state["hypotheses"]
                            if item["hypothesis_id"] == check["check_id"] + "/" + direction)
            self.assertEqual(accepted["status"], "supported")
            self.assertIn(accepted["hypothesis_id"], state["belief_update"]["accepted_hypothesis_ids"])
            self.assertEqual(state["final_report"], state["belief_update"]["claim"])
        self.assertNotEqual(before["rac"]["hypotheses"], after["rac"]["hypotheses"])
        self.assertNotEqual(before["rac"]["belief_update"], after["rac"]["belief_update"])
        self.assertNotEqual(before["rac"]["final_report"], after["rac"]["final_report"])

    def test_zero_and_null_revision_are_preserved_without_reading_predecessor(self):
        baseline = self.receive(amount="0", orders="0")
        current = self.receive(start="2026-03-02", amount="100", orders="10")
        old_id = self.publish([baseline, current])
        replacement = self.receive(start="2026-03-02", amount="", orders="0", predecessor=current)
        new_id = self.publish([baseline, replacement])
        result = self.review(new_id)
        self.assertEqual(self.store(result["baseline"])["metrics"]["transaction_amount"]["value"], "0")
        store = self.store(result["query"])
        self.assertIsNone(store["metrics"]["transaction_amount"]["value"])
        self.assertEqual(store["metrics"]["transaction_orders"]["value"], 0)
        self.assertEqual(store["records"][0]["source"]["batch_id"], replacement)
        self.assertIsNone(store["records"][0]["record"]["transaction_amount"])
        checks = {item["field"]: item for item in result["rac"]["checks"] if item["operation"] == "compare"}
        self.assertEqual(checks["transaction_amount"]["status"], "unresolved")
        self.assertEqual(checks["transaction_orders"]["result"], "unchanged")
        self.assertEqual(result["rac"]["belief_update"]["status"], "tentative")
        self.assertEqual(self.store(self.review(old_id)["query"])["metrics"]["transaction_amount"]["value"], "100")

    def test_arbitrary_ranges_keep_current_and_baseline_record_lineage(self):
        batches = [self.receive(start="2026-03-0" + str(day), amount=str(day), orders="1")
                   for day in range(1, 5)]
        publication_id = self.publish(batches)
        result = self.review(publication_id, start="2026-03-03", end="2026-03-04",
                             baseline_start="2026-03-01", baseline_end="2026-03-02")
        self.assertEqual(result["publication_id"], publication_id)
        for key, selected, value in (("baseline", batches[:2], "3"), ("query", batches[2:], "7")):
            query = result[key]
            self.assertEqual(query["publication_id"], publication_id)
            store = self.store(query)
            self.assertEqual(store["metrics"]["transaction_amount"]["value"], value)
            self.assertEqual({row["source"]["batch_id"] for row in store["records"]}, set(selected))
            self.assertTrue(all(row["source"]["source_line_end"] == 2 for row in store["records"]))
            self.assertTrue(all(row["source"]["file_sha256"] for row in store["records"]))
            packet_window = "baseline" if key == "baseline" else "current"
            packet = next(item for item in result["rac"]["evidence_packets"]
                          if item["window_id"] == packet_window and item["field"] == "transaction_amount")
            self.assertEqual(packet["value"], value)
            self.assertEqual({row["source"]["batch_id"] for row in packet["records"]}, set(selected))
            self.assertTrue(all(row["source"]["source_line_end"] == 2 for row in packet["records"]))

    def test_review_opens_one_publication_and_new_release_cannot_mix_windows(self):
        baseline = self.receive(amount="50")
        current = self.receive(start="2026-03-02", amount="100")
        first = self.publish([baseline, current])
        released = []
        @contextmanager
        def replace_after_pin(root, directory, publication_id):
            with open_publication(root, directory, publication_id) as pinned:
                replacement = self.receive(start="2026-03-02", amount="20", predecessor=current)
                released.append(self.publish([baseline, replacement]))
                yield pinned
        with patch.object(range_analysis, "open_publication", side_effect=replace_after_pin) as opener:
            result = self.review(first)
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(result["query"]["publication_id"], first)
        self.assertEqual(result["baseline"]["publication_id"], first)
        self.assertEqual(self.store(result["query"])["metrics"]["transaction_amount"]["value"], "100")
        self.assertEqual(self.store(result["baseline"])["metrics"]["transaction_amount"]["value"], "50")
        self.assertEqual(self.store(self.review(released[0])["query"])["metrics"]["transaction_amount"]["value"], "20")

    def test_missing_records_cannot_fall_back_to_repository_csv_or_saved_facts(self):
        publication_id = self.publish([self.receive()])
        original = Path.open
        def verified_open(path, *args, **kwargs):
            resolved = path.resolve()
            if (resolved.is_relative_to(ROOT / "retail_ops/outputs") or
                    resolved.is_relative_to(ROOT / "rac/outputs") or
                    resolved.is_relative_to(ROOT / "retail_ops/data") and resolved.suffix == ".csv"):
                raise AssertionError("Repository evidence fallback: " + str(path))
            return original(path, *args, **kwargs)
        with patch.object(Path, "open", verified_open):
            result = self.review(publication_id)
        self.assertEqual(self.store(result["query"])["records"], [])
        self.assertIsNone(self.store(result["query"])["metrics"]["transaction_amount"]["value"])
        self.assertEqual(len(self.store(result["baseline"])["records"]), 1)
        self.assertTrue(all(item["status"] == "unresolved" for item in result["rac"]["checks"]
                            if item["operation"] == "compare"))
        self.assertEqual(result["rac"]["belief_update"]["status"], "tentative")

    def test_http_natural_month_and_identical_selected_dates_return_identical_json(self):
        previous = self.receive(start="2026-02-01", end="2026-02-28", amount="50")
        current = self.receive(end="2026-03-31", amount="100")
        publication_id = self.publish([previous, current])
        common = {"publication_id": publication_id, "dataset_id": DATASET,
                  "store_ids": ["B"], "fields": ["transaction_amount"]}
        before = self.persistent_state()
        for path in ("/retail/query", "/retail/review"):
            selected = {**common, "period_start": "2026-03-01", "period_end": "2026-03-31"}
            month = {**common, "month": "2026-03"}
            if path.endswith("review"):
                selected["baseline"] = {"period_start": "2026-02-01", "period_end": "2026-02-28"}
                month["baseline"] = {"month": "2026-02"}
            shortcut = self.post(path, month)
            explicit = self.post(path, selected)
            self.assertEqual(shortcut.status_code, 200, shortcut.text)
            self.assertEqual(explicit.status_code, 200, explicit.text)
            self.assertEqual(shortcut.json(), explicit.json())
        self.assertEqual(self.persistent_state(), before)

    def test_http_and_direct_review_never_write_queries_or_change_intake_database(self):
        publication_id = self.publish([self.receive(amount="50"), self.receive(start="2026-03-02")])
        before = self.persistent_state()
        direct = self.review(publication_id)
        queried = self.post("/retail/query", self.body(publication_id))
        reviewed = self.post("/retail/review", self.body(publication_id,
            baseline={"period_start": "2026-03-01", "period_end": "2026-03-01"}))
        self.assertEqual(queried.status_code, 200, queried.text)
        self.assertEqual(reviewed.status_code, 200, reviewed.text)
        self.assertEqual(queried.json(), direct["query"])
        self.assertEqual(reviewed.json(), direct)
        self.assertEqual(self.review(publication_id), direct)
        self.assertEqual(self.persistent_state(), before)

    def test_review_uses_published_archive_after_database_and_original_registry_are_removed(self):
        publication_id = self.publish([self.receive(amount="50"), self.receive(start="2026-03-02")])
        original = self.review(publication_id)
        self.db.unlink()
        self.registry_path.unlink()
        (self.base / "identity-B.json").unlink()
        before = self.persistent_state()
        self.assertEqual(self.review(publication_id), original)
        self.assertEqual(self.persistent_state(), before)
        self.assertFalse(self.db.exists())

    def test_http_rejects_caller_source_evidence_and_storage_overrides(self):
        publication_id = self.publish([self.receive(start="2026-03-02")])
        before = self.persistent_state()
        for path in ("/retail/query", "/retail/review"):
            for extra in ({"directory": str(self.directory)}, {"root": str(ROOT)},
                          {"evidence": {"transaction_amount": "999999"}}, {"save": True}):
                with self.subTest(path=path, extra=extra):
                    response = self.post(path, self.body(publication_id, **extra))
                    self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.persistent_state(), before)

    def test_http_review_cross_store_scope_uses_all_requested_stores(self):
        publication_id = self.publish([self.receive(start="2026-03-02", amount="50"),
            self.receive("C", start="2026-03-02", amount="100")])
        response = self.post("/retail/review", self.body(publication_id, store_ids=["B", "C"]))
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertIsNone(result["baseline"])
        self.assertEqual([row["store_id"] for row in result["query"]["stores"]], ["B", "C"])
        self.assertEqual(self.store(result["query"], "B")["metrics"]["transaction_amount"]["value"], "50")
        self.assertEqual(self.store(result["query"], "C")["metrics"]["transaction_amount"]["value"], "100")

    def test_http_rejects_overlapping_or_multi_store_baseline(self):
        publication_id = self.publish([self.receive(start="2026-03-02")])
        for body in (self.body(publication_id, baseline={"month": "2026-03"}),
                     self.body(publication_id, store_ids=["B", "C"],
                               baseline={"period_start": "2026-03-01", "period_end": "2026-03-01"})):
            response = self.post("/retail/review", body)
            self.assertEqual(response.status_code, 422, response.text)


if __name__ == "__main__":
    unittest.main()
