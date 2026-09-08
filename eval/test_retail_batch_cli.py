"""Process-level intake CLI checks using synthetic external source files."""

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class RetailBatchCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.database = self.directory / "batches.sqlite3"
        self.input_path = self.directory / "source.csv"
        self.data = (
            "store_id,period_month,period_start,period_end,transaction_amount,transaction_orders\n"
            "A,2026-03,2026-03-01,2026-03-31,,0\n"
        ).encode()
        self.input_path.write_bytes(self.data)
        identity = {
            "source_system": "meituan_merchant_backend",
            "source_account_id": "synthetic_cli_account",
            "source_store_id": "synthetic_cli_source_store",
        }
        identity_bytes = json.dumps(identity, sort_keys=True).encode()
        (self.directory / "identity.json").write_bytes(identity_bytes)
        self.registry_path = self.directory / "registry.json"
        self.registry_path.write_text(json.dumps({
            "registry_version": "1",
            "bindings": [{
                "binding_id": "synthetic_cli_binding", **identity, "store_id": "A",
                "dataset_ids": ["store_a_monthly_metrics"],
                "identity_evidence_path": "identity.json",
                "identity_evidence_sha256": hashlib.sha256(identity_bytes).hexdigest(),
                "reviewed_by": "synthetic_cli_operator",
            }],
            "uploads": [{
                "upload_id": "synthetic_cli_upload", "binding_id": "synthetic_cli_binding",
                "dataset_id": "store_a_monthly_metrics", "period_start": "2026-03-01",
                "period_end": "2026-03-31", "file_sha256": hashlib.sha256(self.data).hexdigest(),
                "source_page": "synthetic_cli_source_page", "extracted_at": "2026-05-01T00:00:00+00:00",
                "mapping_version": "canonical_csv_v1",
            }],
        }, indent=2))

    def command(self, *arguments):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        return subprocess.run(
            [sys.executable, "-m", "retail_ops.ingestion.batch_cli", *map(str, arguments)],
            cwd=self.directory, env=env, text=True, capture_output=True, timeout=30,
        )

    def receive(self, *, database=None, upload_id="synthetic_cli_upload", extra=()):
        # Relative external paths deliberately exercise the same path behavior
        # available to a terminal user outside the repository directory.
        return self.command("receive", "--database", database or self.database,
                            "--registry", "registry.json", "--upload-id", upload_id,
                            "--input", "source.csv", *extra)

    def test_receive_retry_and_show_preserve_zero_blank_source_and_batch_identity(self):
        first = self.receive()
        self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
        result = json.loads(first.stdout)
        self.assertEqual(result["status"], "validated")
        self.assertFalse(result["idempotent"])
        self.assertEqual(result["file_sha256"], hashlib.sha256(self.data).hexdigest())
        record = result["preview"]["validated_records"][0]["record"]
        self.assertIsNone(record["transaction_amount"])
        self.assertEqual(record["transaction_orders"], 0)
        self.assertEqual(result["metadata"]["source_page"], "synthetic_cli_source_page")
        retry = self.receive()
        self.assertEqual(retry.returncode, 0, retry.stderr + retry.stdout)
        retried = json.loads(retry.stdout)
        self.assertEqual(retried["batch_id"], result["batch_id"])
        self.assertTrue(retried["idempotent"])
        shown = self.command("show", "--database", self.database, "--batch-id", result["batch_id"])
        self.assertEqual(shown.returncode, 0, shown.stderr + shown.stdout)
        stored = json.loads(shown.stdout)
        for key in ("batch_id", "status", "file_sha256", "registry_sha256", "metadata", "preview"):
            self.assertEqual(stored[key], result[key], key)
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM batches").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT raw_data FROM batches").fetchone()[0], self.data)

    def test_unknown_upload_is_quarantined_with_raw_bytes_and_available_registry_snapshot(self):
        received = self.receive(upload_id="unregistered_cli_upload")
        self.assertEqual(received.returncode, 2, received.stderr + received.stdout)
        result = json.loads(received.stdout)
        self.assertEqual(result["status"], "quarantined")
        self.assertIsNone(result["metadata"])
        self.assertIsNone(result["registration"])
        self.assertTrue(result["errors"])
        with sqlite3.connect(self.database) as connection:
            raw, registration = connection.execute("SELECT raw_data,registry_bytes FROM batches").fetchone()
        self.assertEqual(raw, self.data)
        self.assertEqual(registration, self.registry_path.read_bytes())
        shown = self.command("show", "--database", self.database, "--batch-id", result["batch_id"])
        self.assertEqual(shown.returncode, 2, shown.stderr + shown.stdout)
        self.assertEqual(json.loads(shown.stdout)["status"], "quarantined")

    def test_database_inside_the_repository_is_rejected_before_file_creation(self):
        with tempfile.TemporaryDirectory(prefix="intake-cli-guard-", dir=ROOT) as directory:
            database = Path(directory) / "must_not_be_created.sqlite3"
            result = self.receive(database=database)
            self.assertEqual(result.returncode, 2)
            self.assertIn("outside the repository", result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertFalse(database.exists())

    def test_invalid_proposals_json_and_duplicate_keys_fail_before_creating_database(self):
        proposals = self.directory / "proposals.json"
        for content in (b"[", b'[{"dataset_id":"a","dataset_id":"b"}]'):
            with self.subTest(content=content):
                proposals.write_bytes(content)
                result = self.receive(extra=("--proposals", proposals))
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("Cannot process batch", result.stderr)
                self.assertFalse(self.database.exists())
                self.assertEqual(self.input_path.read_bytes(), self.data)

    def test_show_detects_changed_archived_source_bytes(self):
        received = self.receive()
        self.assertEqual(received.returncode, 0, received.stderr + received.stdout)
        batch_id = json.loads(received.stdout)["batch_id"]
        with sqlite3.connect(self.database) as connection:
            connection.execute("UPDATE batches SET raw_data=? WHERE batch_id=?", (b"synthetic tampering", batch_id))
        result = self.command("show", "--database", self.database, "--batch-id", batch_id)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("SHA-256", result.stderr)


if __name__ == "__main__":
    unittest.main()
