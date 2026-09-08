"""End-to-end RAC checks using artificial mutations of copied source files."""

import csv
from copy import deepcopy
import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rac.src import evidence_review
from rac.src.demo2_csv_grounding import REPEATED_WINDOW_SOURCE_PATH, SNAPSHOT_SOURCE_PATH
from rac.src.grounded_pipeline import run_grounded_pipeline
from rac.src.local_evidence_resolver import resolve_evidence_packet
from rac.src.store_a_csv_grounding import SOURCE_PATH
from rac.src.state_validation import CognitionStateValidationError, validate_cognition_state


ROOT = Path(__file__).resolve().parents[1]
PANEL = "retail_ops/data/store_period_panel_metrics.csv"
CAUSAL = "Can Store A's March-to-April increases in transaction amount and transaction orders be attributed to search exposure alone?"
COMPARISON = "Are Stores B-F directly comparable in March 2026?"


class RacEvidenceUpdatesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # These are source copies for synthetic counterexamples. No checked-in
        # business records or generated outputs are rewritten by this suite.
        for folder in ("retail_ops/data", "retail_ops/contracts", "retail_ops/outputs", "rac/schemas"):
            shutil.copytree(ROOT / folder, self.root / folder)
        for filename in ("retail_ops/COMPARABILITY_GATE_V0.md", "rac/README.md"):
            shutil.copyfile(ROOT / filename, self.root / filename)
        self.original_a = (self.root / SOURCE_PATH).read_bytes()

    def run_review(self, question=CAUSAL):
        return run_grounded_pipeline(question, root=self.root)

    def mutate(self, source, transform):
        path = self.root / source
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            fields, rows = reader.fieldnames, list(reader)
        transform(rows)
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    def set_a(self, period, values):
        self.mutate(SOURCE_PATH, lambda rows: next(
            row for row in rows if row["store_id"] == "A" and row["period_month"] == period
        ).update(values))

    @staticmethod
    def checks(state):
        return {item["check_id"]: item for item in state["evidence_review"]["checks"]}

    @staticmethod
    def hypotheses(state):
        return {item["hypothesis_id"]: item for item in state["hypotheses"]}

    @staticmethod
    def decisions(state):
        return {field: state[field] for field in (
            "hypotheses", "critic_findings", "fact_check", "belief_update", "final_report"
        )}

    def hashes(self):
        return {str(path.relative_to(self.root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.root.rglob("*") if path.is_file()}

    def test_baseline_reruns_deterministically_without_writing_sources(self):
        before = self.hashes()
        questions = (
            CAUSAL, COMPARISON,
            "What should be checked before changing promotions for a store?",
            "How should the RAC system be connected to the existing memory layer?",
        )
        for question in questions:
            with self.subTest(question=question):
                first = self.run_review(question)
                self.assertEqual(first, self.run_review(question))
                self.assertIsNone(first["belief_update"]["confidence"])
                self.assertTrue(all(h["confidence"] is None for h in first["hypotheses"]))
        self.assertEqual(before, self.hashes())

    def test_both_outcomes_reversed_recompute_all_decision_sections(self):
        baseline = self.run_review()
        self.set_a("2026-04", {"transaction_amount": "1", "transaction_orders": "1"})
        changed = self.run_review()
        checks = self.checks(changed)
        for field in ("transaction_amount", "transaction_orders"):
            self.assertEqual(checks[field + "/" + field]["result"], "decreased")
        self.assertEqual(self.hypotheses(changed)["H2"]["status"], "rejected")
        self.assertIn("transaction_amount decreased", changed["belief_update"]["claim"])
        self.assertIn("transaction_orders decreased", changed["belief_update"]["claim"])
        self.assertTrue(changed["fact_check"]["unsupported_claims"])
        self.assertEqual(changed["fact_check"]["status"], "pass_with_warnings")
        for field, old in self.decisions(baseline).items():
            self.assertNotEqual(old, changed[field], field)

    def test_mixed_equal_and_zero_outcomes_have_exact_directions(self):
        scenarios = (
            ({"transaction_amount": "10", "transaction_orders": "10"},
             {"transaction_amount": "20", "transaction_orders": "5"}, ("increased", "decreased")),
            ({"transaction_amount": "10.00", "transaction_orders": "10"},
             {"transaction_amount": "10", "transaction_orders": "10"}, ("unchanged", "unchanged")),
            ({"transaction_amount": "0", "transaction_orders": "0"},
             {"transaction_amount": "0", "transaction_orders": "0"}, ("unchanged", "unchanged")),
            ({"transaction_amount": "0", "transaction_orders": "0"},
             {"transaction_amount": "1", "transaction_orders": "1"}, ("increased", "increased")),
        )
        for previous, current, directions in scenarios:
            with self.subTest(previous=previous, current=current):
                (self.root / SOURCE_PATH).write_bytes(self.original_a)
                self.set_a("2026-03", previous)
                self.set_a("2026-04", current)
                state = self.run_review()
                checks = self.checks(state)
                for field, direction in zip(("transaction_amount", "transaction_orders"), directions):
                    check = checks[field + "/" + field]
                    self.assertEqual((check["status"], check["result"]), ("supported", direction))
                    self.assertEqual(check["operands"][0]["value"], previous[field])
                    self.assertIn(f"{field} {direction}", state["belief_update"]["claim"])

    def test_large_integer_and_decimal_comparisons_do_not_round_to_equal(self):
        self.set_a("2026-03", {"transaction_orders": "9007199254740992",
                                "transaction_amount": "1.000000000000000000001"})
        self.set_a("2026-04", {"transaction_orders": "9007199254740993",
                                "transaction_amount": "1.000000000000000000002"})
        checks = self.checks(self.run_review())
        for field in ("transaction_amount", "transaction_orders"):
            self.assertEqual(checks[field + "/" + field]["result"], "increased")

    def test_blank_and_malformed_values_leave_no_stale_positive_outcome(self):
        for value, expected_status in (("", "missing"), ("not-a-number", "invalid")):
            with self.subTest(value=value):
                (self.root / SOURCE_PATH).write_bytes(self.original_a)
                self.set_a("2026-04", {"transaction_amount": value, "transaction_orders": value})
                state = self.run_review()
                checks = self.checks(state)
                for field in ("transaction_amount", "transaction_orders"):
                    check = checks[field + "/" + field]
                    self.assertEqual((check["status"], check["result"]), (expected_status, "unresolved"))
                    self.assertNotIn(f"{field} increased", state["belief_update"]["claim"])
                self.assertEqual(self.hypotheses(state)["H1"]["status"], "unsupported")
                self.assertEqual(state["belief_update"]["status"], "tentative")
                self.assertEqual(state["fact_check"]["status"], "pass_with_warnings")
                self.assertTrue(state["critic_findings"])

    def test_search_reversal_changes_the_co_movement_hypothesis(self):
        baseline = self.run_review()
        self.assertEqual(self.hypotheses(baseline)["H2"]["status"], "plausible")
        self.set_a("2026-04", {"search_exposure_users": "1"})
        changed = self.run_review()
        self.assertEqual(self.checks(changed)["search_exposure/search_exposure_users"]["result"], "decreased")
        self.assertEqual(self.hypotheses(changed)["H2"]["status"], "rejected")
        self.assertNotEqual(self.hypotheses(baseline)["H2"], self.hypotheses(changed)["H2"])

    def test_missing_store_c_record_cannot_be_replaced_with_markdown(self):
        self.mutate(SNAPSHOT_SOURCE_PATH, lambda rows: rows.__setitem__(
            slice(None), [row for row in rows if row["store_id"] != "C"]
        ))
        state = self.run_review(COMPARISON)
        resolved = {item["factor_id"]: item for item in state["grounded_evidence"]["resolved_packets"]}
        period = resolved["same_reporting_period"]
        self.assertEqual(period["source_path"], SNAPSHOT_SOURCE_PATH)
        self.assertEqual(period["grounding_status"], "record_contract_error")
        self.assertTrue(any(item["grounding_status"] == "keyword_matched"
                            for item in period["candidate_sources_checked"]))
        self.assertEqual(self.hypotheses(state)["H1"]["status"], "unsupported")
        self.assertEqual(state["belief_update"]["status"], "tentative")

    def test_changed_panel_requires_reconciling_the_saved_repeated_summary(self):
        self.mutate(PANEL, lambda rows: next(
            row for row in rows if row["store_id"] == "B" and row["period_month"] == "2026-04"
        ).__setitem__("transaction_amount", "1"))
        state = self.run_review(COMPARISON)
        repeated = next(item for item in state["grounded_evidence"]["resolved_packets"]
                        if item["factor_id"] == "repeated_reporting_windows")
        self.assertEqual(repeated["source_path"], REPEATED_WINDOW_SOURCE_PATH)
        self.assertEqual(repeated["grounding_status"], "record_contract_error")
        checks = [item for item in state["evidence_review"]["checks"]
                  if item["factor_id"] == "repeated_reporting_windows"]
        self.assertTrue(checks)
        self.assertTrue(all(item["result"] == "unresolved" for item in checks))
        self.assertEqual(state["belief_update"]["status"], "tentative")

    def test_wrong_store_or_window_stops_before_any_source_resolution(self):
        questions = (
            CAUSAL.replace("Store A", "Store B"),
            CAUSAL.replace("March-to-April", "April-to-May"),
            COMPARISON.replace("March", "April"),
            COMPARISON.replace("Stores B-F", "Stores B-C"),
        )
        for question in questions:
            with self.subTest(question=question), patch("rac.src.grounded_pipeline.resolve_state_evidence") as resolver:
                with self.assertRaises(ValueError):
                    self.run_review(question)
                resolver.assert_not_called()

    def test_model_packet_paths_and_claims_cannot_select_or_prove_evidence(self):
        baseline = self.run_review()
        real_route = evidence_review.route_evidence
        def proposed_packets(kind, factors):
            packets = real_route(kind, factors)
            for packet in packets:
                packet["source_path"] = "/unregistered/model-proposal.csv"
                packet["claim_supported"] = "The model says all values grew by 999999 and proves a sole cause."
            return packets
        with patch("rac.src.evidence_review.route_evidence", side_effect=proposed_packets):
            changed = self.run_review()
        for field in ("hypotheses", "critic_findings", "fact_check", "belief_update"):
            self.assertEqual(baseline[field], changed[field], field)
        self.assertTrue(all(packet["source_path"] == SOURCE_PATH for packet in changed["evidence_packets"]))
        self.assertTrue(all("999999" not in packet["claim_supported"] for packet in changed["evidence_packets"]))

    def test_unregistered_packet_route_is_rejected_before_reading_a_path(self):
        for path in ("/unregistered/model-proposal.csv", "../unregistered.csv", SOURCE_PATH):
            with self.subTest(path=path), patch("rac.src.local_evidence_resolver.read_source_text") as reader:
                with self.assertRaises(ValueError):
                    resolve_evidence_packet({"factor_id": "model_invented_factor", "source_path": path},
                                            question_type="causal_diagnostic", root=self.root)
                reader.assert_not_called()

    def test_altered_decisions_operands_scope_and_cached_report_are_rejected(self):
        baseline = self.run_review()
        mutations = (
            lambda state: state["belief_update"].__setitem__("claim", "A stale growth narrative"),
            lambda state: state["hypotheses"][0].__setitem__("status", "rejected"),
            lambda state: state["evidence_review"]["checks"][0]["operands"][0].__setitem__("value", "999999"),
            lambda state: state["evidence_review"]["scope"].__setitem__("store_ids", ["B"]),
            lambda state: state.__setitem__("final_report", "An unrelated old report"),
            lambda state: state["grounded_evidence_rows"][0].__setitem__("source_path", "unregistered.csv"),
        )
        for number, mutate in enumerate(mutations):
            with self.subTest(mutation=number):
                changed = deepcopy(baseline)
                mutate(changed)
                with self.assertRaises(CognitionStateValidationError):
                    validate_cognition_state(changed, root=self.root)


if __name__ == "__main__":
    unittest.main()
