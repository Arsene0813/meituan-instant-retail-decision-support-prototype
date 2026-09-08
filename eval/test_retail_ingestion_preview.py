from __future__ import annotations

import csv
import io
import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from retail_ops.ingestion import preview as ingestion
from retail_ops.ingestion.contracts import load_dataset_contracts


ROOT = Path(__file__).resolve().parents[1]


def csv_bytes(rows):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(dict.fromkeys(k for row in rows for k in row)))
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


class RetailIngestionPreviewTests(unittest.TestCase):
    def setUp(self):
        self.context = ingestion.UploadContext(
            "demo2_store_period_metrics", "B", "2026-03-01", "2026-03-31", "store_period",
        )
        self.row = {
            "store_id": "B", "period_start": "2026-03-01", "period_end": "2026-03-31",
            "period_month": "2026-03", "transaction_amount": "11665.50", "transaction_orders": "299",
        }

    def preview(self, rows=None, **kwargs):
        return ingestion.preview_csv(ROOT, csv_bytes(rows if rows is not None else [self.row]), self.context, **kwargs)

    def proposal(self, **changes):
        return dict(dataset_id=self.context.dataset_id, grain=self.context.grain,
                    ranking_basis=self.context.ranking_basis, record=dict(self.row, **changes))

    def assert_quarantined(self, result):
        self.assertEqual(result["status"], "quarantined")
        self.assertEqual(result["validated_records"], [])
        self.assertTrue(result["errors"] or result["quarantined_records"])

    def test_schemas_match_all_current_csv_headers_and_dictionary_names(self):
        dictionary = (ROOT / "retail_ops/data/DATA_DICTIONARY.md").read_text()
        contracts = load_dataset_contracts(ROOT)
        self.assertEqual(set(ingestion.SCHEMAS), set(contracts))
        for dataset, fields in ingestion.SCHEMAS.items():
            with self.subTest(dataset=dataset):
                with (ROOT / contracts[dataset].source_path).open(newline="") as f:
                    self.assertEqual(set(next(csv.reader(f))), fields)
                for field in fields:
                    self.assertIn(f"`{field}`", dictionary)

    def test_all_current_source_rows_validate_in_their_confirmed_scope(self):
        for contract in load_dataset_contracts(ROOT).values():
            with (ROOT / contract.source_path).open(newline="") as f:
                rows = list(csv.DictReader(f))
            for row in rows:
                with self.subTest(dataset=contract.dataset_id, store=row["store_id"], month=row["period_month"]):
                    context = ingestion.UploadContext(contract.dataset_id, row["store_id"], row["period_start"],
                                                      row["period_end"], contract.grain, contract.ranking_basis)
                    result = ingestion.preview_csv(ROOT, csv_bytes([row]), context)
                    self.assertEqual(result["status"], "validated", result)

    def test_missing_values_and_explicit_zero_are_distinct(self):
        self.row.update(transaction_amount=" ", transaction_orders="0")
        result = self.preview()
        self.assertEqual(result["status"], "validated")
        record = result["validated_records"][0]["record"]
        self.assertIsNone(record["transaction_amount"])
        self.assertIsNone(record["exposure_users"])
        self.assertEqual(record["transaction_orders"], 0)
        serialized = json.loads(ingestion.preview_json(result))
        self.assertIsNone(serialized["validated_records"][0]["record"]["transaction_amount"])

    def test_invalid_numbers_never_become_zero(self):
        for field, values in {
            "transaction_amount": ["abc", "NaN", "Infinity", "-", "null", "1,000", "1e309", "12元"],
            "transaction_orders": ["1.5", "-1", "True"],
            "order_conversion_rate_pct": ["36.88%"],
        }.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    self.assert_quarantined(self.preview([dict(self.row, **{field: value})]))

    def test_decimal_precision_and_reported_percent_units_are_preserved(self):
        self.row.update(transaction_amount="123456789012345.67", order_conversion_rate_pct="0.36")
        result = self.preview()
        record = result["validated_records"][0]["record"]
        self.assertEqual(record["transaction_amount"], Decimal("123456789012345.67"))
        self.assertEqual(record["order_conversion_rate_pct"], Decimal("0.36"))
        self.assertIn('"123456789012345.67"', ingestion.preview_json(result))

    def test_store_and_period_cannot_be_changed_or_filled_from_context(self):
        for field, value in (("store_id", "C"), ("store_id", "b"), ("store_id", ""),
                             ("period_start", "2026-02-01"), ("period_end", ""), ("period_month", "2026-04")):
            with self.subTest(field=field, value=value):
                self.assert_quarantined(self.preview([dict(self.row, **{field: value})]))
        for start, end in (("2026-03-15", "2026-04-15"), ("2026-03-01", "2026-03-30"),
                           ("2026-03-31", "2026-03-01"), ("2026-02-01", "2026-02-30")):
            scope = replace(self.context, period_start=start, period_end=end)
            self.assert_quarantined(ingestion.preview_csv(ROOT, csv_bytes([self.row]), scope))

    def test_unknown_fields_are_held_even_when_the_value_is_blank(self):
        for value in ("99", ""):
            result = self.preview([dict(self.row, new_metric=value)])
            self.assert_quarantined(result)
            self.assertEqual(result["quarantined_records"][0]["source_record"]["new_metric"], value)

    def test_month_metadata_is_required_by_preview_and_sql_for_every_dataset(self):
        from retail_ops.sql_runtime import prepare_rows

        for contract in load_dataset_contracts(ROOT).values():
            with (ROOT / contract.source_path).open(newline="") as source:
                row = next(csv.DictReader(source))
            scope = ingestion.UploadContext(contract.dataset_id, row["store_id"],
                row["period_start"], row["period_end"], contract.grain, contract.ranking_basis)
            for state in ("omitted", "", " ", None):
                with self.subTest(dataset=contract.dataset_id, month=state):
                    candidate = dict(row)
                    if state == "omitted":
                        candidate.pop("period_month")
                    else:
                        candidate["period_month"] = state
                    result = ingestion.preview_csv(ROOT, csv_bytes([candidate]), scope)
                    self.assert_quarantined(result)
                    self.assertIn("missing required period metadata: period_month",
                                  result["quarantined_records"][0]["errors"])
                    self.assertEqual(result["quarantined_records"][0]["source_record"].get("period_month"),
                                     candidate.get("period_month") or ("" if "period_month" in candidate else None))
                    with self.assertRaises(ValueError):
                        prepare_rows(contract.dataset_id, list(candidate), [candidate])

    def test_unknown_dataset_has_no_same_grain_fallback(self):
        scope = replace(self.context, dataset_id="future_store_data")
        self.assert_quarantined(ingestion.preview_csv(ROOT, csv_bytes([self.row]), scope))
        contracts = load_dataset_contracts(ROOT)
        contracts[scope.dataset_id] = replace(contracts[self.context.dataset_id], dataset_id=scope.dataset_id)
        with patch.object(ingestion, "load_dataset_contracts", return_value=contracts):
            self.assert_quarantined(ingestion.preview_csv(ROOT, csv_bytes([self.row]), scope))

    def test_grain_ranking_basis_and_contract_drift_are_checked(self):
        for updates in ({"grain": "store_sku_period"}, {"ranking_basis": "sales_volume"}):
            self.assert_quarantined(ingestion.preview_csv(ROOT, csv_bytes([self.row]), replace(self.context, **updates)))
        contracts = load_dataset_contracts(ROOT)
        contracts[self.context.dataset_id] = replace(contracts[self.context.dataset_id], key_fields=("store_id",))
        with patch.object(ingestion, "load_dataset_contracts", return_value=contracts):
            self.assert_quarantined(self.preview())

    def test_model_must_preserve_source_values_and_routing(self):
        self.assertEqual(self.preview(proposals=[self.proposal()])["status"], "validated")
        for updates in ({"transaction_amount": "11665.51"}, {"transaction_amount": None},
                        {"store_id": "C"}, {"exposure_users": "0"}, {"transaction_orders": True}):
            with self.subTest(updates=updates):
                self.assert_quarantined(self.preview(proposals=[self.proposal(**updates)]))
        for field, value in (("dataset_id", "store_period_panel_metrics"), ("grain", "store_sku_period"),
                             ("ranking_basis", "sales_volume")):
            proposal = self.proposal()
            proposal[field] = value
            self.assert_quarantined(self.preview(proposals=[proposal]))
        self.assert_quarantined(self.preview(proposals=[]))
        self.assert_quarantined(self.preview(proposals=[dict(self.proposal(), confidence=1.0)]))

    def test_source_scope_conflict_is_not_repaired_by_a_model(self):
        self.assert_quarantined(self.preview([dict(self.row, store_id="C")], proposals=[self.proposal()]))

    def test_excluded_metrics_are_absent_from_success_and_error_outputs(self):
        excluded = {"有效订单数": "998877", "无效订单数": "887766"}
        for additions in ({}, {"new_metric": "pending"}):
            result = self.preview([dict(self.row, **excluded, **additions)])
            serialized = ingestion.preview_json(result)
            for text in (*excluded, *excluded.values()):
                self.assertNotIn(text, serialized)
        result = self.preview([dict(self.row, **excluded)], proposals=[self.proposal(**excluded)])
        self.assertEqual(result["status"], "validated")

    def test_duplicate_keys_hold_all_conflicting_rows(self):
        result = self.preview([self.row, dict(self.row, transaction_amount="10")])
        self.assert_quarantined(result)
        self.assertEqual(len(result["quarantined_records"]), 2)

    def test_partial_upload_is_held_instead_of_exposing_incomplete_ranking(self):
        scope = replace(self.context, dataset_id="demo2_top_search_terms", grain="store_search_term_period")
        base = {key: self.row[key] for key in ("store_id", "period_start", "period_end", "period_month")}
        rows = [dict(base, search_term_rank="1", search_term="已知词", search_term_order_times="0"),
                dict(base, search_term_rank="2", search_term="第二个词", search_term_order_times="bad")]
        result = ingestion.preview_csv(ROOT, csv_bytes(rows), scope)
        self.assert_quarantined(result)
        self.assertEqual(len(result["quarantined_records"]), 2)

    def test_duplicate_proposal_json_keys_cannot_silently_replace_values(self):
        with self.assertRaisesRegex(ValueError, "duplicate JSON"):
            json.loads('{"transaction_amount": 1, "transaction_amount": 2}', object_pairs_hook=ingestion._json_object)

    def test_same_name_sku_rows_and_separate_ranking_bases_remain_separate(self):
        scope = replace(self.context, dataset_id="demo2_top_skus_by_sales_volume",
                        grain="store_sku_period", ranking_basis="sales_volume")
        base = {key: self.row[key] for key in ("store_id", "period_start", "period_end", "period_month")}
        rows = [dict(base, sku_name="同名商品", sku_rank=str(rank), sales_volume=str(volume))
                for rank, volume in ((2, 45), (3, 33))]
        result = ingestion.preview_csv(ROOT, csv_bytes(rows), scope)
        self.assertEqual(result["status"], "validated")
        self.assertEqual(len(result["validated_records"]), 2)
        self.assertTrue(all(r["record"]["sku_transaction_amount"] is None for r in result["validated_records"]))
        self.assert_quarantined(ingestion.preview_csv(ROOT, csv_bytes(rows), replace(scope, ranking_basis="transaction_amount")))

    def test_malformed_csv_does_not_silently_drop_cells(self):
        for data in (b"store_id,store_id\nB,C\n", b"store_id\nB,99\n", b"store_id,period_start\nB\n",
                     b"store_id\n\"unterminated", b"\xff\xfe", b"store_id\n"):
            with self.subTest(data=data):
                self.assert_quarantined(ingestion.preview_csv(ROOT, data, self.context))

    def test_cli_emits_preview_and_failure_exit_status_without_modifying_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "upload.csv"
            data = csv_bytes([self.row])
            path.write_bytes(data)
            command = [sys.executable, "-m", "retail_ops.ingestion.preview", "--input", str(path),
                       "--dataset-id", self.context.dataset_id, "--store-id", "B", "--period-start",
                       "2026-03-01", "--period-end", "2026-03-31", "--grain", "store_period"]
            completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(completed.stdout)["mode"], "preview")
            command[command.index("--store-id") + 1] = "C"
            completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 2)
            self.assertEqual(json.loads(completed.stdout)["validated_records"], [])
            self.assertEqual(path.read_bytes(), data)


if __name__ == "__main__":
    unittest.main()
