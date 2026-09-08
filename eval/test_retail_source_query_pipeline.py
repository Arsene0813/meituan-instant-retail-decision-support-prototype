"""Real synthetic intake, immutable source publication and transient range queries."""
from __future__ import annotations

from contextlib import contextmanager
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from retail_ops.ingestion.batch_store import receive_batch
from retail_ops.ingestion.publication import open_publication, publish
from retail_ops.ingestion import source_query


ROOT = Path(__file__).resolve().parents[1]
DATASET = "store_period_panel_metrics"
DEFAULT_SCOPE = {
    "scope_version": "1", "timezone": "Asia/Shanghai",
    "selection_conditions": "Synthetic all channels, all order statuses and all products; no non-date filters.",
    "reviewed_by": "synthetic-query-operator",
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


class RetailSourceQueryPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.db = self.base / "batches.sqlite3"
        self.directory = self.base / "publications"
        self.registry_path = self.base / "registry.json"
        self.registry = {"registry_version": "2", "bindings": [], "uploads": []}
        self.count = 0

    def receive(self, store="B", start="2026-03-01", end=None, *,
                values=None, predecessor=None, dataset=DATASET, scope=DEFAULT_SCOPE):
        end = end or start
        self.count += 1
        binding_id = "synthetic-query-binding-" + store
        if not any(row["binding_id"] == binding_id for row in self.registry["bindings"]):
            identity = {"source_system": "meituan_merchant_backend",
                        "source_account_id": "synthetic-query-account",
                        "source_store_id": "synthetic-query-store-" + store}
            identity_data = json.dumps(identity).encode()
            filename = "identity-" + store + ".json"
            (self.base / filename).write_bytes(identity_data)
            self.registry["bindings"].append({**identity, "binding_id": binding_id,
                "store_id": store, "dataset_ids": [DATASET, "demo2_store_period_metrics"],
                "identity_evidence_path": filename, "identity_evidence_sha256": sha(identity_data),
                "reviewed_by": "synthetic-query-operator"})
        month = start[:7] if (start, end) == source_query.month_dates(start[:7]) else ""
        metrics = {"transaction_amount": "10", "transaction_orders": "1", "entry_users": "8"}
        metrics.update(values or {})
        record = {"store_id": store, "period_start": start, "period_end": end,
                  "period_month": month, **metrics}
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=list(record), lineterminator="\n")
        writer.writeheader()
        writer.writerow(record)
        data = buffer.getvalue().encode()
        upload_id = "synthetic-query-upload-" + str(self.count)
        receipt = {"upload_id": upload_id, "binding_id": binding_id,
            "dataset_id": dataset, "period_start": start, "period_end": end,
            "file_sha256": sha(data), "source_page": "synthetic-query-source",
            "extracted_at": "2026-04-01T00:00:00+00:00", "mapping_version": "canonical_csv_v2"}
        if scope is not None:
            receipt["aggregation_scope"] = dict(scope)
        self.registry["uploads"].append(receipt)
        self.registry_path.write_text(json.dumps(self.registry), encoding="utf-8")
        batch = receive_batch(ROOT, self.db, self.registry_path, upload_id, data,
                              supersedes_batch_id=predecessor)
        self.assertEqual(batch["status"], "validated", batch["errors"])
        return batch["batch_id"]

    def publish(self, batch_ids):
        return publish(ROOT, self.db, self.directory, batch_ids, source_records=True)["publication_id"]

    def query(self, publication_id, stores=None, start="2026-03-01", end="2026-03-02", fields=None):
        return source_query.query_publication(ROOT, self.directory, publication_id, DATASET,
            stores or ["B"], start, end, fields or ["transaction_amount", "transaction_orders"])

    @staticmethod
    def store(result, store="B"):
        return next(item for item in result["stores"] if item["store_id"] == store)

    def persistent_state(self):
        return {str(path.relative_to(self.base)): sha(path.read_bytes()) if path.is_file() else None
                for path in sorted(self.base.rglob("*"))}

    def command(self, module, *args):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run([sys.executable, "-m", "retail_ops.ingestion." + module, *map(str, args)],
            cwd=self.base, env=env, text=True, capture_output=True, timeout=30)

    def test_daily_cross_store_query_preserves_zero_missing_dates_and_source_lineage(self):
        b1 = self.receive()
        b2 = self.receive(start="2026-03-02", values={"transaction_amount": "0", "transaction_orders": "0"})
        c1 = self.receive("C", values={"transaction_amount": "99"})
        result = self.query(self.publish([b1, b2, c1]), ["C", "B"],
                            fields=["transaction_amount", "transaction_orders", "entry_users"])
        b, c = self.store(result), self.store(result, "C")
        self.assertEqual(b["metrics"]["transaction_amount"]["value"], "10")
        self.assertEqual(b["metrics"]["transaction_orders"]["value"], 1)
        self.assertEqual(b["metrics"]["transaction_amount"]["basis"], "sum_non_overlapping_days")
        self.assertEqual(b["coverage"]["missing_ranges"], [])
        self.assertIsNone(b["metrics"]["entry_users"]["value"])
        self.assertEqual(b["metrics"]["entry_users"]["reason"], "aggregation_not_registered")
        self.assertIsNone(c["metrics"]["transaction_amount"]["value"])
        self.assertEqual(c["metrics"]["transaction_amount"]["reason"], "missing_dates")
        self.assertEqual(c["coverage"]["missing_ranges"], [
            {"period_start": "2026-03-02", "period_end": "2026-03-02"}])
        self.assertEqual([row["record"]["transaction_amount"] for row in b["records"]], ["10", "0"])
        self.assertTrue(all(row["record"]["period_month"] is None for row in b["records"]))
        self.assertEqual({row["source"]["batch_id"] for row in b["records"]}, {b1, b2})
        self.assertTrue(all(row["source"]["source_line_end"] == 2 for row in b["records"]))

    def test_null_revision_updates_selected_snapshot_without_filling_from_predecessor(self):
        first_day = self.receive()
        second_day = self.receive(start="2026-03-02", values={"transaction_amount": "0"})
        first = self.publish([first_day, second_day])
        old = self.query(first)
        revised = self.receive(values={"transaction_amount": "", "transaction_orders": "0"},
                               predecessor=first_day)
        second = self.publish([revised, second_day])
        new = self.query(second)
        self.assertNotEqual(first, second)
        self.assertIsNone(self.store(new)["metrics"]["transaction_amount"]["value"])
        self.assertEqual(self.store(new)["metrics"]["transaction_amount"]["reason"], "missing_metric_values")
        self.assertEqual(self.store(new)["coverage"]["missing_ranges"], [])
        rows = self.store(new)["records"]
        self.assertIsNone(rows[0]["record"]["transaction_amount"])
        self.assertEqual(rows[0]["record"]["transaction_orders"], 0)
        self.assertEqual(rows[0]["source"]["batch_id"], revised)
        self.assertNotIn(first_day, {row["source"]["batch_id"] for row in rows})
        self.assertEqual(self.query(first), old)

    def test_daily_sources_with_unreviewed_scope_are_retained_but_not_summed(self):
        first_day = self.receive(scope=None)
        second_day = self.receive(start="2026-03-02", scope=None)
        result = self.store(self.query(self.publish([first_day, second_day])))
        self.assertEqual(result["coverage"]["missing_ranges"], [])
        self.assertEqual(len(result["records"]), 2)
        self.assertEqual([row["record"]["transaction_amount"] for row in result["records"]], ["10", "10"])
        for field in ("transaction_amount", "transaction_orders"):
            self.assertIsNone(result["metrics"][field]["value"])
            self.assertEqual(result["metrics"][field]["reason"], "aggregation_scope_unreviewed")

    def test_different_reviewed_non_date_filters_do_not_form_one_daily_total(self):
        first_day = self.receive()
        restricted = {**DEFAULT_SCOPE,
            "selection_conditions": "Synthetic all channels and products; completed orders only; no other non-date filters."}
        second_day = self.receive(start="2026-03-02", scope=restricted)
        result = self.store(self.query(self.publish([first_day, second_day])))
        self.assertEqual(result["coverage"]["missing_ranges"], [])
        self.assertEqual(len(result["records"]), 2)
        self.assertIn("different_source_scopes", result["ambiguities"])
        for field in ("transaction_amount", "transaction_orders"):
            self.assertIsNone(result["metrics"][field]["value"])
            self.assertEqual(result["metrics"][field]["reason"], "ambiguous_source_scope_or_window")

    def test_month_shortcut_and_explicit_dates_do_not_save_queries_or_modify_data(self):
        publication_id = self.publish([self.receive(end="2026-03-31", values={"transaction_amount": "310"})])
        before = self.persistent_state()
        arguments = ["--directory", self.directory, "--publication-id", publication_id,
                     "--dataset-id", DATASET, "--store-id", "B", "--field", "transaction_amount"]
        month = self.command("source_query", *arguments, "--month", "2026-03")
        explicit = self.command("source_query", *arguments, "--period-start", "2026-03-01",
                                "--period-end", "2026-03-31")
        self.assertEqual(month.returncode, 0, month.stderr)
        self.assertEqual(explicit.returncode, 0, explicit.stderr)
        self.assertEqual(json.loads(month.stdout), json.loads(explicit.stdout))
        self.assertEqual(self.persistent_state(), before)
        for _ in range(2):
            self.query(publication_id, end="2026-03-31")
        self.assertEqual(self.persistent_state(), before)

    def test_query_uses_published_sources_after_original_batch_database_is_removed(self):
        publication_id = self.publish([self.receive()])
        original = self.query(publication_id, end="2026-03-01")
        self.db.unlink()
        self.registry_path.unlink()
        (self.base / "identity-B.json").unlink()
        remaining = self.persistent_state()
        self.assertEqual(self.query(publication_id, end="2026-03-01"), original)
        self.assertEqual(self.persistent_state(), remaining)
        self.assertFalse(self.db.exists())

    def test_monthly_source_is_reported_for_its_window_and_never_prorated(self):
        publication_id = self.publish([self.receive(end="2026-03-31", values={"transaction_amount": "310"})])
        complete = self.store(self.query(publication_id, end="2026-03-31"))
        self.assertEqual(complete["metrics"]["transaction_amount"]["value"], "310")
        self.assertEqual(complete["metrics"]["transaction_amount"]["basis"], "reported_window")
        partial = self.store(self.query(publication_id, start="2026-03-10", end="2026-03-20"))
        self.assertIsNone(partial["metrics"]["transaction_amount"]["value"])
        self.assertEqual(partial["records"], [])
        self.assertEqual(len(partial["overlapping_records"]), 1)
        self.assertEqual(partial["overlapping_records"][0]["record"]["transaction_amount"], "310")
        self.assertIn("partially_overlapping_source_windows", partial["ambiguities"])

    def test_daily_input_requires_source_publication_before_existing_monthly_analysis(self):
        batch = self.receive()
        with self.assertRaisesRegex(ValueError, "Monthly analysis requires complete month"):
            publish(ROOT, self.db, self.directory, [batch])
        self.assertFalse(self.directory.exists())
        publication_id = self.publish([batch])
        result = self.query(publication_id, end="2026-03-01")
        self.assertEqual(self.store(result)["metrics"]["transaction_amount"]["value"], "10")

    def test_cli_source_publication_and_query_preserve_selected_batch(self):
        batch = self.receive()
        selection = self.base / "selection.json"
        selection.write_text(json.dumps({"batch_ids": [batch]}))
        result = self.command("publication_cli", "publish", "--database", self.db,
            "--directory", self.directory, "--selection", selection, "--source-records")
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads(result.stdout)
        self.assertEqual(manifest["summary"]["profile"], "source_records_v1")
        before = self.persistent_state()
        queried = self.command("source_query", "--directory", self.directory,
            "--publication-id", manifest["publication_id"], "--dataset-id", DATASET, "--store-id", "B",
            "--period-start", "2026-03-01", "--period-end", "2026-03-01", "--field", "transaction_amount")
        self.assertEqual(queried.returncode, 0, queried.stderr)
        body = json.loads(queried.stdout)
        self.assertEqual(body["publication_id"], manifest["publication_id"])
        self.assertEqual(self.store(body)["records"][0]["source"]["batch_id"], batch)
        self.assertEqual(self.persistent_state(), before)

    def test_query_never_reads_repository_business_csvs_or_saved_analysis_outputs(self):
        publication_id = self.publish([self.receive()])
        original_open = Path.open
        def checked_open(path, *args, **kwargs):
            resolved = path.resolve()
            if (resolved.is_relative_to(ROOT / "retail_ops/outputs") or
                    resolved.is_relative_to(ROOT / "retail_ops/data") and resolved.suffix == ".csv"):
                raise AssertionError("Attempted repository source fallback: " + str(path))
            return original_open(path, *args, **kwargs)
        with patch.object(Path, "open", checked_open):
            result = self.query(publication_id, ["B", "C"], end="2026-03-01")
        self.assertEqual(self.store(result)["metrics"]["transaction_amount"]["value"], "10")
        self.assertEqual(self.store(result, "C")["records"], [])
        self.assertIsNone(self.store(result, "C")["metrics"]["transaction_amount"]["value"])

    def test_publication_created_during_query_does_not_change_pinned_sources(self):
        batch = self.receive()
        first = self.publish([batch])
        published = []
        @contextmanager
        def publish_after_pinning(root, directory, publication_id):
            with open_publication(root, directory, publication_id) as pinned:
                replacement = self.receive(values={"transaction_amount": "999"}, predecessor=batch)
                published.append(self.publish([replacement]))
                yield pinned
        with patch.object(source_query, "open_publication", side_effect=publish_after_pinning) as opener:
            result = self.query(first, end="2026-03-01")
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(result["publication_id"], first)
        self.assertEqual(self.store(result)["metrics"]["transaction_amount"]["value"], "10")
        self.assertEqual(self.store(self.query(published[0], end="2026-03-01"))["metrics"]["transaction_amount"]["value"], "999")

    def test_daily_and_monthly_sources_are_preserved_without_double_counting(self):
        daily = self.receive()
        monthly = self.receive(end="2026-03-31", values={"transaction_amount": "310"})
        result = self.store(self.query(self.publish([daily, monthly]), end="2026-03-31"))
        self.assertEqual(len(result["records"]), 2)
        self.assertEqual({row["source"]["batch_id"] for row in result["records"]}, {daily, monthly})
        self.assertIn("overlapping_source_windows", result["ambiguities"])
        self.assertIsNone(result["metrics"]["transaction_amount"]["value"])
        self.assertEqual(result["metrics"]["transaction_amount"]["reason"], "ambiguous_source_scope_or_window")


if __name__ == "__main__":
    unittest.main()
