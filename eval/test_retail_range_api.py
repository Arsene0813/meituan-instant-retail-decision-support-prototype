"""HTTP selectors cannot supply evidence, paths or persistent query actions."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from api import retail_range_api as api


ROOT = Path(__file__).resolve().parents[1]
PUBLICATION_ID = "publication_" + "a" * 64
DATASET = "store_period_panel_metrics"


class RetailRangeApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.client = TestClient(api.create_app(self.directory, ROOT))
        self.addCleanup(self.client.close)
        self.body = {"publication_id": PUBLICATION_ID, "dataset_id": DATASET,
                     "store_ids": ["B"], "month": "2026-03",
                     "fields": ["transaction_amount", "transaction_orders"]}

    def test_query_delegates_only_exact_selectors_and_preserves_values(self):
        payload = {"publication_id": PUBLICATION_ID, "amount": "9007199254740993.10",
                   "orders": 9007199254740993, "missing": None, "zero": 0}
        with patch.object(api, "query_publication", return_value=payload) as reader:
            response = self.client.post("/retail/query", json=self.body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), payload)
        reader.assert_called_once_with(ROOT, self.directory, PUBLICATION_ID, DATASET,
            ["B"], "2026-03-01", "2026-03-31", self.body["fields"])

    def test_month_and_explicit_dates_dispatch_identically(self):
        explicit = {key: value for key, value in self.body.items() if key != "month"}
        explicit.update(period_start="2026-03-01", period_end="2026-03-31")
        with patch.object(api, "query_publication", return_value={}) as reader:
            self.assertEqual(self.client.post("/retail/query", json=self.body).status_code, 200)
            self.assertEqual(self.client.post("/retail/query", json=explicit).status_code, 200)
        self.assertEqual(reader.call_args_list[0], reader.call_args_list[1])

    def test_review_uses_server_analysis_with_optional_baseline(self):
        body = {**self.body, "baseline": {"month": "2026-02"}}
        payload = {"publication_id": PUBLICATION_ID, "query": {}, "baseline": {}, "rac": {}}
        with patch.object(api, "analyze_publication_range", return_value=payload) as reviewer:
            response = self.client.post("/retail/review", json=body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), payload)
        reviewer.assert_called_once_with(ROOT, self.directory, PUBLICATION_ID, DATASET,
            ["B"], "2026-03-01", "2026-03-31", self.body["fields"],
            baseline_start="2026-02-01", baseline_end="2026-02-28")
        with patch.object(api, "analyze_publication_range", return_value=payload) as reviewer:
            response = self.client.post("/retail/review", json={**self.body, "store_ids": ["B", "C"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(reviewer.call_args.kwargs, {"baseline_start": None, "baseline_end": None})

    def test_request_cannot_override_paths_evidence_or_persistence(self):
        for field, value in (("directory", "/tmp/other"), ("root", "/tmp/other"),
                             ("database", "batches.sqlite3"), ("sql", "SELECT * FROM source_records"),
                             ("query", {}), ("evidence_packets", []), ("rac", {}),
                             ("save", True), ("output", "report.json"), ("message", "B店营业额")):
            for endpoint in ("/retail/query", "/retail/review"):
                with self.subTest(endpoint=endpoint, field=field), patch.object(api, "query_publication") as reader, \
                        patch.object(api, "analyze_publication_range") as reviewer:
                    response = self.client.post(endpoint, json={**self.body, field: value})
                    self.assertEqual(response.status_code, 422)
                    reader.assert_not_called()
                    reviewer.assert_not_called()

    def test_baseline_cannot_supply_another_publication_or_store(self):
        for field, value in (("publication_id", PUBLICATION_ID), ("dataset_id", DATASET),
                             ("store_ids", ["C"]), ("evidence", {}), ("save", True)):
            with self.subTest(field=field), patch.object(api, "analyze_publication_range") as reviewer:
                response = self.client.post("/retail/review", json={**self.body,
                    "baseline": {"month": "2026-02", field: value}})
                self.assertEqual(response.status_code, 422)
                reviewer.assert_not_called()

    def test_query_does_not_accept_baseline(self):
        with patch.object(api, "query_publication") as reader:
            response = self.client.post("/retail/query", json={**self.body, "baseline": {"month": "2026-02"}})
        self.assertEqual(response.status_code, 422)
        reader.assert_not_called()

    def test_duplicate_raw_json_selectors_never_choose_the_last_value(self):
        duplicate_values = ('"month": "2026-04"',
                            '"publication_id": "publication_' + "b" * 64 + '"',
                            '"store_ids": ["C"]')
        for endpoint in ("/retail/query", "/retail/review"):
            for duplicate in duplicate_values:
                body = json.dumps(self.body)[:-1] + ", " + duplicate + "}"
                with self.subTest(endpoint=endpoint, duplicate=duplicate), \
                        patch.object(api, "query_publication") as reader, \
                        patch.object(api, "analyze_publication_range") as reviewer, \
                        patch.object(api, "_directory") as directory:
                    response = self.client.post(endpoint, content=body,
                                                headers={"Content-Type": "application/json"})
                    self.assertEqual(response.status_code, 422)
                    self.assertEqual(response.json()["detail"]["code"], "invalid_json")
                    reader.assert_not_called()
                    reviewer.assert_not_called()
                    directory.assert_not_called()

    def test_duplicate_nested_baseline_dates_are_rejected_before_io(self):
        body = json.dumps(self.body)[:-1] + ', "baseline": {"month": "2026-01", "month": "2026-02"}}'
        with patch.object(api, "analyze_publication_range") as reviewer, patch.object(api, "_directory") as directory:
            response = self.client.post("/retail/review", content=body,
                                        headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"]["code"], "invalid_json")
        reviewer.assert_not_called()
        directory.assert_not_called()

    def test_nonfinite_json_is_rejected_before_validation_error_serialization(self):
        for endpoint in ("/retail/query", "/retail/review"):
            for value in ("NaN", "Infinity", "-Infinity", "1e999"):
                body = {key: item for key, item in self.body.items() if key != "fields"}
                raw = json.dumps(body)[:-1] + ', "fields": [' + value + "]}"
                with self.subTest(endpoint=endpoint, value=value), \
                        patch.object(api, "query_publication") as reader, \
                        patch.object(api, "analyze_publication_range") as reviewer, \
                        patch.object(api, "_directory") as directory:
                    response = self.client.post(endpoint, content=raw,
                                                headers={"Content-Type": "application/json"})
                    self.assertEqual(response.status_code, 422)
                    self.assertEqual(response.json()["detail"]["code"], "invalid_json")
                    reader.assert_not_called()
                    reviewer.assert_not_called()
                    directory.assert_not_called()

    def test_ranked_datasets_are_queryable_but_not_range_review_inputs(self):
        for dataset in ("store_a_top_skus", "demo2_top_skus_by_transaction_amount",
                        "demo2_top_skus_by_sales_volume", "demo2_top_search_terms"):
            body = {**self.body, "dataset_id": dataset, "fields": None}
            with self.subTest(dataset=dataset), patch.object(api, "analyze_publication_range") as reviewer, \
                    patch.object(api, "_directory") as directory:
                self.assertEqual(self.client.post("/retail/review", json=body).status_code, 422)
                reviewer.assert_not_called()
                directory.assert_not_called()
            with self.subTest(dataset=dataset), patch.object(api, "query_publication", return_value={}) as reader:
                self.assertEqual(self.client.post("/retail/query", json=body).status_code, 200)
                self.assertEqual(reader.call_args.args[3], dataset)

    def test_invalid_or_ambiguous_date_windows_never_reach_publication(self):
        base = {key: value for key, value in self.body.items() if key != "month"}
        windows = ({}, {"period_start": "2026-03-01"}, {"period_end": "2026-03-31"},
            {"month": "2026-03", "period_start": "2026-03-01", "period_end": "2026-03-31"},
            {"month": "2026-13"}, {"month": "2026-3"},
            {"period_start": "2026-02-30", "period_end": "2026-03-01"},
            {"period_start": "2026-03-02", "period_end": "2026-03-01"},
            {"period_start": "2026-3-1", "period_end": "2026-03-31"},
            {"month": 202603}, {"period_start": False, "period_end": "2026-03-31"})
        for window in windows:
            with self.subTest(window=window), patch.object(api, "query_publication") as reader:
                response = self.client.post("/retail/query", json={**base, **window})
                self.assertEqual(response.status_code, 422)
                reader.assert_not_called()

    def test_baseline_requires_one_store_and_preceding_nonoverlapping_dates(self):
        bodies = ({**self.body, "store_ids": ["B", "C"], "baseline": {"month": "2026-02"}},
                  {**self.body, "baseline": {"month": "2026-03"}},
                  {**self.body, "baseline": {"month": "2026-04"}},
                  {**self.body, "baseline": {"period_start": "2026-02-01", "period_end": "2026-03-01"}})
        for body in bodies:
            with self.subTest(body=body), patch.object(api, "analyze_publication_range") as reviewer:
                self.assertEqual(self.client.post("/retail/review", json=body).status_code, 422)
                reviewer.assert_not_called()

    def test_publication_ids_cannot_be_paths_or_implicit_latest(self):
        for publication in ("latest", "1063ba3", "../outside", "/tmp/publication", "publication_" + "A" * 64,
                            PUBLICATION_ID + "\n", PUBLICATION_ID + "/manifest.json", None):
            with self.subTest(publication=publication), patch.object(api, "query_publication") as reader:
                response = self.client.post("/retail/query", json={**self.body, "publication_id": publication})
                self.assertEqual(response.status_code, 422)
                reader.assert_not_called()

    def test_names_are_strict_and_metrics_are_not_inferred_from_aliases(self):
        changes = ({"dataset_id": "monthly_metrics"}, {"dataset_id": "店铺指标"},
                   {"store_ids": []}, {"store_ids": ["B", "B"]}, {"store_ids": [" B"]},
                   {"store_ids": [1]}, {"store_ids": "B"}, {"fields": []},
                   {"fields": ["transaction_amount", "transaction_amount"]}, {"fields": ["营业额"]},
                   {"fields": ["gross_revenue"]}, {"fields": ["store_id"]}, {"fields": [True]})
        for change in changes:
            with self.subTest(change=change), patch.object(api, "query_publication") as reader:
                response = self.client.post("/retail/query", json={**self.body, **change})
                self.assertEqual(response.status_code, 422)
                reader.assert_not_called()

    def test_field_selection_uses_exact_dataset_contract(self):
        for fields in (["transaction_amount"], ["sku_rank"], ["search_term_order_times"]):
            body = {**self.body, "dataset_id": "demo2_top_skus_by_transaction_amount", "fields": fields}
            with self.subTest(fields=fields), patch.object(api, "query_publication") as reader:
                self.assertEqual(self.client.post("/retail/query", json=body).status_code, 422)
                reader.assert_not_called()
        with patch.object(api, "query_publication", return_value={}) as reader:
            response = self.client.post("/retail/query", json={**self.body,
                "dataset_id": "demo2_top_skus_by_transaction_amount", "fields": ["sku_transaction_amount"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(reader.call_args.args[-1], ["sku_transaction_amount"])

    def test_store_ids_are_passed_exactly_without_name_translation(self):
        with patch.object(api, "query_publication", return_value={}) as reader:
            response = self.client.post("/retail/query", json={**self.body, "store_ids": ["store_B"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(reader.call_args.args[4], ["store_B"])

    def test_default_fields_are_delegated_without_filling_values(self):
        body = {key: value for key, value in self.body.items() if key != "fields"}
        with patch.object(api, "query_publication", return_value={}) as reader:
            self.assertEqual(self.client.post("/retail/query", json=body).status_code, 200)
        self.assertIsNone(reader.call_args.args[-1])

    def test_server_configuration_is_required_and_not_created_by_queries(self):
        missing = self.directory / "uncreated"
        configurations = (None, missing, ROOT, ROOT / "retail_ops/outputs")
        for directory in configurations:
            with self.subTest(directory=directory), patch.dict(os.environ, {}, clear=True), \
                    TestClient(api.create_app(directory, ROOT)) as client, patch.object(api, "query_publication") as reader:
                self.assertEqual(client.post("/retail/query", json=self.body).status_code, 503)
                self.assertEqual(client.get("/health").status_code, 503)
                reader.assert_not_called()
        self.assertFalse(missing.exists())

    def test_symlinked_storage_is_not_accepted_as_server_configuration(self):
        link = self.directory / "storage-link"
        link.symlink_to(self.directory, target_is_directory=True)
        with TestClient(api.create_app(link, ROOT)) as client, patch.object(api, "query_publication") as reader:
            self.assertEqual(client.post("/retail/query", json=self.body).status_code, 503)
            reader.assert_not_called()

    def test_environment_configures_storage_once_without_response_paths(self):
        with patch.dict(os.environ, {"MEITUAN_PUBLICATION_DIRECTORY": str(self.directory)}):
            app = api.create_app(root=ROOT)
        with TestClient(app) as client, patch.object(api, "query_publication", return_value={}) as reader:
            self.assertEqual(client.post("/retail/query", json=self.body).status_code, 200)
            self.assertEqual(client.get("/health").json(), {"status": "ready"})
        self.assertEqual(reader.call_args.args[1], self.directory)

    def test_read_failures_return_no_evidence_or_local_paths(self):
        cases = ((ValueError("Unknown publication ID."), 404, "unknown_publication"),
                 (ValueError("Publication analysis rules or runtime differ from the current registered recipe."),
                  409, "publication_verification_failed"),
                 (ValueError("Publication artifact bytes differ from the manifest SHA-256 values."),
                  409, "publication_verification_failed"),
                 (OSError("/private/path/source.csv unreadable"), 503, "publication_read_failed"))
        for error, status, code in cases:
            for endpoint, function in (("/retail/query", "query_publication"),
                                       ("/retail/review", "analyze_publication_range")):
                with self.subTest(endpoint=endpoint, error=error), patch.object(api, function, side_effect=error):
                    response = self.client.post(endpoint, json=self.body)
                    self.assertEqual(response.status_code, status)
                    self.assertEqual(response.json()["detail"]["code"], code)
                    self.assertEqual(set(response.json()), {"detail"})
                    self.assertNotIn("/private/path", response.text)

    def test_service_exposes_no_upload_publication_or_report_writes(self):
        paths = self.client.get("/openapi.json").json()["paths"]
        self.assertEqual(set(paths), {"/health", "/retail/query", "/retail/review"})
        for path in ("/retail/upload", "/retail/publish", "/retail/save", "/chat"):
            self.assertEqual(self.client.post(path, json={}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
