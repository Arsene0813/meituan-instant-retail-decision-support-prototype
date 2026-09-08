"""Persistence regressions use synthetic receipts and temporary databases only."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from retail_ops.ingestion.batch_store import receive_batch, read_batch, read_batch_artifacts

ROOT = Path(__file__).resolve().parents[1]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def csv_data(amount="12.3400", *, store="B", month="03", orders="0"):
    days = "31" if month == "03" else "30"
    return ("store_id,period_start,period_end,period_month,transaction_amount,transaction_orders,entry_users\n"
            f"{store},2026-{month}-01,2026-{month}-{days},2026-{month},{amount},{orders},\n").encode()


class RetailBatchStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.db = self.base / "intake.sqlite3"
        self.registry_path = self.base / "registry.json"
        identity = {"source_system": "meituan_merchant_backend", "source_account_id": "synthetic-account",
                    "source_store_id": "synthetic-store-b"}
        identity_bytes = json.dumps(identity).encode()
        (self.base / "identity.json").write_bytes(identity_bytes)
        self.registry = {"registry_version": "1", "bindings": [{
            **identity, "binding_id": "synthetic-binding-b", "store_id": "B",
            "dataset_ids": ["demo2_store_period_metrics"], "identity_evidence_path": "identity.json",
            "identity_evidence_sha256": sha(identity_bytes), "reviewed_by": "synthetic-test-operator",
        }], "uploads": []}
        self.data = csv_data()
        self.register("u1", self.data)

    def save_registry(self):
        self.registry_path.write_text(json.dumps(self.registry), encoding="utf-8")

    def register(self, upload_id, data, *, month="03", extracted_at="2026-05-01T00:00:00+00:00"):
        self.registry["uploads"].append({
            "upload_id": upload_id, "binding_id": "synthetic-binding-b", "dataset_id": "demo2_store_period_metrics",
            "period_start": f"2026-{month}-01", "period_end": f"2026-{month}-{'31' if month == '03' else '30'}",
            "file_sha256": sha(data), "source_page": "synthetic-fixture", "extracted_at": extracted_at,
            "mapping_version": "canonical_csv_v1",
        })
        self.save_registry()

    def receive(self, upload_id="u1", data=None, **kwargs):
        return receive_batch(ROOT, self.db, self.registry_path, upload_id,
                             self.data if data is None else data, **kwargs)

    def test_round_trip_preserves_raw_null_zero_and_decimal_text(self):
        result = self.receive()
        self.assertEqual(result["status"], "validated")
        record = result["preview"]["validated_records"][0]["record"]
        self.assertEqual(record["transaction_amount"], "12.3400")
        self.assertEqual(record["transaction_orders"], 0)
        self.assertIsNone(record["entry_users"])
        stored = read_batch(self.db, result["batch_id"])
        self.assertEqual(stored["metadata"]["file_sha256"], sha(self.data))
        artifacts = read_batch_artifacts(self.db, result["batch_id"])
        self.assertEqual(artifacts["data"], self.data)
        self.assertEqual(artifacts["registry_bytes"], self.registry_path.read_bytes())

    def test_retry_survives_unrelated_registration_append(self):
        first = self.receive()
        original_registry = self.registry_path.read_bytes()
        self.register("unrelated-april", csv_data(month="04"), month="04")
        repeated = self.receive()
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(first["batch_id"], repeated["batch_id"])
        self.assertEqual(read_batch_artifacts(self.db, first["batch_id"])["registry_bytes"], original_registry)

    def test_new_same_scope_snapshot_requires_explicit_predecessor(self):
        first = self.receive()
        data2 = csv_data("", orders="2")
        self.register("u2", data2)
        held = self.receive("u2", data2)
        self.assertEqual(held["status"], "quarantined")
        revision = self.receive("u2", data2, supersedes_batch_id=first["batch_id"])
        self.assertEqual(revision["status"], "validated")
        self.assertIsNone(revision["preview"]["validated_records"][0]["record"]["transaction_amount"])
        self.assertEqual(read_batch(self.db, first["batch_id"])["status"], "validated")
        self.assertEqual(read_batch_artifacts(self.db, first["batch_id"])["data"], self.data)
        self.assertEqual(read_batch_artifacts(self.db, revision["batch_id"])["data"], data2)

    def test_predecessor_cannot_acquire_two_validated_successors(self):
        first = self.receive()
        for upload_id, amount in (("u2", "20"), ("u3", "30")):
            self.register(upload_id, csv_data(amount))
        second = self.receive("u2", csv_data("20"), supersedes_batch_id=first["batch_id"])
        third = self.receive("u3", csv_data("30"), supersedes_batch_id=first["batch_id"])
        self.assertEqual(second["status"], "validated")
        self.assertEqual(third["status"], "quarantined")

    def test_revision_cannot_change_scope_or_move_extraction_backwards(self):
        first = self.receive()
        april = csv_data(month="04")
        self.register("april", april, month="04")
        self.assertEqual(self.receive("april", april, supersedes_batch_id=first["batch_id"])["status"], "quarantined")
        old = csv_data("9")
        self.register("old", old, extracted_at="2026-04-01T00:00:00+00:00")
        self.assertEqual(self.receive("old", old, supersedes_batch_id=first["batch_id"])["status"], "quarantined")

    def test_unknown_registration_retains_source_but_cannot_validate(self):
        result = self.receive("unknown")
        self.assertEqual(result["status"], "quarantined")
        self.assertIsNone(result["metadata"])
        self.assertEqual(read_batch_artifacts(self.db, result["batch_id"])["data"], self.data)

    def test_missing_registration_can_be_supplied_for_exact_source_later(self):
        held = self.receive("later")
        self.register("later", self.data)
        accepted = self.receive("later")
        self.assertEqual(held["status"], "quarantined")
        self.assertEqual(accepted["status"], "validated")
        self.assertNotEqual(held["batch_id"], accepted["batch_id"])

    def test_changed_upload_id_payload_is_held_after_registry_edit(self):
        first = self.receive()
        changed = csv_data("999")
        self.registry["uploads"][0]["file_sha256"] = sha(changed)
        self.save_registry()
        result = self.receive(data=changed, supersedes_batch_id=first["batch_id"])
        self.assertEqual(result["status"], "quarantined")
        self.assertTrue(any("upload_id" in error for error in result["errors"]))

    def test_unauthorized_wrong_bytes_do_not_reserve_a_reviewed_upload_id(self):
        bad = self.receive(data=csv_data("999"))
        self.assertEqual(bad["status"], "quarantined")
        good = self.receive()
        self.assertEqual(good["status"], "validated")

    def test_unregistered_wrong_bytes_do_not_reserve_a_future_upload_id(self):
        bad = self.receive("later", csv_data("999"))
        self.assertEqual(bad["status"], "quarantined")
        self.register("later", self.data)
        self.assertEqual(self.receive("later")["status"], "validated")

    def test_excluded_proposal_fields_are_not_persisted(self):
        with self.assertRaisesRegex(ValueError, "Excluded"):
            self.receive(proposals=[{"record": {"有效订单数": 987654321}}])
        self.assertFalse(self.db.exists())

    def test_rejected_registry_symlink_is_not_archived(self):
        target = self.base / "unreviewed.json"
        target.write_text("unreviewed identity", encoding="utf-8")
        self.registry_path.unlink()
        self.registry_path.symlink_to(target)
        result = self.receive()
        self.assertEqual(result["status"], "quarantined")
        self.assertIsNone(read_batch_artifacts(self.db, result["batch_id"])["registry_bytes"])

    def test_wrong_store_and_model_overwrite_are_quarantined(self):
        wrong = csv_data(store="C")
        self.register("wrong-store", wrong)
        held = self.receive("wrong-store", wrong)
        self.assertEqual(held["status"], "quarantined")
        self.assertEqual(held["preview"]["validated_records"], [])
        proposal = [{"dataset_id": "demo2_store_period_metrics", "grain": "store_period", "ranking_basis": None,
                     "record": {"store_id": "B", "period_start": "2026-03-01", "period_end": "2026-03-31",
                                "period_month": "2026-03", "transaction_amount": "999", "transaction_orders": 0}}]
        result = self.receive(proposals=proposal)
        self.assertEqual(result["status"], "quarantined")
        self.assertEqual(read_batch(self.db, result["batch_id"])["proposals"], proposal)

    def test_excluded_columns_are_rejected_before_creating_storage(self):
        data = b"store_id," + "有效订单数".encode() + b"\nB,987654321\n"
        for source in (data, data + b"\xff", b'store_id,"unterminated header\n'):
            with self.subTest(source=source):
                with self.assertRaises(ValueError):
                    self.receive(data=source)
                self.assertFalse(self.db.exists())

    def test_changed_lookup_columns_cannot_hide_a_batch_or_bypass_revision_checks(self):
        first = self.receive()
        self.register("u2", self.data)
        for column in ("scope_key", "upload_payload_key", "retry_key"):
            with self.subTest(column=column):
                with sqlite3.connect(self.db) as connection:
                    original = connection.execute(f"SELECT {column} FROM batches WHERE batch_id=?", (first["batch_id"],)).fetchone()[0]
                    connection.execute(f"UPDATE batches SET {column}=? WHERE batch_id=?", ("synthetic_corrupted_control", first["batch_id"]))
                with self.assertRaisesRegex(ValueError, "control field"):
                    read_batch(self.db, first["batch_id"])
                with self.assertRaisesRegex(ValueError, "control field"):
                    self.receive("u2")
                with sqlite3.connect(self.db) as connection:
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM batches").fetchone()[0], 1)
                    connection.execute(f"UPDATE batches SET {column}=? WHERE batch_id=?", (original, first["batch_id"]))
        self.assertEqual(read_batch(self.db, first["batch_id"])["status"], "validated")

    def test_corrupt_archived_bytes_are_detected_on_read_and_retry(self):
        result = self.receive()
        with sqlite3.connect(self.db) as connection:
            connection.execute("UPDATE batches SET raw_data=? WHERE batch_id=?", (b"corrupt", result["batch_id"]))
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            read_batch(self.db, result["batch_id"])
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            self.receive()

    def test_concurrent_exact_retries_create_one_batch(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.receive(), range(4)))
        self.assertEqual(len({result["batch_id"] for result in results}), 1)
        self.assertEqual(sum(not result["idempotent"] for result in results), 1)
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM batches").fetchone()[0], 1)

    def test_concurrent_revisions_cannot_fork_a_predecessor(self):
        first = self.receive()
        self.register("u2", csv_data("20"))
        self.register("u3", csv_data("30"))
        def receive_revision(item):
            upload_id, amount = item
            return self.receive(upload_id, csv_data(amount), supersedes_batch_id=first["batch_id"])
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(receive_revision, [("u2", "20"), ("u3", "30")]))
        self.assertEqual(sorted(result["status"] for result in results), ["quarantined", "validated"])

    def test_unrelated_sqlite_database_is_not_modified(self):
        with sqlite3.connect(self.db) as connection:
            connection.execute("CREATE TABLE unrelated(value TEXT)")
            connection.execute("INSERT INTO unrelated VALUES ('retain')")
        with self.assertRaisesRegex(ValueError, "not a supported"):
            self.receive()
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(connection.execute("SELECT * FROM unrelated").fetchall(), [("retain",)])
            self.assertEqual(connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [("unrelated",)])


if __name__ == "__main__":
    unittest.main()
