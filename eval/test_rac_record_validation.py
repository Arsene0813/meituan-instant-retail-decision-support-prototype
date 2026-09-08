"""Synthetic source mutations for RAC record selection and numeric fidelity."""

import csv
import shutil
import tempfile
import unittest
from pathlib import Path

from rac.src.demo2_csv_grounding import (
    REPEATED_WINDOW_SOURCE_PATH,
    SNAPSHOT_SOURCE_PATH,
    resolve_demo2_record,
)
from rac.src.store_a_csv_grounding import SOURCE_PATH, resolve_store_a_record


ROOT = Path(__file__).resolve().parents[1]
PANEL = "retail_ops/data/store_period_panel_metrics.csv"


class RacRecordValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.original = {}
        for name in (
            SOURCE_PATH, SNAPSHOT_SOURCE_PATH, REPEATED_WINDOW_SOURCE_PATH,
            PANEL, "retail_ops/contracts/datasets.v1.json",
        ):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)
            self.original[name] = target.read_bytes()

    def reset_source(self, name):
        (self.root / name).write_bytes(self.original[name])

    def change_rows(self, name, transform):
        path = self.root / name
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            fields, rows = reader.fieldnames, list(reader)
        transform(rows)
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    def resolve(self, name, factor=None):
        if name == SOURCE_PATH:
            return resolve_store_a_record(
                {}, factor_id=factor or "transaction_orders", root=self.root
            )
        return resolve_demo2_record(
            {}, factor_id=factor or (
                "order_volume" if name == SNAPSHOT_SOURCE_PATH
                else "repeated_reporting_windows"
            ), root=self.root,
        )

    def assert_contract_error(self, result):
        self.assertEqual(result["grounding_status"], "record_contract_error", result)
        self.assertTrue(result["record_contract_errors"])
        self.assertEqual(result["evidence_values"], [])

    def test_declared_baseline_records_and_row_keys_are_preserved(self):
        for name in (SOURCE_PATH, SNAPSHOT_SOURCE_PATH, REPEATED_WINDOW_SOURCE_PATH):
            with self.subTest(source=name):
                result = self.resolve(name)
                self.assertEqual(result["grounding_status"], "record_matched", result)
                keys = result["record_scope"]["key_fields"]
                self.assertEqual(keys, ["store_id"] if name == REPEATED_WINDOW_SOURCE_PATH
                                 else ["store_id", "period_month"])

    def test_transaction_amount_has_a_store_a_record_route(self):
        result = self.resolve(SOURCE_PATH, "transaction_amount")
        self.assertEqual(result["grounding_status"], "record_matched")
        self.assertEqual(result["evidence_fields"], ["transaction_amount"])

    def test_complete_dates_and_month_labels_must_match(self):
        mutations = [
            ("period_start", "2026-03-02"), ("period_end", "2026-04-30"),
            ("period_start", "2026-02-30"), ("period_month", "2026-04"),
            ("period_end", ""), ("store_id", " B "),
        ]
        for name in (SOURCE_PATH, SNAPSHOT_SOURCE_PATH):
            for field, value in mutations:
                with self.subTest(source=name, field=field, value=value):
                    self.reset_source(name)
                    self.change_rows(name, lambda rows: next(
                        row for row in rows if row["period_month"] == "2026-03"
                    ).__setitem__(field, value))
                    self.assert_contract_error(self.resolve(name))

    def test_missing_date_column_is_a_contract_error(self):
        for name in (SOURCE_PATH, SNAPSHOT_SOURCE_PATH):
            with self.subTest(source=name):
                path = self.root / name
                with path.open(newline="") as handle:
                    cells = list(csv.reader(handle))
                index = cells[0].index("period_end")
                with path.open("w", newline="") as handle:
                    csv.writer(handle).writerows([row[:index] + row[index + 1:] for row in cells])
                self.assert_contract_error(self.resolve(name))

    def test_invalid_numeric_text_and_fractional_counts_are_rejected(self):
        for name in (SOURCE_PATH, SNAPSHOT_SOURCE_PATH):
            for field, value in [
                ("transaction_orders", "1.5"), ("transaction_orders", "-1"),
                ("transaction_orders", "NaN"), ("transaction_orders", "1e3"),
                ("transaction_amount", "Infinity"), ("transaction_amount", "10元"),
            ]:
                with self.subTest(source=name, field=field, value=value):
                    self.reset_source(name)
                    self.change_rows(name, lambda rows: rows[0].__setitem__(field, value))
                    self.assert_contract_error(self.resolve(name))

    def test_blank_zero_large_integer_and_exact_decimal_remain_raw(self):
        for name in (SOURCE_PATH, SNAPSHOT_SOURCE_PATH):
            for field, value in [
                ("transaction_orders", ""), ("transaction_orders", "0"),
                ("transaction_orders", "9007199254740993123456789"),
                ("transaction_amount", "123456789.123456789012345678901"),
            ]:
                with self.subTest(source=name, field=field, value=value):
                    self.reset_source(name)
                    self.change_rows(name, lambda rows: next(
                        row for row in rows if row["period_month"] == "2026-03"
                    ).__setitem__(field, value))
                    factor = "transaction_amount" if field == "transaction_amount" else None
                    result = self.resolve(name, factor)
                    self.assertEqual(result["grounding_status"], "record_matched", result)
                    self.assertEqual(result["evidence_values"][0]["values"][field], value)

    def test_duplicate_and_missing_selected_records_are_rejected(self):
        for name in (SOURCE_PATH, SNAPSHOT_SOURCE_PATH, REPEATED_WINDOW_SOURCE_PATH):
            for duplicate in (True, False):
                with self.subTest(source=name, duplicate=duplicate):
                    self.reset_source(name)
                    def mutate(rows):
                        index = 1 if name == SOURCE_PATH else 0
                        if duplicate:
                            rows.append(dict(rows[index]))
                        else:
                            rows.pop(index)
                    self.change_rows(name, mutate)
                    self.assert_contract_error(self.resolve(name))

    def test_duplicate_headers_and_wrong_cell_counts_are_rejected(self):
        for name in (SOURCE_PATH, SNAPSHOT_SOURCE_PATH, REPEATED_WINDOW_SOURCE_PATH):
            for fault in ("duplicate", "extra", "missing"):
                with self.subTest(source=name, fault=fault):
                    self.reset_source(name)
                    path = self.root / name
                    with path.open(newline="") as handle:
                        rows = list(csv.reader(handle))
                    if fault == "duplicate":
                        rows[0][-1] = rows[0][0]
                    elif fault == "extra":
                        rows[1].append("unregistered cell")
                    else:
                        rows[1].pop()
                    with path.open("w", newline="") as handle:
                        csv.writer(handle).writerows(rows)
                    self.assert_contract_error(self.resolve(name))

    def test_repeated_summary_is_reconciled_against_current_source_panel(self):
        self.change_rows(PANEL, lambda rows: next(
            row for row in rows if row["store_id"] == "B" and row["period_month"] == "2026-04"
        ).__setitem__("transaction_amount", "1"))
        self.assert_contract_error(self.resolve(REPEATED_WINDOW_SOURCE_PATH))

    def test_repeated_summary_blank_and_zero_must_match_source(self):
        for value in ("", "0", "9007199254740993123456789"):
            with self.subTest(value=value):
                self.reset_source(PANEL)
                self.reset_source(REPEATED_WINDOW_SOURCE_PATH)
                self.change_rows(PANEL, lambda rows: next(
                    row for row in rows if row["store_id"] == "B" and row["period_month"] == "2026-02"
                ).__setitem__("transaction_orders", value))
                output = value + ".0" if value else ""
                self.change_rows(REPEATED_WINDOW_SOURCE_PATH, lambda rows: rows[0].__setitem__(
                    "feb_transaction_orders", output
                ))
                result = self.resolve(REPEATED_WINDOW_SOURCE_PATH)
                self.assertEqual(result["grounding_status"], "record_matched", result)
                self.assertEqual(result["evidence_values"][0]["values"]["feb_transaction_orders"], output)

    def test_repeated_summary_rejects_malformed_count_and_incomplete_panel_window(self):
        self.change_rows(REPEATED_WINDOW_SOURCE_PATH, lambda rows: rows[0].__setitem__(
            "observed_month_count", "3.5"
        ))
        self.assert_contract_error(self.resolve(REPEATED_WINDOW_SOURCE_PATH))
        self.reset_source(REPEATED_WINDOW_SOURCE_PATH)
        self.change_rows(PANEL, lambda rows: rows[0].__setitem__("period_end", "2026-02-27"))
        self.assert_contract_error(self.resolve(REPEATED_WINDOW_SOURCE_PATH))


if __name__ == "__main__":
    unittest.main()
