"""Publication consumers share synthetic batches, never repository business CSVs."""
from __future__ import annotations

import calendar
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
from retail_ops.ingestion import publication_cli


ROOT = Path(__file__).resolve().parents[1]
COMPARISON = "Are Stores B-F directly comparable in March 2026?"
CAUSAL = "Can Store A's March-to-April increases in transaction amount and transaction orders be attributed to search exposure alone?"


def sha(data):
    return hashlib.sha256(data).hexdigest()


class RetailPublicationAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.db = self.base / "batches.sqlite3"
        self.directory = self.base / "publications"
        self.registry_path = self.base / "registry.json"
        self.registry = {"registry_version": "1", "bindings": [], "uploads": []}
        self.count = 0

    def receive(self, store="B", month=3, dataset="demo2_store_period_metrics", *,
                values=None, ranked=None, predecessor=None):
        self.count += 1
        binding_id = "synthetic-binding-" + store
        if not any(item["binding_id"] == binding_id for item in self.registry["bindings"]):
            identity = {"source_system": "meituan_merchant_backend",
                        "source_account_id": "synthetic-analysis-account",
                        "source_store_id": "synthetic-analysis-store-" + store}
            identity_data = json.dumps(identity).encode()
            filename = "identity-" + store + ".json"
            (self.base / filename).write_bytes(identity_data)
            self.registry["bindings"].append({**identity, "binding_id": binding_id, "store_id": store,
                "dataset_ids": ["store_a_monthly_metrics", "demo2_store_period_metrics",
                                "store_period_panel_metrics", "demo2_top_skus_by_transaction_amount"],
                "identity_evidence_path": filename, "identity_evidence_sha256": sha(identity_data),
                "reviewed_by": "synthetic-analysis-operator"})
        window = {"store_id": store, "period_month": f"2026-{month:02}",
                  "period_start": f"2026-{month:02}-01",
                  "period_end": f"2026-{month:02}-{calendar.monthrange(2026, month)[1]}"}
        if ranked is None:
            metrics = {"transaction_amount": "100", "transaction_orders": "10",
                       "activity_orders": "5", "search_exposure_users": "100",
                       "search_entry_users": "10", "entry_users": "20", "exposure_users": "200"}
            metrics.update(values or {})
            rows = [{**window, **metrics}]
        else:
            rows = [{**window, **item} for item in ranked]
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        data = buffer.getvalue().encode()
        upload_id = "synthetic-analysis-upload-" + str(self.count)
        self.registry["uploads"].append({"upload_id": upload_id, "binding_id": binding_id,
            "dataset_id": dataset, "period_start": window["period_start"], "period_end": window["period_end"],
            "file_sha256": sha(data), "source_page": "synthetic-analysis-source",
            "extracted_at": "2026-05-01T00:00:00+00:00", "mapping_version": "canonical_csv_v1"})
        self.registry_path.write_text(json.dumps(self.registry), encoding="utf-8")
        batch = receive_batch(ROOT, self.db, self.registry_path, upload_id, data,
                              supersedes_batch_id=predecessor)
        self.assertEqual(batch["status"], "validated", batch["errors"])
        return batch["batch_id"]

    def publish(self, ids):
        return publish(ROOT, self.db, self.directory, ids)["publication_id"]

    def analyze(self, publication_id, question=COMPARISON, **kwargs):
        return publication_cli.analyze_publication(ROOT, self.directory, publication_id, question, **kwargs)

    @staticmethod
    def sql_row(result, store="B", month="2026-03"):
        sql = result["sql"]
        return next(row for row in (dict(zip(sql["columns"], cells)) for cells in sql["rows"])
                    if row["store_id"] == store and row["period_month"] == month)

    @staticmethod
    def fact(result, slot="transaction_conversion_profile", store="B", month="2026-03"):
        return next(row for row in result["facts"] if row["entity_id"] == "store_" + store
                    and row["period_label"] == month and row["slot"] == slot)

    @staticmethod
    def check(result, check_id):
        return next(row for row in result["rac"]["evidence_review"]["checks"] if row["check_id"] == check_id)

    def bf_panel(self):
        return {(store, month): self.receive(store, month, dataset="store_period_panel_metrics")
                for store in "BCDEF" for month in (2, 3, 4)}

    def test_two_versions_keep_sql_facts_and_rac_on_selected_null_and_zero(self):
        ids = self.bf_panel()
        first = self.publish(list(ids.values()))
        old = self.analyze(first)
        old_amount = self.sql_row(old)["transaction_amount"]
        self.assertEqual(old_amount, 100)
        self.assertEqual(self.fact(old)["observed_values"]["transaction_amount"], old_amount)
        amount_check = self.check(old, "transaction_amount/transaction_amount")
        self.assertEqual(amount_check["status"], "supported")
        self.assertEqual(next(row["value"] for row in amount_check["operands"]
                              if row["row_key"]["store_id"] == "B"), "100.0")
        ids["B", 3] = self.receive("B", 3, dataset="store_period_panel_metrics",
            values={"transaction_amount": "", "transaction_orders": "0", "activity_orders": "0"},
            predecessor=ids["B", 3])
        second = self.publish(list(ids.values()))
        new = self.analyze(second)
        self.assertNotEqual(first, second)
        self.assertEqual(new["publication_id"], second)
        self.assertIsNone(self.sql_row(new)["transaction_amount"])
        self.assertEqual(self.sql_row(new)["transaction_orders"], 0)
        self.assertIsNone(self.fact(new)["observed_values"]["transaction_amount"])
        self.assertEqual(self.fact(new)["observed_values"]["transaction_orders"], 0)
        self.assertEqual(self.check(new, "transaction_amount/transaction_amount")["status"], "missing")
        orders = self.check(new, "order_volume/transaction_orders")
        self.assertEqual(orders["status"], "supported")
        self.assertEqual(next(row["value"] for row in orders["operands"]
                              if row["row_key"]["store_id"] == "B"), "0")
        self.assertEqual(self.analyze(first), old)

    def test_revision_replaces_whole_sku_list_and_does_not_restore_old_ranks(self):
        profile = self.receive()
        old_skus = self.receive(dataset="demo2_top_skus_by_transaction_amount", ranked=[
            {"sku_rank": str(rank), "sku_name": "Synthetic SKU " + str(rank),
             "sku_transaction_amount": str(rank * 10), "sales_volume": ""} for rank in (1, 2, 3)])
        old_id = self.publish([profile, old_skus])
        old = self.analyze(old_id)
        self.assertEqual(self.sql_row(old)["top3_sku_transaction_amount"], 60)
        new_skus = self.receive(dataset="demo2_top_skus_by_transaction_amount", predecessor=old_skus,
            ranked=[{"sku_rank": "1", "sku_name": "Synthetic replacement", "sku_transaction_amount": "0", "sales_volume": ""}])
        new = self.analyze(self.publish([profile, new_skus]))
        self.assertIsNone(self.sql_row(new)["top3_sku_transaction_amount"])
        with open_publication(ROOT, self.directory, new["publication_id"]) as pinned:
            with (pinned.root / "retail_ops/data/demo2_top_skus_by_transaction_amount.csv").open() as handle:
                rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sku_transaction_amount"], "0")
        self.assertEqual(rows[0]["sales_volume"], "")
        self.assertEqual(self.sql_row(self.analyze(old_id))["top3_sku_transaction_amount"], 60)

    def test_store_a_new_april_changes_sql_and_rac_growth_premise(self):
        march = self.receive("A", 3, "store_a_monthly_metrics")
        april = self.receive("A", 4, "store_a_monthly_metrics",
                             values={"transaction_amount": "200", "transaction_orders": "20"})
        first = self.publish([march, april])
        old = self.analyze(first, CAUSAL, query_name="01_store_a_month_over_month_diagnostic.sql")
        self.assertEqual(self.check(old, "transaction_amount/transaction_amount")["result"], "increased")
        new_april = self.receive("A", 4, "store_a_monthly_metrics", predecessor=april,
                                values={"transaction_amount": "1", "transaction_orders": "1"})
        second = self.publish([march, new_april])
        new = self.analyze(second, CAUSAL, query_name="01_store_a_month_over_month_diagnostic.sql")
        self.assertEqual(self.sql_row(new, "A", "2026-04")["transaction_amount"], 1)
        self.assertEqual(self.check(new, "transaction_amount/transaction_amount")["result"], "decreased")
        self.assertEqual(self.check(new, "transaction_orders/transaction_orders")["result"], "decreased")
        self.assertIn("transaction_amount decreased", new["rac"]["belief_update"]["claim"])
        self.assertTrue(new["rac"]["fact_check"]["unsupported_claims"])
        self.assertEqual(self.analyze(first, CAUSAL, query_name="01_store_a_month_over_month_diagnostic.sql"), old)

    def test_missing_stores_produce_invalid_rac_coverage_without_repository_fallback(self):
        result = self.analyze(self.publish([self.receive()]))
        self.assertEqual({fact["entity_id"] for fact in result["facts"]}, {"store_B"})
        self.assertEqual(self.check(result, "transaction_amount/transaction_amount")["status"], "invalid")
        packet = next(row for row in result["rac"]["grounded_evidence"]["resolved_packets"]
                      if row["factor_id"] == "transaction_amount")
        self.assertEqual(packet["grounding_status"], "record_contract_error")
        self.assertEqual(packet["evidence_values"], [])
        self.assertEqual(result["rac"]["belief_update"]["status"], "tentative")

    def test_analysis_never_reads_repository_business_sources_or_saved_outputs(self):
        publication_id = self.publish([self.receive()])
        original_open = Path.open
        def checked_open(path, *args, **kwargs):
            resolved = path.resolve()
            if (resolved.is_relative_to(ROOT / "retail_ops/outputs")
                    or resolved.is_relative_to(ROOT / "retail_ops/data") and resolved.suffix == ".csv"):
                raise AssertionError("Attempted repository data fallback: " + str(path))
            return original_open(path, *args, **kwargs)
        with patch.object(Path, "open", checked_open):
            result = self.analyze(publication_id)
        self.assertEqual(self.sql_row(result)["transaction_amount"], 100)

    def test_new_publication_during_analysis_does_not_change_pinned_run(self):
        first_batch = self.receive()
        first = self.publish([first_batch])
        real_query = publication_cli.run_query
        published = []
        def publish_during_query(query, *, root):
            if not published:
                second_batch = self.receive(values={"transaction_amount": "999"}, predecessor=first_batch)
                published.append(self.publish([second_batch]))
            return real_query(query, root=root)
        with patch.object(publication_cli, "open_publication", wraps=open_publication) as opener:
            with patch.object(publication_cli, "run_query", side_effect=publish_during_query):
                old = self.analyze(first)
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(old["publication_id"], first)
        self.assertEqual(self.sql_row(old)["transaction_amount"], 100)
        self.assertEqual(self.fact(old)["observed_values"]["transaction_amount"], 100)
        self.assertEqual(self.sql_row(self.analyze(published[0]))["transaction_amount"], 999)

    def test_unregistered_question_window_and_query_are_rejected(self):
        publication_id = self.publish([self.receive()])
        for question in ("Are Stores B-F directly comparable in April 2026?", "Are Stores B and C directly comparable in March 2026?"):
            with self.subTest(question=question), self.assertRaises(ValueError):
                self.analyze(publication_id, question)
        with self.assertRaisesRegex(ValueError, "query is not registered"):
            self.analyze(publication_id, query_name="unregistered.sql")

    def command(self, *args):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        return subprocess.run([sys.executable, "-m", "retail_ops.ingestion.publication_cli", *map(str, args)],
                              cwd=self.base, env=env, text=True, capture_output=True, timeout=30)

    def test_cli_publish_inspect_and_analyze_save_same_publication_id(self):
        batch = self.receive()
        selection = self.base / "selection.json"
        selection.write_text(json.dumps({"batch_ids": [batch]}))
        created = self.command("publish", "--database", self.db, "--directory", self.directory,
                               "--selection", selection)
        self.assertEqual(created.returncode, 0, created.stderr)
        publication_id = json.loads(created.stdout)["publication_id"]
        inspected = self.command("inspect", "--directory", self.directory, "--publication-id", publication_id)
        self.assertEqual(inspected.returncode, 0, inspected.stderr)
        self.assertEqual(json.loads(inspected.stdout)["publication_id"], publication_id)
        output = self.base / "analysis.json"
        analyzed = self.command("analyze", "--directory", self.directory, "--publication-id", publication_id,
                                "--question", COMPARISON, "--output", output)
        self.assertEqual(analyzed.returncode, 0, analyzed.stderr)
        self.assertEqual(json.loads(analyzed.stdout)["publication_id"], publication_id)
        self.assertEqual(json.loads(output.read_bytes())["publication_id"], publication_id)
        original = output.read_bytes()
        repeated = self.command("analyze", "--directory", self.directory, "--publication-id", publication_id,
                                "--question", COMPARISON, "--output", output)
        self.assertEqual(repeated.returncode, 2)
        self.assertEqual(output.read_bytes(), original)

    def test_cli_selection_requires_explicit_unique_batch_ids(self):
        selection = self.base / "selection.json"
        for text in ('{"batch_ids": []}', '{"latest": true}', '{"batch_ids": ["x", "x"]}',
                     '{"batch_ids": [1]}', '{"batch_ids": ["x"], "batch_ids": ["y"]}'):
            with self.subTest(text=text):
                selection.write_text(text)
                result = self.command("publish", "--database", self.db, "--directory", self.directory,
                                      "--selection", selection)
                self.assertEqual(result.returncode, 2)
        self.assertFalse(self.directory.exists())

    def test_cli_output_cannot_replace_a_publication_file(self):
        publication_id = self.publish([self.receive()])
        result = self.command("analyze", "--directory", self.directory, "--publication-id", publication_id,
                              "--question", COMPARISON, "--output", self.directory / "analysis.json")
        self.assertEqual(result.returncode, 2)
        self.assertIn("outside", result.stderr)
        self.assertFalse((self.directory / "analysis.json").exists())

    def test_analysis_reconciles_saved_diagnostics_and_fact_values(self):
        publication_id = self.publish([self.receive()])
        real_query = publication_cli.run_query
        def stale_query(query, *, root):
            columns, rows = real_query(query, root=root)
            changed = [list(row) for row in rows]
            changed[0][columns.index("transaction_amount")] = 777
            return columns, changed
        with patch.object(publication_cli, "run_query", side_effect=stale_query):
            with self.assertRaisesRegex(ValueError, "published SQL differs"):
                self.analyze(publication_id)
        with patch.object(publication_cli, "build_facts", return_value=[]):
            with self.assertRaisesRegex(ValueError, "Published facts differ"):
                self.analyze(publication_id)


if __name__ == "__main__":
    unittest.main()
