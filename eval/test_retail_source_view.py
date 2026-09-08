"""Synthetic replayed records retain source values, dates and line-level identity."""
from copy import deepcopy
import csv
import hashlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from retail_ops.ingestion import batch_store, preview, source_view
from retail_ops.ingestion.contracts import load_dataset_contracts


ROOT = Path(__file__).resolve().parents[1]


class RetailSourceViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("retail_ops/contracts/datasets.v1.json", "retail_ops/data/DATA_DICTIONARY.md"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, path)
        self.contracts = load_dataset_contracts(self.root)
        self.serial = 0

    def selected(self, *, start="2026-03-01", end="2026-03-31", month="2026-03",
                 dataset="demo2_store_period_metrics", rows=None, store="B", mapping="canonical_csv_v1"):
        """Represent the normalized output of upstream independent replay."""
        self.serial += 1
        batch = "batch_" + format(self.serial, "032x")
        rows = [{"transaction_amount": "9007199254740993.0100", "transaction_orders": "0"}] if rows is None else rows
        source = [{"store_id": store, "period_start": start, "period_end": end,
                   **({"period_month": month} if month is not None else {}), **row} for row in rows]
        fields = sorted(set().union(*(set(row) for row in source)))
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(source)
        data = buffer.getvalue().encode()
        digest = hashlib.sha256(data).hexdigest()
        context = {"dataset_id": dataset, "store_id": store, "period_start": start, "period_end": end,
                   "grain": self.contracts[dataset].grain, "ranking_basis": self.contracts[dataset].ranking_basis}
        receipt = {"upload_id": "synthetic_" + batch, "binding_id": "synthetic_binding",
                   "dataset_id": dataset, "period_start": start, "period_end": end,
                   "source_page": "synthetic_source_page", "extracted_at": "2026-05-01T00:00:00Z",
                   "file_sha256": digest, "mapping_version": mapping}
        binding = {"binding_id": "synthetic_binding", "source_system": "meituan_merchant_backend",
                   "source_account_id": "synthetic_account", "source_store_id": "synthetic_store_" + store,
                   "store_id": store, "dataset_ids": [dataset]}
        metadata = {"batch_id": batch, "dataset_id": dataset, "source_system": binding["source_system"],
                    "source_name": self.contracts[dataset].source_name,
                    "snapshot_semantics": self.contracts[dataset].snapshot_semantics,
                    "coverage_start": start, "coverage_end": end, "status": "validated",
                    **{key: receipt[key] for key in ("source_page", "extracted_at", "file_sha256", "mapping_version")}}
        reader = csv.DictReader(io.StringIO(data.decode(), newline=""))
        validated = []
        for row in reader:
            record = {field: preview._value(field, row.get(field)) for field in preview.SCHEMAS[dataset]}
            validated.append({"source_line_end": reader.line_num, "record": json.loads(batch_store._json(record))})
        checked = {"context": dict(context), "status": "validated", "errors": [], "quarantined_records": [],
                   "file_sha256": digest, "mapping_version": mapping, "validated_records": validated}
        result = {"batch_id": batch, "upload_id": receipt["upload_id"], "status": "validated", "errors": [],
                  "file_sha256": digest, "metadata": metadata, "preview": checked,
                  "registration": {"receipt": receipt, "binding": binding, "context": context}}
        return {"result": result, "data": data, "registry_bytes": b"synthetic upstream archive",
                "identity_evidence": b"synthetic upstream identity"}

    def test_exact_values_and_lineage_without_live_csv_fallback(self):
        selected = self.selected()
        before = deepcopy(selected)
        path_open = Path.open

        def guarded(path, *args, **kwargs):
            if path.suffix == ".csv":
                raise AssertionError("source view must not read repository CSV fixtures")
            return path_open(path, *args, **kwargs)

        with patch.object(Path, "open", guarded):
            records = source_view.source_records(self.root, [selected])
        self.assertEqual(selected, before)
        self.assertEqual(len(records), 1)
        entry = records[0]
        self.assertEqual(entry["dataset_id"], "demo2_store_period_metrics")
        self.assertEqual(entry["record"]["transaction_amount"], "9007199254740993.0100")
        self.assertEqual(entry["record"]["transaction_orders"], 0)
        self.assertIsNone(entry["record"]["entry_users"])
        self.assertEqual(entry["source"]["source_line_end"], 2)
        self.assertEqual(entry["source"]["source_account_id"], "synthetic_account")
        self.assertEqual(entry["source"]["file_sha256"], hashlib.sha256(selected["data"]).hexdigest())
        self.assertIsNone(entry["source"]["aggregation_scope"])
        self.assertIsNone(entry["source"]["aggregation_scope_sha256"])

    def test_reviewed_receipt_scope_and_its_digest_are_preserved(self):
        selected = self.selected(start="2026-03-15", end="2026-03-15", month=None, mapping="canonical_csv_v2")
        scope = {"scope_version": "1", "timezone": "Asia/Shanghai",
                 "selection_conditions": "全部配送方式；该店全部商品；无其他筛选条件",
                 "reviewed_by": "synthetic_operator"}
        selected["result"]["registration"]["receipt"]["aggregation_scope"] = deepcopy(scope)
        before = deepcopy(selected)
        source = source_view.source_records(self.root, [selected])[0]["source"]
        self.assertEqual(source["aggregation_scope"], scope)
        serialized = json.dumps(scope, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        self.assertEqual(source["aggregation_scope_sha256"], hashlib.sha256(serialized.encode()).hexdigest())
        self.assertEqual(selected, before)

    def test_scope_cannot_come_from_source_values_or_model_proposals(self):
        selected = self.selected(mapping="canonical_csv_v2")
        scope = {"scope_version": "1", "timezone": "Asia/Shanghai",
                 "selection_conditions": "全部配送方式；该店全部商品", "reviewed_by": "model_generated"}
        selected["result"]["proposals"] = {"aggregation_scope": scope}
        selected["result"]["metadata"]["aggregation_scope"] = scope
        source = source_view.source_records(self.root, [selected])[0]["source"]
        self.assertIsNone(source["aggregation_scope"])
        self.assertIsNone(source["aggregation_scope_sha256"])
        selected["result"]["preview"]["validated_records"][0]["record"]["aggregation_scope"] = scope
        with self.assertRaisesRegex(ValueError, "canonical record fields"):
            source_view.source_records(self.root, [selected])

    def test_scope_requires_a_valid_reviewed_v2_object(self):
        valid = {"scope_version": "1", "timezone": "Asia/Shanghai",
                 "selection_conditions": "无其他筛选条件", "reviewed_by": "synthetic_operator"}
        for mapping, scope in (("canonical_csv_v1", valid), ("canonical_csv_v2", None),
                               ("canonical_csv_v2", {**valid, "timezone": "unknown_zone"}),
                               ("canonical_csv_v2", {**valid, "selection_conditions": ""})):
            selected = self.selected(mapping=mapping)
            selected["result"]["registration"]["receipt"]["aggregation_scope"] = scope
            with self.subTest(mapping=mapping, scope=scope), self.assertRaises(ValueError):
                source_view.source_records(self.root, [selected])

    def test_monthly_daily_and_partial_source_windows_stay_separate_without_backfill(self):
        monthly = self.selected()
        daily = self.selected(start="2026-03-15", end="2026-03-15", month=None, mapping="canonical_csv_v2")
        partial = self.selected(start="2026-02-15", end="2026-03-14", month=None, mapping="canonical_csv_v2")
        records = source_view.source_records(self.root, [daily, monthly, partial])
        self.assertEqual([row["record"]["period_start"] for row in records],
                         ["2026-02-15", "2026-03-01", "2026-03-15"])
        self.assertEqual([row["record"]["period_month"] for row in records], [None, "2026-03", None])
        self.assertEqual(len(records), 3)
        self.assertTrue(all(set(row) == {"dataset_id", "record", "source"} for row in records))

    def test_sku_ranks_and_multiline_names_are_preserved_in_source_order_identity(self):
        dataset = "demo2_top_skus_by_sales_volume"
        selected = self.selected(dataset=dataset, rows=[
            {"sku_rank": "3", "sku_name": "同名\n商品", "sales_volume": "33"},
            {"sku_rank": "2", "sku_name": "同名\n商品", "sales_volume": "45"},
        ])
        records = source_view.source_records(self.root, [selected])
        self.assertEqual([row["record"]["sku_rank"] for row in records], [2, 3])
        self.assertEqual([row["record"]["sales_volume"] for row in records], [45, 33])
        self.assertEqual([row["source"]["source_line_end"] for row in records], [5, 3])
        self.assertTrue(all(row["record"]["sku_transaction_amount"] is None for row in records))

    def test_dataset_identity_is_not_projected_to_another_view(self):
        panel = self.selected(dataset="store_period_panel_metrics")
        rows = source_view.source_records(self.root, [panel])
        self.assertEqual([row["dataset_id"] for row in rows], ["store_period_panel_metrics"])

    def test_build_writes_one_deterministic_artifact(self):
        first = self.selected()
        second = self.selected(store="C")
        before = {path.relative_to(self.root) for path in self.root.rglob("*") if path.is_file()}
        summary = source_view.build_source_view(self.root, [second, first])
        path = self.root / source_view.RECORDS_PATH
        data = path.read_bytes()
        self.assertEqual(summary, {"profile": "source_records_v1", "records_path": source_view.RECORDS_PATH,
                                   "record_count": 2, "datasets": ["demo2_store_period_metrics"]})
        self.assertEqual(source_view.build_source_view(self.root, [first, second]), summary)
        self.assertEqual(path.read_bytes(), data)
        after = {path.relative_to(self.root) for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(after - before, {Path(source_view.RECORDS_PATH)})

    def test_rejects_incomplete_or_unregistered_record_and_invalid_numeric_representation(self):
        for mutation in (
            lambda record: record.pop("entry_users"),
            lambda record: record.update(gross_revenue="1"),
            lambda record: record.update(transaction_orders=False),
            lambda record: record.update(transaction_amount=9007199254740993.01),
            lambda record: record.update(transaction_amount="123"),
        ):
            selected = self.selected()
            mutation(selected["result"]["preview"]["validated_records"][0]["record"])
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                source_view.source_records(self.root, [selected])

    def test_rejects_mismatched_registration_metadata_and_source_digest(self):
        for mutation in (
            lambda result: result["metadata"].update(dataset_id="store_period_panel_metrics"),
            lambda result: result["metadata"].update(coverage_end="2026-03-30"),
            lambda result: result["metadata"].update(source_system="another_source"),
            lambda result: result["metadata"].update(mapping_version="another_mapping"),
            lambda result: result["registration"]["binding"].update(store_id="C"),
            lambda result: result["registration"]["receipt"].update(source_page="unreviewed_page"),
            lambda result: result["preview"].update(file_sha256="0" * 64),
            lambda result: result["preview"].update(status="quarantined"),
            lambda result: result["preview"].update(errors=["pending check"]),
        ):
            selected = self.selected()
            mutation(selected["result"])
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                source_view.source_records(self.root, [selected])

    def test_rejects_wrong_source_line_missing_rows_and_duplicate_logical_keys(self):
        selected = self.selected(dataset="demo2_top_skus_by_sales_volume", rows=[
            {"sku_rank": "1", "sku_name": "商品一", "sales_volume": "1"},
            {"sku_rank": "2", "sku_name": "商品二", "sales_volume": "2"},
        ])
        for mode in ("wrong_line", "missing_row", "same_line", "same_batch", "same_key"):
            items = [deepcopy(selected)]
            records = items[0]["result"]["preview"]["validated_records"]
            if mode == "wrong_line":
                records[0]["source_line_end"] = 3
            elif mode == "missing_row":
                records.pop()
            elif mode == "same_line":
                records[1]["source_line_end"] = 2
            elif mode == "same_batch":
                items.append(deepcopy(selected))
            else:
                other = deepcopy(selected)
                other["result"]["batch_id"] = "batch_" + "f" * 32
                other["result"]["metadata"]["batch_id"] = other["result"]["batch_id"]
                items.append(other)
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                source_view.source_records(self.root, items)

    def test_rejects_context_that_disagrees_with_the_actual_source_rows(self):
        selected = self.selected()
        result = selected["result"]
        result["registration"]["context"]["store_id"] = "C"
        result["preview"]["context"]["store_id"] = "C"
        result["registration"]["binding"]["store_id"] = "C"
        with self.assertRaisesRegex(ValueError, "different store"):
            source_view.source_records(self.root, [selected])


if __name__ == "__main__":
    unittest.main()
