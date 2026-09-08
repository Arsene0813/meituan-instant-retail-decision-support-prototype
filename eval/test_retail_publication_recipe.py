"""Publication view routing, absence, and row lineage use synthetic inputs."""
import csv
from copy import deepcopy
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from retail_ops.ingestion import preview
from retail_ops.ingestion.publication_recipe import (
    CODE_FILES, STATIC_FILES, PROFILE_PATH, build_evidence_view, recipe_files,
)

ROOT = Path(__file__).resolve().parents[1]


class RetailPublicationRecipeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in STATIC_FILES:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, path)

    def batch(self, dataset, cells, batch_id="synthetic_batch"):
        start, end = "2026-03-01", "2026-03-31"
        raw = {"store_id": "B", "period_start": start, "period_end": end,
               "period_month": "2026-03", **cells}
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=list(raw), lineterminator="\n", quoting=csv.QUOTE_ALL)
        writer.writeheader(); writer.writerow(raw)
        data = buffer.getvalue().encode()
        grain, ranking = preview.ROUTES[dataset]
        result = preview.preview_csv(self.root, data, preview.UploadContext(dataset, "B", start, end, grain, ranking))
        self.assertEqual(result["status"], "validated")
        return {"result": {"batch_id": batch_id, "metadata": {"dataset_id": dataset},
                           "preview": json.loads(preview.preview_json(result))}}

    def records(self, name):
        with (self.root / f"retail_ops/data/{name}.csv").open(newline="") as handle:
            return list(csv.DictReader(handle))

    def test_explicit_panel_projection_keeps_null_zero_and_declares_missing_tables(self):
        selected = [self.batch("store_period_panel_metrics", {"transaction_amount": "", "transaction_orders": "0"})]
        summary = build_evidence_view(self.root, selected)
        for name in ("demo2_store_period_metrics", "store_period_panel_metrics"):
            rows = self.records(name)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["transaction_amount"], "")
            self.assertEqual(rows[0]["transaction_orders"], "0")
            self.assertEqual(summary["row_lineage"][name][0]["batch_id"], "synthetic_batch")
        self.assertEqual(summary["views"]["demo2_top_search_terms"]["row_count"], 0)
        self.assertTrue(summary["views"]["demo2_top_search_terms"]["missing_input"])
        self.assertEqual(self.records("demo2_top_search_terms"), [])
        facts = json.loads((self.root / summary["facts"]["output_path"]).read_text())
        transaction = next(fact for fact in facts if fact["slot"] == "transaction_conversion_profile")
        self.assertIsNone(transaction["observed_values"]["transaction_amount"])
        self.assertEqual(transaction["observed_values"]["transaction_orders"], 0)

    def test_multiline_sku_lineage_points_to_source_and_materialized_record_end(self):
        selected = [self.batch("demo2_top_skus_by_transaction_amount", {
            "sku_rank": "1", "sku_name": 'synthetic\r\n"sku"', "sku_transaction_amount": "0.00",
        })]
        source_line = selected[0]["result"]["preview"]["validated_records"][0]["source_line_end"]
        summary = build_evidence_view(self.root, selected)
        name = "demo2_top_skus_by_transaction_amount"
        self.assertEqual(self.records(name)[0]["sku_name"], 'synthetic\r\n"sku"')
        entry = summary["row_lineage"][name][0]
        self.assertEqual(entry["source_line_end"], source_line)
        self.assertEqual(entry["output_line_end"], 3)
        self.assertEqual(entry["key"]["sku_rank"], 1)

    def test_projection_cannot_join_competing_records_into_one_view(self):
        selected = [self.batch("demo2_store_period_metrics", {"transaction_amount": "1"}, "first"),
                    self.batch("store_period_panel_metrics", {"transaction_amount": "2"}, "second")]
        with self.assertRaisesRegex(ValueError, "same canonical key"):
            build_evidence_view(self.root, selected)

    def test_projection_cannot_silently_drop_fields_to_accept_a_different_schema(self):
        path = self.root / PROFILE_PATH
        profile = json.loads(path.read_text())
        target = next(view for view in profile["views"] if view["dataset_id"] == "demo2_store_period_metrics")
        target["input_dataset_ids"] = ["store_a_monthly_metrics"]
        path.write_text(json.dumps(profile))
        with self.assertRaisesRegex(ValueError, "canonical field contracts"):
            build_evidence_view(self.root, [])

    def test_recipe_hashes_code_and_excludes_live_business_files(self):
        for name in CODE_FILES:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, path)
        before = recipe_files(self.root)
        for name in ("retail_ops/data/demo2_store_period_metrics.csv",
                     "retail_ops/outputs/generated_demo2_retail_memory_facts.json"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("synthetic unrelated live data")
        self.assertEqual(before, recipe_files(self.root))
        self.assertFalse(set(CODE_FILES) & set(before))
        path = self.root / "retail_ops/sql_runtime.py"
        path.write_bytes(path.read_bytes() + b"\n# synthetic code change\n")
        after = recipe_files(self.root)
        self.assertNotEqual(before["publication_runtime.json"], after["publication_runtime.json"])


if __name__ == "__main__":
    unittest.main()
