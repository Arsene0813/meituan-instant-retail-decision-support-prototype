"""Atomic manual document intake uses synthetic reviewed identities and records."""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator

from retail_ops.ingestion import batch_store, intake_registry

ROOT = Path(__file__).resolve().parents[1]
PANEL = "store_period_panel_metrics"
SALES = "demo2_top_skus_by_sales_volume"


def sha(data):
    return hashlib.sha256(data).hexdigest()


class RetailDocumentIntakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.database = self.base / "batches.sqlite3"
        self.registry_path = self.base / "registry.json"
        self.data = ("\ufeff店铺：B\r\n时间范围2026.3\r\n成交金额12.3400\r\n成交订单量0\r\n"
                     "入店人数\r\n商品销量top3交易商品：同名商品（销量3），同名商品（销量0），商品丙（销量）\r\n"
                     "店铺：C\r\n时间范围2026.3\r\n成交金额30\r\n成交订单量2\r\n").encode()
        self.registry = {"registry_version": "3", "bindings": [], "uploads": []}
        for store in ("B", "C"):
            identity = {"source_system": "meituan_merchant_backend", "source_account_id": "synthetic-account",
                        "source_store_id": "synthetic-" + store}
            original = (json.dumps(identity, indent=2) + "\n").encode()
            path = f"identity-{store}.json"
            (self.base / path).write_bytes(original)
            self.registry["bindings"].append({**identity, "binding_id": "binding-" + store,
                "store_id": store, "dataset_ids": [PANEL, SALES], "identity_evidence_path": path,
                "identity_evidence_sha256": sha(original), "reviewed_by": "synthetic-operator"})
        self.add_document("doc1", self.data)

    def save(self):
        self.registry_path.write_text(json.dumps(self.registry, ensure_ascii=False), encoding="utf-8")

    def add_document(self, name, data, *, version="manual_text_v2", start="2026-03-01", end="2026-03-31"):
        for suffix, store, line, dataset in (("b-panel", "B", 1, PANEL), ("b-sales", "B", 1, SALES),
                                             ("c-panel", "C", 7, PANEL)):
            self.registry["uploads"].append({"upload_id": f"{name}-{suffix}", "document_id": name,
                "binding_id": "binding-" + store, "source_block_line": line, "dataset_id": dataset,
                "period_start": start, "period_end": end, "file_sha256": sha(data),
                "source_page": "synthetic-manual-source", "extracted_at": "2026-05-01T00:00:00Z",
                "mapping_version": version})
        self.save()

    def receive(self, name="doc1", data=None, **kwargs):
        return batch_store.receive_document(ROOT, self.database, self.registry_path, name,
                                            self.data if data is None else data, **kwargs)

    def validated_count(self):
        if not self.database.exists():
            return 0
        with closing(sqlite3.connect(self.database)) as connection:
            return connection.execute("SELECT count(*) FROM batches WHERE status='validated'").fetchone()[0]

    def test_complete_document_routes_three_groups_and_preserves_original_bytes(self):
        result = self.receive()
        self.assertEqual(result["status"], "validated", result["errors"])
        self.assertEqual(len(result["batches"]), 3)
        self.assertEqual(self.validated_count(), 3)
        for child in result["batches"]:
            self.assertEqual(child["metadata"]["mapping_version"], "manual_text_v2")
            self.assertEqual(batch_store.read_batch_artifacts(self.database, child["batch_id"])["data"], self.data)
        first = result["batches"][0]["preview"]["validated_records"][0]
        self.assertEqual(first["record"]["transaction_amount"], "12.3400")
        self.assertEqual(first["record"]["transaction_orders"], 0)
        self.assertIsNone(first["record"]["entry_users"])
        self.assertEqual(first["source_locator"]["source_block_line"], 1)
        ranks = result["batches"][1]["preview"]["validated_records"]
        self.assertEqual([row["record"]["sku_rank"] for row in ranks], [1, 2, 3])
        self.assertEqual([row["record"]["sales_volume"] for row in ranks], [3, 0, None])
        self.assertEqual(ranks[0]["record"]["sku_name"], ranks[1]["record"]["sku_name"])

    def test_every_independent_identity_is_archived_with_each_child(self):
        result = self.receive()
        for child in result["batches"]:
            bundle = child["registration"]["document_identity_evidence"]
            self.assertEqual(set(bundle), {"identity-B.json", "identity-C.json"})
            for path, encoded in bundle.items():
                self.assertEqual(base64.b64decode(encoded, validate=True), (self.base / path).read_bytes())
        replay = batch_store.replay_document_group(ROOT, self.registry_path, "doc1-c-panel", self.data)
        self.assertEqual(replay["resolved"]["context"].store_id, "C")
        self.assertEqual(batch_store._json(replay["preview"]), batch_store._json(result["batches"][2]["preview"]))

    def test_exact_retry_is_complete_and_idempotent(self):
        first = self.receive()
        second = self.receive()
        self.assertTrue(second["idempotent"])
        self.assertEqual([item["batch_id"] for item in first["batches"]],
                         [item["batch_id"] for item in second["batches"]])
        self.assertEqual(self.validated_count(), 3)

    def test_concurrent_identical_document_has_one_complete_intake(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.receive(), range(2)))
        self.assertEqual(sorted(result["idempotent"] for result in results), [False, True])
        self.assertTrue(all(result["status"] == "validated" for result in results))
        self.assertEqual(self.validated_count(), 3)

    def test_unknown_line_holds_complete_document_and_preserves_source_for_review(self):
        data = self.data + "未知后台字段99\r\n".encode()
        for receipt in self.registry["uploads"]:
            receipt["file_sha256"] = sha(data)
        self.save()
        result = self.receive(data=data)
        self.assertEqual(result["status"], "quarantined")
        self.assertEqual(result["batches"], [])
        self.assertEqual(self.validated_count(), 0)
        self.assertEqual(batch_store.read_batch_artifacts(self.database, result["quarantine_batch_id"])["data"], data)
        self.assertTrue(self.receive(data=data)["idempotent"])

    def test_missing_receipt_does_not_accept_other_valid_groups(self):
        self.registry["uploads"].pop()
        self.save()
        result = self.receive()
        self.assertEqual(result["status"], "quarantined")
        self.assertIn("every parsed source group", " ".join(result["errors"]))
        self.assertEqual(self.validated_count(), 0)

    def test_receipt_omission_after_success_cannot_partially_retry(self):
        first = self.receive()
        self.assertEqual(first["status"], "validated")
        self.registry["uploads"].pop()
        self.save()
        result = self.receive()
        self.assertEqual(result["status"], "quarantined")
        self.assertEqual(result["batches"], [])
        self.assertEqual(self.validated_count(), 3)

    def test_extra_or_duplicate_selection_is_rejected(self):
        for change in ("extra", "duplicate"):
            with self.subTest(change=change):
                receipt = copy.deepcopy(self.registry["uploads"][0])
                receipt["upload_id"] = "new-" + change
                if change == "extra":
                    receipt["source_block_line"] = 100
                self.registry["uploads"].append(receipt)
                self.save()
                self.assertEqual(self.receive()["status"], "quarantined")
                self.assertEqual(self.validated_count(), 0)
                self.registry["uploads"].pop()

    def test_text_alias_does_not_override_reviewed_store_or_window(self):
        first = self.registry["uploads"][0]
        for key, value in (("binding_id", "binding-C"), ("period_start", "2026-04-01")):
            with self.subTest(key=key):
                original = first[key]
                first[key] = value
                self.save()
                self.assertEqual(self.receive()["status"], "quarantined")
                self.assertEqual(self.validated_count(), 0)
                first[key] = original

    def test_independent_identity_mismatch_holds_whole_document(self):
        (self.base / "identity-C.json").write_text("{}")
        result = self.receive()
        self.assertEqual(result["status"], "quarantined")
        self.assertEqual(self.validated_count(), 0)

    def test_excluded_source_and_invalid_utf8_never_create_database(self):
        for data in (self.data + "未知内容\n无效订单数999\n".encode(), b"\xff"):
            with self.subTest(data=data[-12:]):
                with self.assertRaises(ValueError):
                    self.receive(data=data)
                self.assertFalse(self.database.exists())

    def test_single_csv_receive_cannot_validate_manual_child(self):
        with self.assertRaises(intake_registry.DocumentIntakeRequired):
            batch_store.receive_batch(ROOT, self.database, self.registry_path, "doc1-b-panel", self.data)
        self.assertFalse(self.database.exists())

    def test_csv_boundary_rejects_excluded_text_prelude_but_preserves_sku_literal(self):
        original = "来源整理说明\n店铺：B\n时间范围2026.3\n有效订单数999\n".encode()
        with self.assertRaisesRegex(ValueError, "excluded order"):
            batch_store.receive_batch(ROOT, self.database, self.registry_path, "unknown", original)
        self.assertFalse(self.database.exists())
        data = ("store_id,period_start,period_end,period_month,sku_rank,sku_name,sales_volume\n"
                "B,2026-03-01,2026-03-31,2026-03,1,有效订单数说明商品,3\n"
                "B,2026-03-01,2026-03-31,2026-03,2,商品乙,2\n"
                "B,2026-03-01,2026-03-31,2026-03,3,商品丙,1\n").encode()
        receipt = copy.deepcopy(self.registry["uploads"][1])
        for field in ("document_id", "source_block_line"):
            del receipt[field]
        receipt.update(upload_id="csv-literal", mapping_version="canonical_csv_v1", file_sha256=sha(data))
        self.registry["uploads"].append(receipt)
        self.save()
        result = batch_store.receive_batch(ROOT, self.database, self.registry_path, "csv-literal", data)
        self.assertEqual(result["status"], "validated", result["errors"])
        self.assertEqual(result["preview"]["validated_records"][0]["record"]["sku_name"], "有效订单数说明商品")

    def test_changed_document_bytes_need_new_document_id(self):
        self.assertEqual(self.receive()["status"], "validated")
        data = self.data.replace(b"12.3400", b"99")
        for receipt in self.registry["uploads"]:
            receipt["file_sha256"] = sha(data)
        self.save()
        result = self.receive(data=data)
        self.assertEqual(result["status"], "quarantined")
        self.assertEqual(self.validated_count(), 3)

    def test_explicit_complete_revision_retains_missing_new_value(self):
        first = self.receive()
        data = self.data.replace(b"12.3400", b"")
        self.add_document("doc2", data)
        supersedes = {child["upload_id"].replace("doc1", "doc2"): child["batch_id"] for child in first["batches"]}
        result = self.receive("doc2", data, supersedes=supersedes)
        self.assertEqual(result["status"], "validated", result["errors"])
        self.assertIsNone(result["batches"][0]["preview"]["validated_records"][0]["record"]["transaction_amount"])
        self.assertEqual(self.validated_count(), 6)

    def test_one_missing_predecessor_rolls_back_other_children(self):
        first = self.receive()
        data = self.data.replace(b"12.3400", b"20")
        self.add_document("doc2", data)
        supersedes = {child["upload_id"].replace("doc1", "doc2"): child["batch_id"] for child in first["batches"]}
        supersedes.pop("doc2-c-panel")
        result = self.receive("doc2", data, supersedes=supersedes)
        self.assertEqual(result["status"], "quarantined")
        self.assertEqual(result["batches"], [])
        self.assertEqual(self.validated_count(), 3)
        supersedes["doc2-c-panel"] = first["batches"][2]["batch_id"]
        self.assertEqual(self.receive("doc2", data, supersedes=supersedes)["status"], "validated")

    def test_unexpected_failure_rolls_back_every_insert(self):
        original = batch_store._store_one
        calls = []
        def fail_second(*args):
            calls.append(True)
            if len(calls) == 2:
                raise OSError("synthetic disk failure")
            return original(*args)
        with patch.object(batch_store, "_store_one", side_effect=fail_second):
            with self.assertRaisesRegex(OSError, "synthetic disk"):
                self.receive()
        if self.database.exists():
            with closing(sqlite3.connect(self.database)) as connection:
                tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                self.assertEqual(tables, [])
        self.assertEqual(self.receive()["status"], "validated")

    def test_manual_v3_daily_window_and_reviewed_scope(self):
        data = self.data.replace(b"2026.3", b"2026-03-01\xe8\x87\xb32026-03-01")
        for receipt in self.registry["uploads"]:
            receipt.update(mapping_version="manual_text_v3", period_end="2026-03-01", file_sha256=sha(data),
                aggregation_scope={"scope_version": "1", "timezone": "Asia/Shanghai",
                                   "selection_conditions": "synthetic all categories", "reviewed_by": "test-operator"})
        self.save()
        result = self.receive(data=data)
        self.assertEqual(result["status"], "validated", result["errors"])
        self.assertIsNone(result["batches"][0]["preview"]["validated_records"][0]["record"]["period_month"])

    def test_manual_v2_cannot_be_relabelled_as_daily_source(self):
        for receipt in self.registry["uploads"]:
            receipt["period_end"] = "2026-03-01"
        self.save()
        self.assertEqual(self.receive()["status"], "quarantined")
        self.assertEqual(self.validated_count(), 0)

    def test_tampered_original_or_control_metadata_is_detected(self):
        result = self.receive()
        child = result["batches"][0]
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("UPDATE batches SET raw_data=? WHERE batch_id=?", (b"altered", child["batch_id"]))
            connection.commit()
        with self.assertRaisesRegex(ValueError, "source bytes"):
            batch_store.read_batch(self.database, child["batch_id"])
        with self.assertRaisesRegex(ValueError, "source bytes"):
            self.receive()

    def test_schema_and_runtime_reject_text_selector_on_csv(self):
        schema = json.loads((ROOT / "retail_ops/contracts/intake_registry.v3.schema.json").read_bytes())
        validator = Draft202012Validator(schema)
        validator.validate(self.registry)
        receipt = self.registry["uploads"][0]
        receipt["mapping_version"] = "canonical_csv_v2"
        self.assertTrue(list(validator.iter_errors(self.registry)))
        with self.assertRaisesRegex(ValueError, "registered fields"):
            intake_registry.validate_upload_window(receipt, "3")

    def test_cli_receive_document_and_show_original_child(self):
        source = self.base / "source.txt"
        source.write_bytes(self.data)
        command = [sys.executable, "-m", "retail_ops.ingestion.batch_cli", "receive-document",
                   "--database", str(self.database), "--registry", str(self.registry_path),
                   "--document-id", "doc1", "--input", str(source)]
        run = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr + run.stdout)
        result = json.loads(run.stdout)
        self.assertEqual(len(result["batches"]), 3)
        shown = subprocess.run([sys.executable, "-m", "retail_ops.ingestion.batch_cli", "show", "--database",
                                str(self.database), "--batch-id", result["batches"][0]["batch_id"]],
                               cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertEqual(json.loads(shown.stdout)["file_sha256"], sha(self.data))

    def test_cli_unknown_group_exits_two_without_partial_intake(self):
        source = self.base / "source.txt"
        source.write_bytes(self.data)
        self.registry["uploads"].pop()
        self.save()
        run = subprocess.run([sys.executable, "-m", "retail_ops.ingestion.batch_cli", "receive-document",
            "--database", str(self.database), "--registry", str(self.registry_path),
            "--document-id", "doc1", "--input", str(source)], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(run.returncode, 2, run.stderr)
        self.assertEqual(json.loads(run.stdout)["status"], "quarantined")
        self.assertEqual(self.validated_count(), 0)


if __name__ == "__main__":
    unittest.main()
