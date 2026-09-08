"""Read-only date selection and conservative source-field summaries."""
from contextlib import contextmanager, redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from retail_ops.ingestion import source_query as query
from retail_ops.ingestion.preview import SCHEMAS

ROOT = Path(__file__).resolve().parents[1]
DATASET = "demo2_store_period_metrics"
# Synthetic operator-reviewed scope, unrelated to a real backend account.
SCOPE = {"scope_version": "1", "timezone": "Asia/Shanghai",
         "selection_conditions": "all transaction categories; no extra filters", "reviewed_by": "unit_test_operator"}


class SourceQueryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in ("retail_ops/contracts/datasets.v1.json", query.POLICY_PATH):
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, self.root / name)
        (self.root / query.RECORDS_PATH).parent.mkdir(parents=True, exist_ok=True)
        self.summary = {"profile": "source_records_v1", "records_path": query.RECORDS_PATH,
                        "record_count": 0, "datasets": []}
        self.open_count = 0
        @contextmanager
        def opened(*args):
            self.open_count += 1
            yield SimpleNamespace(root=self.root, publication_id="publication_test",
                                  manifest={"summary": self.summary})
        self.addCleanup(patch.stopall)
        patch.object(query, "open_publication", opened).start()

    def entry(self, start="2026-03-01", end=None, *, store="B", dataset=DATASET,
              batch=None, binding="binding_B", account="account1", page="transactions", line=2, scope=SCOPE, **values):
        end = end or start
        record = dict.fromkeys(SCHEMAS[dataset])
        record.update(store_id=store, period_start=start, period_end=end,
                      period_month=start[:7] if (start, end) == query.month_dates(start[:7]) else None)
        record.update(values)
        return {"dataset_id": dataset, "record": record, "source": {
            "batch_id": batch or "batch_" + start.replace("-", ""), "source_line_end": line,
            "binding_id": binding, "source_system": "meituan_merchant_backend",
            "source_account_id": account, "source_store_id": "source_" + store,
            "source_page": page, "extracted_at": "2026-04-01T00:00:00+08:00",
            "file_sha256": "a" * 64, "mapping_version": "canonical_csv_v2",
            "aggregation_scope": None if scope is None else dict(scope),
            "aggregation_scope_sha256": query.aggregation_scope_sha256(scope)}}

    def save(self, entries):
        self.summary["record_count"] = len(entries)
        self.summary["datasets"] = sorted({entry["dataset_id"] for entry in entries})
        (self.root / query.RECORDS_PATH).write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")

    def read(self, *, stores=None, dataset=DATASET, start="2026-03-01", end="2026-03-02", fields=None):
        return query.query_publication(ROOT, self.root, "publication_test", dataset,
                                       stores or ["B"], start, end, fields)

    def test_daily_complete_exact_amount_and_zero_order_sum(self):
        self.save([self.entry(transaction_amount="10.00", transaction_orders=0),
                   self.entry("2026-03-02", transaction_amount="0.00", transaction_orders=3)])
        result = self.read(fields=["transaction_orders", "transaction_amount"])
        store = result["stores"][0]
        self.assertEqual(store["metrics"]["transaction_amount"]["value"], "10.00")
        self.assertEqual(store["metrics"]["transaction_orders"]["value"], 3)
        self.assertEqual(store["metrics"]["transaction_orders"]["basis"], "sum_non_overlapping_days")
        self.assertEqual(store["coverage"], {"requested_days": 2, "covered_days": 2, "missing_ranges": []})
        self.assertEqual(store["records"][0]["record"]["transaction_orders"], 0)
        self.assertEqual(self.open_count, 1)

    def test_daily_sums_do_not_round_large_integers_or_decimals(self):
        self.save([self.entry(transaction_amount="900719925474099312345678901234567890.123456789", transaction_orders=9007199254740993),
                   self.entry("2026-03-02", transaction_amount="0.000000001", transaction_orders=1)])
        metrics = self.read()["stores"][0]["metrics"]
        self.assertEqual(metrics["transaction_amount"]["value"], "900719925474099312345678901234567890.123456790")
        self.assertEqual(metrics["transaction_orders"]["value"], 9007199254740994)

    def test_missing_day_does_not_become_zero_or_partial_total(self):
        self.save([self.entry(transaction_amount="10", transaction_orders=5)])
        store = self.read(end="2026-03-05")["stores"][0]
        self.assertEqual(store["coverage"]["missing_ranges"], [{"period_start": "2026-03-02", "period_end": "2026-03-05"}])
        self.assertIsNone(store["metrics"]["transaction_amount"]["value"])
        self.assertEqual(store["metrics"]["transaction_amount"]["reason"], "missing_dates")

    def test_null_metric_coverage_is_distinct_from_date_coverage(self):
        self.save([self.entry(transaction_amount="10", transaction_orders=0),
                   self.entry("2026-03-02", transaction_amount=None, transaction_orders=0)])
        store = self.read()["stores"][0]
        self.assertEqual(store["coverage"]["missing_ranges"], [])
        amount = store["metrics"]["transaction_amount"]
        self.assertIsNone(amount["value"])
        self.assertEqual(amount["reason"], "missing_metric_values")
        self.assertEqual(amount["coverage"]["null_source_records"], 1)
        self.assertEqual(amount["coverage"]["missing_ranges"], [{"period_start": "2026-03-02", "period_end": "2026-03-02"}])
        self.assertEqual(store["metrics"]["transaction_orders"]["value"], 0)

    def test_user_counts_rates_rank_and_background_fields_never_sum(self):
        fields = ["entry_users", "order_conversion_rate_pct", "store_average_rank", "estimated_income_proxy", "average_order_value"]
        values = dict(entry_users=2, order_conversion_rate_pct="50", store_average_rank="1", estimated_income_proxy="10", average_order_value="5")
        self.save([self.entry(**values), self.entry("2026-03-02", **values)])
        store = self.read(fields=fields)["stores"][0]
        for field in fields:
            with self.subTest(field=field):
                self.assertIsNone(store["metrics"][field]["value"])
                self.assertEqual(store["metrics"][field]["reason"], "aggregation_not_registered")
                self.assertEqual(store["records"][0]["record"][field], values[field])

    def test_one_exact_month_reports_all_values_unchanged(self):
        self.save([self.entry(end="2026-03-31", transaction_amount="100.00", transaction_orders=0,
                              entry_users=10, order_conversion_rate_pct="35.50", region_type="城市")])
        store = self.read(end="2026-03-31")["stores"][0]
        for field, value in {"transaction_amount": "100.00", "transaction_orders": 0,
                             "entry_users": 10, "order_conversion_rate_pct": "35.50", "region_type": "城市"}.items():
            self.assertEqual(store["metrics"][field]["value"], value)
            self.assertEqual(store["metrics"][field]["basis"], "reported_window")
        self.assertIsNone(store["metrics"]["exposure_users"]["value"])
        self.assertEqual(store["metrics"]["exposure_users"]["reason"], "missing_reported_value")

    def test_partially_overlapping_month_is_shown_without_proration(self):
        self.save([self.entry(end="2026-03-31", transaction_amount="310")])
        store = self.read(start="2026-03-10", end="2026-03-12")["stores"][0]
        self.assertEqual(store["records"], [])
        self.assertEqual(store["overlapping_records"][0]["record"]["transaction_amount"], "310")
        self.assertEqual(store["coverage"]["covered_days"], 0)
        self.assertIsNone(store["metrics"]["transaction_amount"]["value"])
        self.assertIn("partially_overlapping_source_windows", store["ambiguities"])

    def test_multiple_coarse_windows_are_not_summed(self):
        self.save([self.entry(end="2026-03-15", transaction_amount="150"),
                   self.entry("2026-03-16", end="2026-03-31", transaction_amount="160")])
        store = self.read(end="2026-03-31")["stores"][0]
        self.assertEqual(store["coverage"]["covered_days"], 31)
        self.assertIsNone(store["metrics"]["transaction_amount"]["value"])
        self.assertEqual(store["metrics"]["transaction_amount"]["reason"], "aggregation_requires_daily_source_records")

    def test_monthly_and_daily_overlap_have_no_automatic_priority(self):
        self.save([self.entry(end="2026-03-31", batch="batch_month", transaction_amount="310"),
                   self.entry(transaction_amount="10")])
        store = self.read(end="2026-03-31")["stores"][0]
        self.assertEqual(len(store["records"]), 2)
        self.assertIn("overlapping_source_windows", store["ambiguities"])
        self.assertEqual(len(store["source_window_overlaps"]), 1)
        self.assertIsNone(store["metrics"]["transaction_amount"]["value"])

    def test_same_day_different_batches_are_ambiguous(self):
        self.save([self.entry(batch="batch_old", transaction_amount="10"),
                   self.entry(batch="batch_new", transaction_amount="20")])
        store = self.read(end="2026-03-01")["stores"][0]
        self.assertIsNone(store["metrics"]["transaction_amount"]["value"])
        self.assertIn("overlapping_source_windows", store["ambiguities"])

    def test_different_source_scopes_block_nonoverlapping_daily_sum(self):
        self.save([self.entry(transaction_amount="10"),
                   self.entry("2026-03-02", account="account2", transaction_amount="20")])
        store = self.read()["stores"][0]
        self.assertEqual(store["coverage"]["covered_days"], 2)
        self.assertEqual(len(store["source_scopes"]), 2)
        self.assertIn("different_source_scopes", store["ambiguities"])
        self.assertIsNone(store["metrics"]["transaction_amount"]["value"])

    def test_duplicate_record_in_one_batch_cannot_create_a_daily_sum(self):
        self.save([self.entry(transaction_amount="10"), self.entry(transaction_amount="10", line=3),
                   self.entry("2026-03-02", transaction_amount="20")])
        metric = self.read()["stores"][0]["metrics"]["transaction_amount"]
        self.assertIsNone(metric["value"])
        self.assertEqual(metric["reason"], "duplicate_daily_records")

    def test_ranks_share_a_report_window_without_appearing_as_overlap(self):
        dataset = "demo2_top_skus_by_transaction_amount"
        self.save([self.entry(dataset=dataset, end="2026-03-31", sku_rank=rank,
                              sku_name="同名商品", sku_transaction_amount=str(rank * 10), line=rank + 1)
                   for rank in (1, 2, 3)])
        store = self.read(dataset=dataset, end="2026-03-31")["stores"][0]
        self.assertEqual(store["coverage"]["covered_days"], 31)
        self.assertEqual(store["source_window_overlaps"], [])
        self.assertEqual([item["record"]["sku_rank"] for item in store["records"]], [1, 2, 3])
        self.assertIsNone(store["metrics"]["sku_transaction_amount"]["value"])
        self.assertEqual(store["metrics"]["sku_transaction_amount"]["reason"], "ranked_records_only")

    def test_same_named_sku_on_different_days_is_not_merged(self):
        dataset = "demo2_top_skus_by_sales_volume"
        self.save([self.entry(dataset=dataset, sku_rank=1, sku_name="商品", sales_volume=2),
                   self.entry("2026-03-02", dataset=dataset, sku_rank=1, sku_name="商品", sales_volume=3)])
        store = self.read(dataset=dataset)["stores"][0]
        self.assertEqual(len(store["records"]), 2)
        self.assertIsNone(store["metrics"]["sales_volume"]["value"])

    def test_search_ranking_retains_reported_zero_without_total(self):
        dataset = "demo2_top_search_terms"
        self.save([self.entry(dataset=dataset, search_term_rank=1, search_term="美瞳", search_term_order_times=0)])
        store = self.read(dataset=dataset, end="2026-03-01")["stores"][0]
        self.assertEqual(store["records"][0]["record"]["search_term_order_times"], 0)
        self.assertIsNone(store["metrics"]["search_term_order_times"]["value"])

    def test_cross_store_returns_each_scope_and_missing_store_without_total(self):
        self.save([self.entry(store="B", transaction_amount="10"),
                   self.entry("2026-03-02", store="B", transaction_amount="0"),
                   self.entry(store="C", transaction_amount="20"),
                   self.entry(store="D", transaction_amount="999")])
        result = self.read(stores=["C", "B", "E"])
        self.assertEqual([store["store_id"] for store in result["stores"]], ["B", "C", "E"])
        self.assertEqual(result["stores"][0]["metrics"]["transaction_amount"]["value"], "10")
        self.assertIsNone(result["stores"][1]["metrics"]["transaction_amount"]["value"])
        self.assertEqual(result["stores"][2]["records"], [])
        self.assertEqual(set(result), {"publication_id", "dataset_id", "period_start", "period_end", "fields", "stores"})

    def test_store_input_is_parameterized_not_sql(self):
        malicious = "B') OR 1=1 --"
        self.save([self.entry(transaction_amount="10")])
        store = self.read(stores=[malicious])["stores"][0]
        self.assertEqual(store["store_id"], malicious)
        self.assertEqual(store["records"], [])

    def test_unregistered_fields_and_datasets_never_fall_back(self):
        self.save([])
        for fields in (["gross_revenue"], ["transaction_amount", "transaction_amount"], ["store_id"], []):
            with self.subTest(fields=fields), self.assertRaisesRegex(ValueError, "registered non-key"):
                self.read(fields=fields)
        with self.assertRaisesRegex(ValueError, "registered contract"):
            self.read(dataset="similar_store_metrics")

    def test_invalid_date_or_duplicate_store_query_is_rejected(self):
        self.save([])
        for args in ({"start": "2026-3-01"}, {"start": "2026-03-03"}, {"stores": ["B", "B"]}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.read(**args)
        self.assertEqual(query.month_dates("2024-02"), ("2024-02-01", "2024-02-29"))

    def test_query_at_maximum_date_has_finite_correct_coverage(self):
        self.save([])
        coverage = self.read(start="9999-12-31", end="9999-12-31")["stores"][0]["coverage"]
        self.assertEqual(coverage, {"requested_days": 1, "covered_days": 0,
                                   "missing_ranges": [{"period_start": "9999-12-31", "period_end": "9999-12-31"}]})

    def test_source_metadata_and_numeric_types_are_checked(self):
        for mutate in (lambda item: item["source"].pop("source_page"),
                       lambda item: item["record"].update(transaction_amount=1.2),
                       lambda item: item["record"].update(transaction_orders=True),
                       lambda item: item["record"].update(period_month="2026-03"),
                       lambda item: item["source"].update(source_system="other_backend"),
                       lambda item: item["source"].update(mapping_version="canonical_csv_v1")):
            item = self.entry(transaction_amount="10")
            mutate(item)
            self.save([item])
            with self.assertRaises(ValueError):
                self.read()

    def test_manifest_record_count_is_checked(self):
        self.save([self.entry(transaction_amount="10")])
        self.summary["record_count"] = 2
        with self.assertRaisesRegex(ValueError, "record count"):
            self.read()

    def test_changed_aggregation_registration_is_rejected(self):
        self.save([])
        policy_path = self.root / query.POLICY_PATH
        original = json.loads(policy_path.read_text())
        replacements = {
            "default_fields": "all_values", "source_scope_fields": [],
            "sum_requirements": [], "exact_window_policy": "prefer_latest",
            "other_field_policy": "sum", "ranking_policy": "merge_skus",
            "partial_overlap_policy": "prorate", "query_storage": "save_query",
            "aggregation_scope_requirement": "trust_same_account",
        }
        for field, replacement in replacements.items():
            policy_path.write_text(json.dumps({**original, field: replacement}))
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "policy changed"):
                self.read()
        original["summary_policies"]["entry_users"] = {"operation": "sum_non_overlapping_days", "grain": "store_period"}
        policy_path.write_text(json.dumps(original))
        with self.assertRaisesRegex(ValueError, "summary fields"):
            self.read()

    def test_month_and_explicit_dates_produce_identical_stdout(self):
        self.save([self.entry(end="2026-03-31", transaction_amount="100")])
        base = ["--directory", str(self.root), "--publication-id", "publication_test",
                "--dataset-id", DATASET, "--store-id", "B", "--field", "transaction_amount"]
        outputs = []
        for args in (["--month", "2026-03"], ["--period-start", "2026-03-01", "--period-end", "2026-03-31"]):
            handle = io.StringIO()
            with redirect_stdout(handle):
                self.assertEqual(query.main(base + args), 0)
            outputs.append(handle.getvalue())
        self.assertEqual(outputs[0], outputs[1])
        self.assertNotIn("query_kind", outputs[0])
        self.assertNotIn("report_window_kind", outputs[0])

    def test_cli_rejects_mixed_window_and_output_saving(self):
        self.save([])
        base = ["--directory", str(self.root), "--publication-id", "publication_test", "--dataset-id", DATASET, "--store-id", "B"]
        for flags in (["--month", "2026-03", "--period-end", "2026-03-31"],
                      ["--period-start", "2026-03-01"],
                      ["--month", "2026-03", "--output", str(self.root / "query.json")]):
            with self.subTest(flags=flags), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                query.main(base + flags)
            self.assertEqual(error.exception.code, 2)
        self.assertFalse((self.root / "query.json").exists())

    def test_unreviewed_scope_keeps_records_but_blocks_daily_sum(self):
        for scope in (None, SCOPE):
            self.save([self.entry(transaction_amount="10", scope=None),
                       self.entry("2026-03-02", transaction_amount="20", scope=scope)])
            store = self.read()["stores"][0]
            self.assertEqual(len(store["records"]), 2)
            self.assertIsNone(store["metrics"]["transaction_amount"]["value"])
            self.assertEqual(store["metrics"]["transaction_amount"]["reason"], "aggregation_scope_unreviewed")

    def test_unknown_scope_allows_exact_reported_window_value(self):
        self.save([self.entry(end="2026-03-31", transaction_amount="100", scope=None)])
        metric = self.read(end="2026-03-31")["stores"][0]["metrics"]["transaction_amount"]
        self.assertEqual(metric["value"], "100")
        self.assertEqual(metric["basis"], "reported_window")

    def test_changed_filter_timezone_or_reviewer_prevents_daily_sum(self):
        for field, value in (("selection_conditions", "delivery category only"),
                             ("timezone", "UTC"), ("reviewed_by", "another_operator")):
            with self.subTest(field=field):
                self.save([self.entry(transaction_amount="10"),
                           self.entry("2026-03-02", transaction_amount="20", scope={**SCOPE, field: value})])
                store = self.read()["stores"][0]
                self.assertIsNone(store["metrics"]["transaction_amount"]["value"])
                self.assertIn("different_source_scopes", store["ambiguities"])

    def test_scope_object_and_digest_must_both_validate(self):
        for mutate in (lambda source: source["aggregation_scope"].update(selection_conditions="different filter"),
                       lambda source: source.update(aggregation_scope_sha256="f" * 64),
                       lambda source: source["aggregation_scope"].update(timezone="invalid/timezone"),
                       lambda source: source.update(aggregation_scope=None),
                       lambda source: source.update(mapping_version="canonical_csv_v1")):
            # A full month otherwise permits v1; the reviewed-scope rule must
            # independently reject that format even when its dates are valid.
            item = self.entry(end="2026-03-31", transaction_amount="10")
            mutate(item["source"])
            self.save([item])
            with self.assertRaises(ValueError):
                self.read()

    def test_query_does_not_write_files_or_read_live_business_paths(self):
        self.save([self.entry(transaction_amount="10")])
        before = {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        original_open = Path.open
        allowed = {self.root / query.RECORDS_PATH, self.root / query.POLICY_PATH,
                   self.root / "retail_ops/contracts/datasets.v1.json"}
        def guarded_open(path, mode="r", *args, **kwargs):
            self.assertIn(path, allowed)
            self.assertFalse(any(flag in mode for flag in "wax+"))
            return original_open(path, mode, *args, **kwargs)
        original_connect = query.sqlite3.connect
        def memory_only(database, *args, **kwargs):
            self.assertEqual(database, ":memory:")
            return original_connect(database, *args, **kwargs)
        with patch.object(Path, "open", guarded_open), patch.object(query.sqlite3, "connect", memory_only), \
                patch.object(Path, "mkdir", side_effect=AssertionError("query attempted directory write")), \
                patch.object(Path, "unlink", side_effect=AssertionError("query attempted delete")):
            self.read()
        after = {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
