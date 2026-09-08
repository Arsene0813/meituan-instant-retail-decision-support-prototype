from __future__ import annotations

import contextlib
import copy
import csv
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from retail_ops.scripts import generate_demo2_retail_memory_facts as generator


ROOT = Path(__file__).resolve().parents[1]
DIAGNOSTICS = generator.COMPARABILITY_OUTPUT
SEARCH = Path(generator.TOP_SEARCH_TERMS_SOURCE_PATH)
SKU = Path(generator.TOP_SKUS_BY_AMOUNT_SOURCE_PATH)
CONTRACTS = Path("retail_ops/contracts/datasets.v1.json")


class Demo2FactGenerationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for relative in (DIAGNOSTICS, SEARCH, SKU, CONTRACTS, generator.OUTPUT_PATH):
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, target)

    def read(self, relative):
        with (self.root / relative).open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def write(self, relative, rows):
        with (self.root / relative).open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def fact(self, slot, period="2026-03", facts=None):
        facts = generator.build_facts(self.root) if facts is None else facts
        return next(fact for fact in facts if fact["entity_id"] == "store_B"
                    and fact["slot"] == slot and fact["period_label"] == period)

    def test_current_fixture_preserves_recorded_values_and_fact_scope(self):
        expected = json.loads((ROOT / generator.OUTPUT_PATH).read_text())
        self.assertEqual(generator.build_facts(self.root), expected)
        self.assertEqual(len(expected), 25)

    def test_same_store_different_months_use_their_own_auxiliary_records(self):
        for relative in (DIAGNOSTICS, SEARCH, SKU):
            rows = self.read(relative)
            april = copy.deepcopy([row for row in rows if row["store_id"] == "B"])
            for row in april:
                row.update(period_month="2026-04", period_start="2026-04-01", period_end="2026-04-30")
                if relative == SEARCH:
                    row["search_term"] = "APRIL_ONLY_" + row["search_term_rank"]
                elif relative == SKU:
                    row["sku_name"] = "APRIL_ONLY_" + row["sku_rank"]
                else:
                    row["comparison_scope_flag"] = "not_comparable_period_mismatch"
            self.write(relative, rows + april)
        facts = generator.build_facts(self.root)
        self.assertEqual(len(facts), 30)
        for period, marker in (("2026-03", False), ("2026-04", True)):
            visibility = self.fact("visibility_entry_profile", period, facts)
            products = self.fact("top3_sku_product_mix_note", period, facts)
            terms = visibility["observed_values"]["top_search_terms"]
            skus = products["observed_values"]["top_skus_by_transaction_amount"]
            self.assertEqual(len(terms), 3)
            self.assertEqual(len(skus), 3)
            self.assertTrue(all(item["search_term"].startswith("APRIL_ONLY_") == marker for item in terms))
            self.assertTrue(all(item["sku_name"].startswith("APRIL_ONLY_") == marker for item in skus))
        april_facts = [fact for fact in facts if fact["period_label"] == "2026-04"]
        for fact in april_facts:
            self.assertEqual((fact["period_start"], fact["period_end"]), ("2026-04-01", "2026-04-30"))
            self.assertIn("April 2026", fact["value"])
            self.assertNotIn("March 2026", json.dumps(fact))
        self.assertEqual(self.fact("single_metric_attribution_guard", "2026-04", facts)
                         ["observed_values"]["comparison_scope_flag"], "not_comparable_period_mismatch")

    def test_missing_same_period_auxiliary_rows_do_not_fall_back_to_another_month(self):
        for relative in (SEARCH, SKU):
            rows = self.read(relative)
            for row in rows:
                if row["store_id"] == "B":
                    row.update(period_month="2026-04", period_start="2026-04-01", period_end="2026-04-30")
            self.write(relative, rows)
        self.assertEqual(self.fact("visibility_entry_profile")["observed_values"]["top_search_terms"], [])
        self.assertEqual(self.fact("top3_sku_product_mix_note")["observed_values"]["top_skus_by_transaction_amount"], [])

    def test_optional_missing_columns_and_blank_values_stay_null_without_backfill(self):
        original = self.read(DIAGNOSTICS)
        for missing in (None, "", " \t ", "omitted"):
            with self.subTest(missing=missing):
                rows = copy.deepcopy(original)
                for row in rows:
                    for field in ("transaction_orders", "average_order_value", "payment_conversion_rate_pct"):
                        if missing == "omitted":
                            row.pop(field)
                        else:
                            row[field] = missing
                self.write(DIAGNOSTICS, rows)
                observed = self.fact("transaction_conversion_profile")["observed_values"]
                for field in ("transaction_orders", "average_order_value", "payment_conversion_rate_pct"):
                    self.assertIsNone(observed[field])
                self.assertEqual(observed["transaction_amount"], 11665.5)

    def test_explicit_zero_counts_amounts_and_auxiliary_values_stay_zero(self):
        for relative, fields in ((DIAGNOSTICS, ("transaction_orders", "transaction_amount")),
                                 (SEARCH, ("search_term_order_times",)), (SKU, ("sku_transaction_amount",))):
            rows = self.read(relative)
            for field in fields:
                rows[0][field] = "0"
            self.write(relative, rows)
        self.assertEqual(self.fact("transaction_conversion_profile")["observed_values"]["transaction_orders"], 0)
        self.assertEqual(self.fact("transaction_conversion_profile")["observed_values"]["transaction_amount"], 0)
        self.assertEqual(self.fact("visibility_entry_profile")["observed_values"]["top_search_terms"][0]["search_term_order_times"], 0)
        self.assertEqual(self.fact("top3_sku_product_mix_note")["observed_values"]["top_skus_by_transaction_amount"][0]["sku_transaction_amount"], 0)

    def test_large_integer_and_more_than_two_decimal_places_survive_json(self):
        rows = self.read(DIAGNOSTICS)
        rows[0].update(transaction_orders="9007199254740993", transaction_amount="123.4567")
        self.write(DIAGNOSTICS, rows)
        with contextlib.redirect_stdout(io.StringIO()):
            generator.main(self.root)
        facts = json.loads((self.root / generator.OUTPUT_PATH).read_text(), parse_float=Decimal)
        observed = self.fact("transaction_conversion_profile", facts=facts)["observed_values"]
        self.assertEqual(observed["transaction_orders"], 9007199254740993)
        self.assertIsInstance(observed["transaction_orders"], int)
        self.assertEqual(observed["transaction_amount"], Decimal("123.4567"))

    def test_invalid_numeric_values_stop_before_replacing_existing_output(self):
        original = self.read(DIAGNOSTICS)
        output = self.root / generator.OUTPUT_PATH
        before = output.read_bytes()
        for field, invalid in (("transaction_orders", "2.5"), ("transaction_orders", "1e3"),
                               ("transaction_orders", "-1"), ("transaction_amount", "NaN"),
                               ("transaction_amount", "Infinity"), ("transaction_amount", "1_000"),
                               ("transaction_amount", "1e400"), ("transaction_amount", "1e-400"),
                               ("transaction_amount", "123456789012345.6789")):
            with self.subTest(field=field, invalid=invalid):
                rows = copy.deepcopy(original)
                rows[0][field] = invalid
                self.write(DIAGNOSTICS, rows)
                with self.assertRaises((ValueError, ArithmeticError)):
                    generator.main(self.root)
                self.assertEqual(output.read_bytes(), before)

    def test_invalid_auxiliary_numeric_values_are_not_silently_truncated(self):
        for relative, field in ((SEARCH, "search_term_order_times"), (SKU, "sales_volume")):
            rows = self.read(relative)
            original = copy.deepcopy(rows)
            rows[0][field] = "2.5"
            self.write(relative, rows)
            with self.assertRaises(ValueError):
                generator.build_facts(self.root)
            self.write(relative, original)

    def test_missing_optional_auxiliary_values_stay_null(self):
        rows = self.read(SEARCH)
        rows[0]["search_term_order_times"] = ""
        self.write(SEARCH, rows)
        rows = self.read(SKU)
        rows[0]["sku_transaction_amount"] = ""
        self.write(SKU, rows)
        self.assertIsNone(self.fact("visibility_entry_profile")["observed_values"]["top_search_terms"][0]["search_term_order_times"])
        self.assertIsNone(self.fact("top3_sku_product_mix_note")["observed_values"]["top_skus_by_transaction_amount"][0]["sku_transaction_amount"])

    def test_missing_conflicting_or_partial_period_metadata_is_rejected(self):
        for relative in (DIAGNOSTICS, SEARCH, SKU):
            original = self.read(relative)
            for field, value in (("store_id", ""), ("period_month", ""), ("period_month", "2026-04"),
                                 ("period_end", "2026-03-30"), ("period_start", "2026-03-02")):
                with self.subTest(relative=relative, field=field, value=value):
                    rows = copy.deepcopy(original)
                    rows[0][field] = value
                    self.write(relative, rows)
                    with self.assertRaises(ValueError):
                        generator.build_facts(self.root)
            self.write(relative, original)

    def test_duplicate_store_period_or_rank_keys_are_rejected(self):
        for relative in (DIAGNOSTICS, SEARCH, SKU):
            rows = self.read(relative)
            self.write(relative, rows + [copy.deepcopy(rows[0])])
            with self.subTest(relative=relative), self.assertRaises(ValueError):
                generator.build_facts(self.root)
            self.write(relative, rows)

    def test_identical_names_keep_distinct_source_ranks_in_rank_order(self):
        for relative, name, rank in ((SEARCH, "search_term", "search_term_rank"), (SKU, "sku_name", "sku_rank")):
            rows = self.read(relative)
            rows[0][name] = rows[1][name] = "SAME_NAME"
            self.write(relative, list(reversed(rows)))
        facts = generator.build_facts(self.root)
        for slot, key, name, rank in (("visibility_entry_profile", "top_search_terms", "search_term", "search_term_rank"),
                                      ("top3_sku_product_mix_note", "top_skus_by_transaction_amount", "sku_name", "sku_rank")):
            items = self.fact(slot, facts=facts)["observed_values"][key]
            self.assertEqual([item[rank] for item in items], [1, 2, 3])
            self.assertEqual([item[name] for item in items[:2]], ["SAME_NAME", "SAME_NAME"])

    def test_ranking_basis_requires_the_registered_amount_dataset(self):
        contracts = json.loads((self.root / CONTRACTS).read_text())
        for item in contracts["datasets"]:
            if item["dataset_id"] == "demo2_top_skus_by_transaction_amount":
                item["ranking_basis"] = "sales_volume"
        (self.root / CONTRACTS).write_text(json.dumps(contracts))
        with self.assertRaises(ValueError):
            generator.build_facts(self.root)

    def test_top3_slot_uses_source_ranks_one_to_three(self):
        rows = self.read(SKU)
        rank_four = dict(rows[0], sku_rank="4", sku_name="RANK_FOUR", sku_transaction_amount="999.99")
        self.write(SKU, [row for row in rows if not (row["store_id"] == "B" and row["sku_rank"] == "1")] + [rank_four])
        items = self.fact("top3_sku_product_mix_note")["observed_values"]["top_skus_by_transaction_amount"]
        self.assertEqual([item["sku_rank"] for item in items], [2, 3])
        self.assertNotIn("RANK_FOUR", [item["sku_name"] for item in items])

    def test_unknown_or_duplicate_columns_and_ragged_csv_are_rejected(self):
        for relative in (DIAGNOSTICS, SEARCH, SKU):
            path = self.root / relative
            original = path.read_text()
            lines = original.splitlines()
            for bad in (lines[0] + ",unknown_metric\n" + "\n".join(lines[1:]),
                        lines[0] + ",store_id\n" + "\n".join(lines[1:]),
                        original + "B,too,few,cells\n"):
                with self.subTest(relative=relative):
                    path.write_text(bad)
                    with self.assertRaises(ValueError):
                        generator.build_facts(self.root)
            path.write_text(original)

    def test_failed_atomic_replace_keeps_existing_output_and_cleans_temporary_file(self):
        output = self.root / generator.OUTPUT_PATH
        before = output.read_bytes()
        files_before = set(output.parent.iterdir())
        with patch.object(generator.os, "replace", side_effect=OSError("simulated write failure")):
            with self.assertRaises(OSError):
                generator.main(self.root)
        self.assertEqual(output.read_bytes(), before)
        self.assertEqual(set(output.parent.iterdir()), files_before)

    def test_import_has_no_file_reads_or_writes(self):
        script = (
            "from unittest.mock import patch; from pathlib import Path; "
            "from retail_ops import sql_runtime; "
            "from retail_ops.ingestion import preview; "
            "guard = patch.object(Path, 'open', side_effect=AssertionError('unexpected file IO')); "
            "guard.start(); import retail_ops.scripts.generate_demo2_retail_memory_facts"
        )
        subprocess.run([sys.executable, "-c", script], cwd=ROOT, check=True, capture_output=True)


if __name__ == "__main__":
    unittest.main()
