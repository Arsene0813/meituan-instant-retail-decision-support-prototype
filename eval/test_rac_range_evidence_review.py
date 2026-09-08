"""RAC judgments must change with verified window values and their scope."""
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from rac.src import range_evidence_review as review
from retail_ops.ingestion import source_query
from retail_ops.ingestion.preview import SCHEMAS


ROOT = Path(__file__).resolve().parents[1]
DATASET = "demo2_store_period_metrics"
SCOPE = {"scope_version": "1", "timezone": "Asia/Shanghai",
         "selection_conditions": "Synthetic all channels and products, no extra filters.",
         "reviewed_by": "synthetic-review-operator"}


class RangeEvidenceReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for relative in ("retail_ops/contracts/datasets.v1.json", source_query.POLICY_PATH,
                         review.POLICY_PATH, review.SCHEMA_PATH):
            (self.root / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, self.root / relative)
        (self.root / source_query.RECORDS_PATH).parent.mkdir(parents=True, exist_ok=True)
        self.entries = []

    def entry(self, start="2026-03-01", end=None, store="B", scope=SCOPE, **values):
        end = end or start
        record = dict.fromkeys(SCHEMAS[DATASET])
        record.update(store_id=store, period_start=start, period_end=end,
                      period_month=start[:7] if source_query.month_dates(start[:7]) == (start, end) else None)
        record.update(values)
        return {"dataset_id": DATASET, "record": record, "source": {
            "batch_id": "batch_" + start.replace("-", "") + store, "source_line_end": 2,
            "binding_id": "binding_" + store, "source_system": "meituan_merchant_backend",
            "source_account_id": "synthetic_account", "source_store_id": "source_" + store,
            "source_page": "synthetic-transactions", "extracted_at": "2026-04-30T01:00:00+08:00",
            "file_sha256": "a" * 64, "mapping_version": "canonical_csv_v2",
            "aggregation_scope": deepcopy(scope), "aggregation_scope_sha256": source_query.aggregation_scope_sha256(scope)}}

    def query(self, start="2026-03-01", end=None, stores=None, fields=None):
        (self.root / source_query.RECORDS_PATH).write_text(json.dumps(self.entries), encoding="utf-8")
        @contextmanager
        def opened(*args):
            yield SimpleNamespace(root=self.root, publication_id="publication_" + "a" * 64, manifest={"summary": {
                "profile": "source_records_v1", "records_path": source_query.RECORDS_PATH,
                "record_count": len(self.entries)}})
        with patch.object(source_query, "open_publication", opened):
            return source_query.query_publication(self.root, self.root, "publication_" + "a" * 64, DATASET,
                stores or ["B"], start, end or start, fields or ["transaction_amount", "transaction_orders"])

    def state(self, current, baseline=None):
        state = review.review_range_queries(current, baseline, root=self.root)
        review.validate_range_state(state, current, baseline, root=self.root)
        return state

    @staticmethod
    def comparison(state, field="transaction_amount"):
        return next(item for item in state["checks"] if item["field"] == field and item["operation"] == "compare")

    def test_factual_single_range_preserves_zero_null_and_lineage(self):
        self.entries = [self.entry(transaction_amount="0.00", transaction_orders=None)]
        state = self.state(self.query())
        values = {item["field"]: item for item in state["evidence_packets"]}
        self.assertEqual(values["transaction_amount"]["value"], "0.00")
        self.assertIsNone(values["transaction_orders"]["value"])
        self.assertEqual(values["transaction_orders"]["reason"], "missing_reported_value")
        self.assertEqual(values["transaction_amount"]["records"][0]["source"], self.entries[0]["source"])
        self.assertEqual(state["belief_update"]["status"], "tentative")
        self.assertTrue(all(item["confidence"] is None for item in state["hypotheses"]))

    def test_new_values_reverse_hypotheses_belief_and_rendered_direction(self):
        self.entries = [self.entry(transaction_amount="50", transaction_orders=5),
                        self.entry("2026-03-02", transaction_amount="100", transaction_orders=10)]
        baseline = self.query()
        old = self.state(self.query("2026-03-02"), baseline)
        self.entries[1]["record"].update(transaction_amount="1", transaction_orders=1)
        new = self.state(self.query("2026-03-02"), baseline)
        self.assertEqual(self.comparison(old)["result"], "increased")
        self.assertEqual(self.comparison(new)["result"], "decreased")
        self.assertEqual(self.comparison(new)["difference"], "-49")
        for key in ("hypotheses", "belief_update", "final_report"):
            self.assertNotEqual(old[key], new[key])
        supported = [item for item in new["hypotheses"] if item["status"] == "supported"]
        self.assertTrue(any(item["hypothesis_id"].endswith("/decreased") for item in supported))
        self.assertFalse(any(item["hypothesis_id"].endswith("/increased") for item in supported))

    def test_huge_decimal_and_integer_differences_are_exact(self):
        self.entries = [self.entry(transaction_amount="900719925474099312345678901234567890.123456789", transaction_orders=9007199254740993),
                        self.entry("2026-03-02", transaction_amount="900719925474099312345678901234567890.123456788", transaction_orders=9007199254740994)]
        state = self.state(self.query("2026-03-02"), self.query())
        self.assertEqual(self.comparison(state)["difference"], "-0.000000001")
        self.assertEqual(self.comparison(state, "transaction_orders")["difference"], "1")
        hypothesis = next(item for item in state["hypotheses"] if item["hypothesis_id"] == "transaction_measures_align")
        self.assertEqual(hypothesis["status"], "contradicted")

    def test_null_replacement_does_not_reuse_prior_value_or_direction(self):
        self.entries = [self.entry(transaction_amount="20", transaction_orders=0),
                        self.entry("2026-03-02", transaction_amount=None, transaction_orders=0)]
        state = self.state(self.query("2026-03-02"), self.query())
        self.assertEqual(self.comparison(state)["result"], "unresolved")
        self.assertIsNone(self.comparison(state)["difference"])
        self.assertEqual(self.comparison(state, "transaction_orders")["result"], "unchanged")

    def test_scope_unreviewed_keeps_reported_values_and_holds_comparison(self):
        self.entries = [self.entry(transaction_amount="20", transaction_orders=2, scope=None),
                        self.entry("2026-03-02", transaction_amount="30", transaction_orders=3, scope=None)]
        state = self.state(self.query("2026-03-02"), self.query())
        self.assertIn("comparison_scope_unreviewed", self.comparison(state)["reasons"])
        self.assertEqual([item["value"] for item in state["evidence_packets"] if item["field"] == "transaction_amount"], ["20", "30"])
        self.assertIn("transaction_amount：20。", state["final_report"])
        self.assertIn("transaction_amount：30。", state["final_report"])

    def test_different_filters_timezone_page_or_same_store_identity_hold_comparison(self):
        for change in ("selection_conditions", "timezone", "source_page", "binding_id", "source_account_id", "source_store_id"):
            with self.subTest(change=change):
                other = self.entry("2026-03-02", transaction_amount="30", transaction_orders=3)
                if change in {"selection_conditions", "timezone"}:
                    other["source"]["aggregation_scope"][change] = "UTC" if change == "timezone" else "Synthetic paid items only."
                    other["source"]["aggregation_scope_sha256"] = source_query.aggregation_scope_sha256(other["source"]["aggregation_scope"])
                else:
                    other["source"][change] = "changed_identity_or_page"
                self.entries = [self.entry(transaction_amount="20", transaction_orders=2), other]
                state = self.state(self.query("2026-03-02"), self.query())
                self.assertIn("different_comparison_scopes", self.comparison(state)["reasons"])

    def test_reviewer_identity_does_not_change_semantic_scope(self):
        other_scope = {**SCOPE, "reviewed_by": "another-synthetic-operator"}
        self.entries = [self.entry(transaction_amount="20", transaction_orders=2),
                        self.entry("2026-03-02", transaction_amount="30", transaction_orders=3, scope=other_scope)]
        state = self.state(self.query("2026-03-02"), self.query())
        self.assertEqual(self.comparison(state)["result"], "increased")
        digests = {item["records"][0]["source"]["aggregation_scope_sha256"] for item in state["evidence_packets"]}
        self.assertEqual(len(digests), 2)

    def test_unequal_month_lengths_keep_total_direction_and_record_lengths(self):
        self.entries = [self.entry(end="2026-03-31", transaction_amount="310", transaction_orders=31),
                        self.entry("2026-04-01", end="2026-04-30", transaction_amount="300", transaction_orders=30)]
        state = self.state(self.query("2026-04-01", "2026-04-30"), self.query(end="2026-03-31"))
        self.assertEqual(self.comparison(state)["result"], "decreased")
        self.assertEqual([item["days"] for item in state["scope"]["windows"]], [31, 30])
        self.assertIn("different_window_lengths", [item["code"] for item in state["critic_findings"]])

    def test_reported_amount_can_compare_to_complete_daily_sum_under_same_scope(self):
        self.entries = [self.entry(end="2026-03-02", transaction_amount="10", transaction_orders=2),
                        self.entry("2026-03-03", transaction_amount="7", transaction_orders=1),
                        self.entry("2026-03-04", transaction_amount="8", transaction_orders=2)]
        state = self.state(self.query("2026-03-03", "2026-03-04"), self.query(end="2026-03-02"))
        self.assertEqual(self.comparison(state)["difference"], "5")
        self.assertEqual({item["basis"] for item in state["evidence_packets"]}, {"reported_window", "sum_non_overlapping_days"})

    def test_missing_day_cannot_become_a_decline_judgment(self):
        self.entries = [self.entry(transaction_amount="10", transaction_orders=1),
                        self.entry("2026-03-03", transaction_amount="7", transaction_orders=1)]
        state = self.state(self.query("2026-03-02", "2026-03-03"), self.query())
        self.assertEqual(self.comparison(state)["result"], "unresolved")
        self.assertIn("missing_dates", self.comparison(state)["reasons"][0])

    def test_search_and_activity_hypotheses_recompute_when_signs_diverge(self):
        fields = ["transaction_amount", "transaction_orders", "search_exposure_users", "activity_orders"]
        self.entries = [self.entry(transaction_amount="20", transaction_orders=2, search_exposure_users=10, activity_orders=1),
                        self.entry("2026-03-02", transaction_amount="30", transaction_orders=3, search_exposure_users=20, activity_orders=2)]
        old = self.state(self.query("2026-03-02", fields=fields), self.query(fields=fields))
        self.entries[1]["record"].update(search_exposure_users=1, activity_orders=0)
        new = self.state(self.query("2026-03-02", fields=fields), self.query(fields=fields))
        for identifier in ("search_transaction_align", "activity_orders_align"):
            self.assertEqual(next(item for item in old["hypotheses"] if item["hypothesis_id"] == identifier)["status"], "supported")
            self.assertEqual(next(item for item in new["hypotheses"] if item["hypothesis_id"] == identifier)["status"], "contradicted")

    def test_cross_store_same_period_checks_values_without_ranking_stores(self):
        self.entries = [self.entry(transaction_amount="20", transaction_orders=2, store="B"),
                        self.entry(transaction_amount="30", transaction_orders=1, store="C")]
        state = self.state(self.query(stores=["C", "B"]))
        self.assertEqual(self.comparison(state)["result"], "increased")
        self.assertEqual(self.comparison(state, "transaction_orders")["result"], "decreased")
        self.assertIn("cross_store_context_unresolved", [item["code"] for item in state["critic_findings"]])
        self.assertNotIn("best_store", state)

    def test_background_fields_remain_raw_values_without_direction_hypotheses(self):
        self.entries = [self.entry(estimated_income_proxy="20", refund_amount="2"),
                        self.entry("2026-03-02", estimated_income_proxy="30", refund_amount="3")]
        fields = ["estimated_income_proxy", "refund_amount"]
        state = self.state(self.query("2026-03-02", fields=fields), self.query(fields=fields))
        self.assertTrue(all(item["operation"] == "record_values" for item in state["checks"]))
        self.assertNotIn("利润", state["final_report"])
        self.assertNotIn("退款原因", state["final_report"])

    def test_store_type_and_region_do_not_become_numeric_or_peer_group(self):
        self.entries = [self.entry(store="B", region_type="城市", store_type="partner"),
                        self.entry(store="C", region_type="城市", store_type="partner")]
        state = self.state(self.query(stores=["B", "C"], fields=["region_type", "store_type"]))
        self.assertTrue(all(item["operation"] == "record_values" for item in state["checks"]))
        self.assertEqual([item["value"] for item in state["evidence_packets"]], ["城市", "partner", "城市", "partner"])

    def test_mixed_publication_dataset_fields_store_or_overlapping_windows_rejected(self):
        self.entries = [self.entry(transaction_amount="20", transaction_orders=2),
                        self.entry("2026-03-02", transaction_amount="30", transaction_orders=3)]
        baseline, current = self.query(), self.query("2026-03-02")
        changed = deepcopy(baseline)
        changed["publication_id"] = "publication_" + "b" * 64
        with self.assertRaisesRegex(ValueError, "publication_id must match"):
            self.state(current, changed)
        with self.assertRaisesRegex(ValueError, "fields must match"):
            self.state(current, self.query(fields=["transaction_amount"]))
        with self.assertRaisesRegex(ValueError, "same single store"):
            self.state(self.query("2026-03-02", stores=["C"]), baseline)
        with self.assertRaisesRegex(ValueError, "baseline must finish"):
            self.state(baseline, baseline)

    def test_tampered_query_summary_coverage_and_source_type_rejected(self):
        self.entries = [self.entry(transaction_amount="20", transaction_orders=2)]
        original = self.query()
        changes = [lambda query: query["stores"][0]["metrics"]["transaction_amount"].update(value="999"),
                   lambda query: query["stores"][0]["coverage"].update(covered_days=0),
                   lambda query: query["stores"][0]["records"][0]["record"].update(transaction_orders=2.5),
                   lambda query: query["stores"][0]["records"][0]["source"].update(aggregation_scope_sha256="b"*64)]
        for mutate in changes:
            changed = deepcopy(original)
            mutate(changed)
            with self.assertRaises(ValueError):
                self.state(changed)

    def test_stale_decision_fields_and_numeric_confidence_are_rejected(self):
        self.entries = [self.entry(transaction_amount="20", transaction_orders=2)]
        query = self.query()
        original = self.state(query)
        changes = [lambda state: state["hypotheses"][0].update(status="contradicted"),
                   lambda state: state["belief_update"].update(claim="old judgment"),
                   lambda state: state.update(final_report="old report"),
                   lambda state: state["hypotheses"][0].update(confidence=0.9)]
        for mutate in changes:
            changed = deepcopy(original)
            mutate(changed)
            with self.assertRaises(ValueError):
                review.validate_range_state(changed, query, root=self.root)

    def test_changed_policy_is_rejected_before_judgment(self):
        self.entries = [self.entry(transaction_amount="20", transaction_orders=2)]
        policy = deepcopy(review.EXPECTED_POLICY)
        policy["other_fields"] = "guess"
        (self.root / review.POLICY_PATH).write_text(json.dumps(policy))
        with self.assertRaisesRegex(ValueError, "policy differs"):
            self.state(self.query())

    def test_default_review_selects_only_registered_transaction_fields(self):
        self.assertEqual(review.default_review_fields(self.root), ["transaction_amount", "transaction_orders"])
        policy = deepcopy(review.EXPECTED_POLICY)
        policy["default_fields"].append("estimated_income_proxy")
        (self.root / review.POLICY_PATH).write_text(json.dumps(policy))
        with self.assertRaisesRegex(ValueError, "policy differs"):
            review.default_review_fields(self.root)

    def test_percent_field_difference_is_labelled_percentage_points(self):
        self.entries = [self.entry(order_conversion_rate_pct="20"),
                        self.entry("2026-03-02", order_conversion_rate_pct="25.5")]
        state = self.state(self.query("2026-03-02", fields=["order_conversion_rate_pct"]),
                           self.query(fields=["order_conversion_rate_pct"]))
        check = self.comparison(state, "order_conversion_rate_pct")
        self.assertEqual(check["difference"], "5.5")
        self.assertIn("5.5 个百分点", check["claim"])
        self.assertIn("2026-03-02", next(item for item in state["hypotheses"]
                                       if item["hypothesis_id"].endswith("/increased"))["claim"])

    def test_duplicate_policy_and_schema_keys_are_rejected(self):
        self.entries = [self.entry(transaction_amount="20", transaction_orders=2)]
        query = self.query()
        state = self.state(query)
        policy = self.root / review.POLICY_PATH
        original = policy.read_text()
        policy.write_text('{"confidence":"not_estimated",' + original[1:])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.state(query)
        policy.write_text(original)
        schema = self.root / review.SCHEMA_PATH
        schema.write_text('{"title":"RACRangeCognitionStateV1",' + schema.read_text()[1:])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            review.validate_range_state(state, query, root=self.root)

    def test_invalid_publication_id_and_store_whitespace_are_rejected(self):
        self.entries = [self.entry(transaction_amount="20", transaction_orders=2)]
        query = self.query()
        invalid = deepcopy(query)
        invalid["publication_id"] = "../publication"
        with self.assertRaisesRegex(ValueError, "publication ID"):
            self.state(invalid)
        invalid = deepcopy(query)
        invalid["stores"][0]["store_id"] = " B"
        with self.assertRaisesRegex(ValueError, "invalid range RAC store"):
            self.state(invalid)

    def test_month_preset_and_same_dates_produce_identical_state_without_mutation(self):
        self.entries = [self.entry(end="2026-03-31", transaction_amount="31", transaction_orders=1)]
        dates = source_query.month_dates("2026-03")
        query = self.query(*dates)
        original = deepcopy(query)
        state = self.state(query)
        self.assertEqual(state, self.state(self.query("2026-03-01", "2026-03-31")))
        self.assertEqual(query, original)


if __name__ == "__main__":
    unittest.main()
