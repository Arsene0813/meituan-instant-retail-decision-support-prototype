"""Synthetic uploads preserve their actual inclusive dates and source values."""
from __future__ import annotations

from copy import deepcopy
import csv
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator

from retail_ops.ingestion import batch_store
from retail_ops.ingestion.intake_registry import (
    aggregation_scope_sha256, resolve_upload, validate_aggregation_scope, validate_upload_window,
)
from retail_ops.ingestion.preview import ROUTES, SCHEMAS, UploadContext, preview_csv
from retail_ops.ingestion.source_windows import (
    ACTUAL_WINDOW_DATASETS, validate_source_dataset, validate_source_window,
)


ROOT = Path(__file__).resolve().parents[1]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encode(row):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(row))
    writer.writeheader()
    writer.writerow(row)
    return stream.getvalue().encode()


class RetailActualWindowIntakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.registry_path = self.base / "registry.json"
        self.database = self.base / "batches.sqlite3"
        identity = {
            "source_system": "meituan_merchant_backend",
            "source_account_id": "synthetic-actual-window-account",
            "source_store_id": "synthetic-actual-window-store-b",
        }
        identity_bytes = json.dumps(identity).encode()
        (self.base / "identity.json").write_bytes(identity_bytes)
        self.registry = {"registry_version": "2", "bindings": [{
            **identity, "binding_id": "synthetic-binding-b", "store_id": "B",
            "dataset_ids": sorted(SCHEMAS), "identity_evidence_path": "identity.json",
            "identity_evidence_sha256": sha(identity_bytes), "reviewed_by": "synthetic-reviewer",
        }], "uploads": []}

    def register(self, *, start="2026-03-08", end="2026-03-08", month="",
                 amount="12.3400", orders="0", dataset="demo2_store_period_metrics",
                 mapping="canonical_csv_v2", upload_id=None, store="B", extra=None):
        row = {"store_id": store, "period_start": start, "period_end": end,
               "period_month": month}
        grain, ranking_basis = ROUTES[dataset]
        if grain == "store_period":
            row.update(transaction_amount=amount, transaction_orders=orders, entry_users="")
        elif grain == "store_sku_period":
            row.update(sku_rank="1", sku_name="synthetic sku", sku_transaction_amount=amount, sales_volume=orders)
        else:
            row.update(search_term_rank="1", search_term="synthetic term", search_term_order_times=orders)
        if extra:
            row.update(extra)
        data = encode(row)
        upload_id = upload_id or f"synthetic-upload-{len(self.registry['uploads']) + 1}"
        receipt = {"upload_id": upload_id, "binding_id": "synthetic-binding-b",
                   "dataset_id": dataset, "period_start": start, "period_end": end,
                   "file_sha256": sha(data), "source_page": "synthetic-source",
                   "extracted_at": "2026-05-02T00:00:00+00:00", "mapping_version": mapping}
        self.registry["uploads"].append(receipt)
        self.save()
        context = UploadContext(dataset, "B", start, end, grain, ranking_basis)
        return receipt, data, row, context

    def save(self):
        self.registry_path.write_text(json.dumps(self.registry), encoding="utf-8")

    def receive(self, registered, **kwargs):
        receipt, data, *_ = registered
        return batch_store.receive_batch(ROOT, self.database, self.registry_path,
                                         receipt["upload_id"], data, **kwargs)

    def preview(self, registered, **kwargs):
        receipt, data, _, context = registered
        return preview_csv(ROOT, data, context, mapping_version=receipt["mapping_version"], **kwargs)

    def test_daily_source_survives_round_trip_with_null_zero_and_exact_decimal(self):
        registered = self.register()
        result = self.receive(registered)
        self.assertEqual(result["status"], "validated")
        record = result["preview"]["validated_records"][0]["record"]
        self.assertEqual(record["transaction_amount"], "12.3400")
        self.assertEqual(record["transaction_orders"], 0)
        self.assertIsNone(record["entry_users"])
        self.assertIsNone(record["period_month"])
        self.assertEqual((record["period_start"], record["period_end"]), ("2026-03-08", "2026-03-08"))
        self.assertEqual(result["metadata"]["coverage_start"], "2026-03-08")
        self.assertEqual(result["metadata"]["mapping_version"], "canonical_csv_v2")
        self.assertNotIn("report_window_kind", result["registration"]["receipt"])
        self.assertEqual(batch_store.read_batch(self.database, result["batch_id"])["preview"], result["preview"])
        self.assertEqual(batch_store.read_batch_artifacts(self.database, result["batch_id"])["data"], registered[1])

    def test_cross_month_source_keeps_full_dates_without_a_month_label(self):
        registered = self.register(start="2026-02-15", end="2026-03-14")
        result = self.receive(registered)
        self.assertEqual(result["status"], "validated")
        record = result["preview"]["validated_records"][0]["record"]
        self.assertIsNone(record["period_month"])
        self.assertEqual((record["period_start"], record["period_end"]), ("2026-02-15", "2026-03-14"))

    def test_v2_whole_month_requires_original_month_metadata(self):
        for month in ("", "2026-02", "2026-03"):
            with self.subTest(month=month):
                result = self.preview(self.register(start="2026-03-01", end="2026-03-31", month=month))
                self.assertEqual(result["status"], "validated" if month == "2026-03" else "quarantined")
                if not month:
                    self.assertIn("missing required period metadata: period_month",
                                  result["quarantined_records"][0]["errors"])

    def test_non_month_source_rejects_a_month_label_instead_of_changing_its_meaning(self):
        for start, end in (("2026-03-08", "2026-03-08"), ("2026-02-15", "2026-03-14")):
            with self.subTest(start=start):
                result = self.receive(self.register(start=start, end=end, month="2026-03"))
                self.assertEqual(result["status"], "quarantined")
                self.assertEqual(result["preview"]["validated_records"], [])
                self.assertIn("period_month must be blank", " ".join(result["preview"]["quarantined_records"][0]["errors"]))

    def test_missing_non_month_column_stays_null_and_source_has_no_added_column(self):
        receipt, _, row, context = self.register()
        row.pop("period_month")
        data = encode(row)
        self.registry["uploads"][-1]["file_sha256"] = sha(data)
        self.save()
        result = self.receive((receipt, data, row, context))
        self.assertEqual(result["status"], "validated")
        self.assertIsNone(result["preview"]["validated_records"][0]["record"]["period_month"])
        self.assertNotIn(b"period_month", batch_store.read_batch_artifacts(self.database, result["batch_id"])["data"])

    def test_v1_default_still_requires_a_complete_month(self):
        _, data, _, context = self.register()
        result = preview_csv(ROOT, data, context)
        self.assertEqual(result["status"], "quarantined")
        self.assertEqual(result["mapping_version"], "canonical_csv_v1")
        whole = self.register(start="2026-03-01", end="2026-03-31", month="2026-03", mapping="canonical_csv_v1")
        self.assertEqual(self.preview(whole)["status"], "validated")

    def test_registry_v1_cannot_enable_v2_even_for_a_complete_month(self):
        for mapping in ("canonical_csv_v1", "canonical_csv_v2"):
            with self.subTest(mapping=mapping):
                self.registry["uploads"] = []
                self.registry["registry_version"] = "1"
                registered = self.register(start="2026-03-01", end="2026-03-31", month="2026-03", mapping=mapping)
                if mapping == "canonical_csv_v1":
                    self.assertEqual(self.receive(registered)["status"], "validated")
                else:
                    with self.assertRaisesRegex(ValueError, "registry version"):
                        resolve_upload(ROOT, self.registry_path, registered[0]["upload_id"], registered[1])

    def test_v2_registers_only_explicit_period_datasets_for_non_month_source(self):
        self.assertEqual(len(ACTUAL_WINDOW_DATASETS), 5)
        for dataset in sorted(SCHEMAS):
            with self.subTest(dataset=dataset):
                self.registry["uploads"] = []
                registered = self.register(dataset=dataset, upload_id="synthetic-" + dataset)
                result = self.receive(registered)
                self.assertEqual(result["status"], "validated" if dataset in ACTUAL_WINDOW_DATASETS else "quarantined")
        with self.assertRaisesRegex(ValueError, "no registered source-window"):
            validate_source_dataset("model_invented_dataset", "canonical_csv_v2", "2026-03-08", "2026-03-08")

    def test_invalid_reversed_or_noncanonical_dates_are_rejected(self):
        for start, end in (("2026-03-09", "2026-03-08"), ("2026-02-29", "2026-03-01"),
                           ("2026-3-8", "2026-03-08"), ("2026-03-08", "2026-03-08T00:00:00"),
                           (None, "2026-03-08")):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                validate_source_window(start, end, "canonical_csv_v2")
        self.assertEqual(validate_source_window("2024-02-01", "2024-02-29", "canonical_csv_v2"), "2024-02")

    def test_v2_does_not_relax_unknown_field_store_or_source_window_checks(self):
        for extra, store in (({"gross_revenue": "999"}, "B"), ({}, "C"),
                             ({"unregistered_business_metric": "999"}, "B")):
            with self.subTest(extra=extra, store=store):
                result = self.preview(self.register(extra=extra, store=store))
                self.assertEqual(result["status"], "quarantined")
                self.assertEqual(result["validated_records"], [])
        registered = self.register()
        receipt, _, row, context = registered
        row["period_end"] = "2026-03-09"
        result = preview_csv(ROOT, encode(row), context, mapping_version=receipt["mapping_version"])
        self.assertEqual(result["status"], "quarantined")

    def test_model_cannot_supply_a_missing_metric_or_change_the_scope(self):
        registered = self.register(amount="")
        receipt, _, row, _ = registered
        proposal = {"dataset_id": receipt["dataset_id"], "grain": "store_period", "ranking_basis": None,
                    "record": dict(row)}
        self.assertEqual(self.preview(registered, proposals=[proposal])["status"], "validated")
        for field, value in (("transaction_amount", "100"), ("store_id", "C"),
                             ("period_month", "2026-03")):
            with self.subTest(field=field):
                changed = deepcopy(proposal)
                changed["record"][field] = value
                result = self.preview(registered, proposals=[changed])
                self.assertEqual(result["status"], "quarantined")
                self.assertEqual(result["validated_records"], [])

    def test_same_dates_can_be_explicitly_revised_from_v1_to_v2(self):
        first = self.receive(self.register(start="2026-03-01", end="2026-03-31", month="2026-03",
                                          mapping="canonical_csv_v1"))
        second_source = self.register(start="2026-03-01", end="2026-03-31", month="2026-03", amount="")
        self.assertEqual(self.receive(second_source)["status"], "quarantined")
        second = self.receive(second_source, supersedes_batch_id=first["batch_id"])
        self.assertEqual(second["status"], "validated")
        self.assertIsNone(second["preview"]["validated_records"][0]["record"]["transaction_amount"])
        self.assertEqual(first["registration"]["context"], second["registration"]["context"])
        self.assertEqual(batch_store.read_batch(self.database, first["batch_id"])["metadata"]["mapping_version"], "canonical_csv_v1")

    def test_daily_revision_cannot_replace_another_day_and_exact_retry_is_idempotent(self):
        first_source = self.register()
        first = self.receive(first_source)
        same = self.receive(first_source)
        self.assertTrue(same["idempotent"])
        self.assertEqual(same["batch_id"], first["batch_id"])
        tomorrow = self.register(start="2026-03-09", end="2026-03-09")
        self.assertEqual(self.receive(tomorrow, supersedes_batch_id=first["batch_id"])["status"], "quarantined")
        self.assertEqual(self.receive(tomorrow)["status"], "validated")

    def test_registry_schemas_keep_receipt_fields_and_disallow_unregistered_formats(self):
        registered = self.register()
        schema = json.loads((ROOT / "retail_ops/contracts/intake_registry.v2.schema.json").read_text())
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        self.assertTrue(validator.is_valid(self.registry))
        self.assertIsNone(validate_upload_window(registered[0], "2"))
        empty = json.loads((ROOT / "retail_ops/contracts/intake_registry.v2.empty.json").read_text())
        self.assertTrue(validator.is_valid(empty))
        for mutate in (lambda item: item.update(mapping_version="model_selected_v9"),
                       lambda item: item.update(report_window_kind="selected"),
                       lambda item: item.pop("period_start")):
            registry = deepcopy(self.registry)
            mutate(registry["uploads"][0])
            self.assertFalse(validator.is_valid(registry))
            with self.assertRaises(ValueError):
                validate_upload_window(registry["uploads"][0], "2")

    def test_archived_batch_with_prior_provenance_shape_still_reads_without_rewriting(self):
        source = self.register(start="2026-03-01", end="2026-03-31", month="2026-03", mapping="canonical_csv_v1")
        recorded = {key: value for key, value in batch_store._provenance(ROOT).items()
                    if key != "source_windows_sha256"}
        # Old ledgers carry their own provenance. Reading must validate those
        # stored hashes rather than replace them with current code provenance.
        with patch.object(batch_store, "_provenance", return_value=recorded):
            result = self.receive(source)
        self.assertEqual(result["status"], "validated")
        before = self.database.read_bytes()
        stored = batch_store.read_batch(self.database, result["batch_id"])
        self.assertEqual(stored["provenance"], recorded)
        self.assertEqual(self.database.read_bytes(), before)
        self.assertIn("source_windows_sha256", batch_store._provenance(ROOT))

    def reviewed_scope(self):
        return {"scope_version": "1", "timezone": "Asia/Shanghai",
                "selection_conditions": "Synthetic all channels and all products; no non-date filters.",
                "reviewed_by": "synthetic-source-condition-reviewer"}

    def with_scope(self, registered, scope):
        registered[0]["aggregation_scope"] = scope
        self.save()
        return registered

    def test_optional_review_is_archived_independently_and_has_a_canonical_digest(self):
        scope = self.reviewed_scope()
        registered = self.with_scope(self.register(), scope)
        resolved = resolve_upload(ROOT, self.registry_path, registered[0]["upload_id"], registered[1])
        self.assertEqual(resolved["receipt"]["aggregation_scope"], scope)
        self.assertNotIn("aggregation_scope", resolved["context"].__dict__)
        result = self.receive(registered)
        self.assertEqual(result["status"], "validated")
        saved = batch_store.read_batch(self.database, result["batch_id"])
        self.assertEqual(saved["registration"]["receipt"]["aggregation_scope"], scope)
        raw = batch_store.read_batch_artifacts(self.database, result["batch_id"])
        self.assertNotIn(b"aggregation_scope", raw["data"])
        self.assertEqual(json.loads(raw["registry_bytes"])["uploads"][0]["aggregation_scope"], scope)
        self.assertEqual(aggregation_scope_sha256(scope), sha(json.dumps(
            scope, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()))
        self.assertEqual(aggregation_scope_sha256(dict(reversed(list(scope.items())))), aggregation_scope_sha256(scope))
        self.assertIsNone(aggregation_scope_sha256(None))
        schema = json.loads((ROOT / "retail_ops/contracts/intake_registry.v2.schema.json").read_text())
        self.assertTrue(Draft202012Validator(schema).is_valid(self.registry))

    def test_review_requires_registered_fields_and_explicit_timezone_and_reviewer(self):
        changes = ({"scope_version": "2"}, {"timezone": "Mars/Olympus"}, {"timezone": "UTC+8"},
                   {"timezone": "localtime"}, {"selection_conditions": ""},
                   {"selection_conditions": " unknown "}, {"reviewed_by": ""},
                   {"extra_proof": "model-invented"})
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_aggregation_scope({**self.reviewed_scope(), **change})
        missing = self.reviewed_scope()
        missing.pop("reviewed_by")
        with self.assertRaises(ValueError):
            validate_aggregation_scope(missing)
        registered = self.with_scope(self.register(), None)
        self.assertEqual(self.receive(registered)["status"], "quarantined")

    def test_v1_registry_or_mapping_cannot_self_enable_scope_review(self):
        for registry_version, mapping in (("1", "canonical_csv_v1"), ("1", "canonical_csv_v2"),
                                          ("2", "canonical_csv_v1")):
            with self.subTest(registry=registry_version, mapping=mapping):
                self.registry["registry_version"] = registry_version
                self.registry["uploads"] = []
                registered = self.with_scope(self.register(start="2026-03-01", end="2026-03-31",
                    month="2026-03", mapping=mapping), self.reviewed_scope())
                with self.assertRaises(ValueError):
                    resolve_upload(ROOT, self.registry_path, registered[0]["upload_id"], registered[1])

    def test_daily_rows_and_model_proposals_cannot_create_a_scope_review(self):
        scope = self.reviewed_scope()
        registered = self.register(extra={"aggregation_scope": json.dumps(scope)})
        self.assertEqual(self.receive(registered)["status"], "quarantined")
        registered = self.register()
        proposal = {"dataset_id": registered[0]["dataset_id"], "grain": "store_period", "ranking_basis": None,
                    "record": {**registered[2], "aggregation_scope": scope}}
        self.assertEqual(self.receive(registered, proposals=[proposal])["status"], "quarantined")

    def test_changed_reviewed_filter_cannot_replace_predecessor_and_legacy_scope_stays_exact(self):
        scope = self.reviewed_scope()
        first = self.receive(self.with_scope(self.register(), scope))
        self.assertEqual(first["status"], "validated")
        changed = self.with_scope(self.register(amount="99"), {
            **scope, "selection_conditions": "Synthetic channel A only; all products."})
        blocked = self.receive(changed, supersedes_batch_id=first["batch_id"])
        self.assertEqual(blocked["status"], "quarantined")
        self.assertTrue(any("reviewed aggregation scope" in error for error in blocked["errors"]))
        revision = self.receive(self.with_scope(self.register(amount=""), deepcopy(scope)),
                                supersedes_batch_id=first["batch_id"])
        self.assertEqual(revision["status"], "validated")
        self.assertIsNone(revision["preview"]["validated_records"][0]["record"]["transaction_amount"])
        without_review = deepcopy(first["registration"])
        without_review["receipt"].pop("aggregation_scope")
        context, binding = without_review["context"], without_review["binding"]
        expected = {key: binding[key] for key in ("binding_id", "source_system", "source_account_id", "source_store_id")}
        expected.update({key: context[key] for key in (
            "dataset_id", "store_id", "period_start", "period_end", "grain", "ranking_basis")})
        self.assertEqual(batch_store._scope(without_review), batch_store._json(expected))
        self.assertEqual(json.loads(batch_store._scope(first["registration"]))["aggregation_scope_sha256"],
                         aggregation_scope_sha256(scope))


if __name__ == "__main__":
    unittest.main()
