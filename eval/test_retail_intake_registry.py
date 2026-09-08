"""Synthetic operator registrations and identity records for persisted intake."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from jsonschema import Draft202012Validator

from retail_ops.ingestion.intake_registry import resolve_upload
from retail_ops.ingestion.preview import UploadContext, preview_csv


ROOT = Path(__file__).resolve().parents[1]


class RetailIntakeRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        self.registry_path = self.root / "intake/registry.json"
        self.registry_path.parent.mkdir(parents=True)
        for name in ("retail_ops/contracts/datasets.v1.json", "retail_ops/data/DATA_DICTIONARY.md"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, path)
        self.data = (
            "store_id,period_month,period_start,period_end,transaction_orders,transaction_amount\n"
            "A,2026-03,2026-03-01,2026-03-31,0,1.25\n"
        ).encode()
        # Explicitly synthetic source identifiers; these are not merchant IDs.
        self.identity = {
            "source_system": "meituan_merchant_backend",
            "source_account_id": "synthetic_test_account_1",
            "source_store_id": "synthetic_test_store_1",
        }
        self.identity_path = self.registry_path.parent / "identity.json"
        self.identity_bytes = json.dumps(self.identity, sort_keys=True).encode()
        self.identity_path.write_bytes(self.identity_bytes)
        self.binding = {
            "binding_id": "synthetic_binding_a",
            **self.identity,
            "store_id": "A",
            "dataset_ids": ["store_a_monthly_metrics"],
            "identity_evidence_path": "identity.json",
            "identity_evidence_sha256": hashlib.sha256(self.identity_bytes).hexdigest(),
            "reviewed_by": "synthetic_test_operator",
        }
        self.receipt = {
            "upload_id": "synthetic_upload_1",
            "binding_id": self.binding["binding_id"],
            "dataset_id": "store_a_monthly_metrics",
            "period_start": "2026-03-01",
            "period_end": "2026-03-31",
            "file_sha256": hashlib.sha256(self.data).hexdigest(),
            "source_page": "synthetic_test_source_page",
            "extracted_at": "2026-09-08T12:00:00+00:00",
            "mapping_version": "canonical_csv_v1",
        }
        self.registry = {
            "registry_version": "1",
            "bindings": [deepcopy(self.binding)],
            "uploads": [deepcopy(self.receipt)],
        }
        self.write_registry()

    def write_registry(self, registry=None):
        self.registry_path.write_text(json.dumps(self.registry if registry is None else registry, indent=2))

    def resolve(self, data=None, upload_id="synthetic_upload_1"):
        return resolve_upload(self.root, self.registry_path, upload_id, self.data if data is None else data)

    def test_receipt_resolves_independent_scope_and_keeps_registration_bytes(self):
        result = self.resolve()
        self.assertIsInstance(result["context"], UploadContext)
        self.assertEqual(result["context"], UploadContext(
            dataset_id="store_a_monthly_metrics", store_id="A",
            period_start="2026-03-01", period_end="2026-03-31", grain="store_period",
        ))
        self.assertEqual(result["registry_bytes"], self.registry_path.read_bytes())
        self.assertEqual(result["registry_sha256"], hashlib.sha256(result["registry_bytes"]).hexdigest())
        self.assertEqual(result["identity_evidence"], self.identity_bytes)
        self.assertEqual(result["receipt"], self.receipt)
        self.assertEqual(result["binding"]["store_id"], "A")
        metadata = result["metadata"]
        self.assertTrue({"batch_id", "received_at", "status"}.isdisjoint(metadata))
        self.assertEqual(metadata["file_sha256"], self.receipt["file_sha256"])
        self.assertEqual(metadata["source_system"], self.identity["source_system"])
        self.assertEqual(metadata["coverage_start"], "2026-03-01")
        self.assertEqual(metadata["coverage_end"], "2026-03-31")
        self.assertEqual(metadata["snapshot_semantics"], "cumulative_period_snapshot")
        self.assertEqual(preview_csv(self.root, self.data, result["context"])["status"], "validated")

    def test_unknown_upload_and_modified_payload_cannot_use_an_existing_receipt(self):
        with self.assertRaises(ValueError):
            self.resolve(upload_id="unregistered_upload")
        with self.assertRaises(ValueError):
            self.resolve(data=self.data.replace(b",0,1.25", b",10,1.25"))

    def test_registered_hash_does_not_override_source_store_identity(self):
        wrong_store_bytes = self.data.replace(b"\nA,2026", b"\nB,2026")
        self.registry["uploads"][0]["file_sha256"] = hashlib.sha256(wrong_store_bytes).hexdigest()
        self.write_registry()
        resolved = self.resolve(data=wrong_store_bytes)
        self.assertEqual(resolved["context"].store_id, "A")
        preview = preview_csv(self.root, wrong_store_bytes, resolved["context"])
        self.assertEqual(preview["status"], "quarantined")
        self.assertEqual(preview["validated_records"], [])

    def test_unknown_fields_and_missing_required_fields_are_rejected(self):
        original = deepcopy(self.registry)
        for location in ("registry", "binding", "upload"):
            for missing in (False, True):
                with self.subTest(location=location, missing=missing):
                    data = deepcopy(original)
                    target = data if location == "registry" else data["bindings" if location == "binding" else "uploads"][0]
                    if missing:
                        target.pop("registry_version" if location == "registry" else "reviewed_by" if location == "binding" else "source_page")
                    else:
                        target["unregistered_field"] = "synthetic proposal"
                    self.write_registry(data)
                    with self.assertRaises(ValueError):
                        self.resolve()

    def test_published_schema_and_parser_agree_on_structure_and_mapping_version(self):
        schema = json.loads((ROOT / "retail_ops/contracts/intake_registry.v1.schema.json").read_text())
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        self.assertTrue(validator.is_valid(self.registry))
        self.resolve()
        for mutation in (
            lambda data: data["bindings"][0].update(transaction_amount=999),
            lambda data: data["uploads"][0].update(mapping_version="unregistered_v9"),
        ):
            data = deepcopy(self.registry)
            mutation(data)
            self.assertFalse(validator.is_valid(data))
            self.write_registry(data)
            with self.assertRaises(ValueError):
                self.resolve()

    def test_duplicate_json_keys_and_registry_ids_are_rejected(self):
        original = deepcopy(self.registry)
        for mode in ("top_json_key", "nested_json_key", "binding_id", "upload_id"):
            with self.subTest(mode=mode):
                data = deepcopy(original)
                if mode == "binding_id":
                    data["bindings"].append(deepcopy(data["bindings"][0]))
                elif mode == "upload_id":
                    data["uploads"].append(deepcopy(data["uploads"][0]))
                text = json.dumps(data)
                if mode == "top_json_key":
                    text = text.replace('"registry_version": "1"', '"registry_version": "1", "registry_version": "1"')
                elif mode == "nested_json_key":
                    text = text.replace('"store_id": "A"', '"store_id": "A", "store_id": "A"')
                self.registry_path.write_text(text)
                with self.assertRaises(ValueError):
                    self.resolve()

    def test_dataset_format_binding_and_window_must_be_registered(self):
        changes = (
            ("dataset_id", "model_invented_dataset"), ("binding_id", "missing_binding"),
            ("mapping_version", "model_inferred_csv_v9"),
            ("period_start", "2026-03-02"), ("period_end", "2026-04-30"),
            ("period_end", "2026-03-32"), ("period_start", "2026-3-1"),
            ("extracted_at", "2026-09-08T12:00:00"), ("source_page", ""),
        )
        original = deepcopy(self.registry)
        for field, value in changes:
            with self.subTest(field=field, value=value):
                data = deepcopy(original)
                data["uploads"][0][field] = value
                self.write_registry(data)
                with self.assertRaises(ValueError):
                    self.resolve()
        data = deepcopy(original)
        data["bindings"][0]["dataset_ids"] = ["demo2_store_period_metrics"]
        self.write_registry(data)
        with self.assertRaises(ValueError):
            self.resolve()

    def test_dataset_route_is_checked_and_source_path_is_not_used_as_a_write_destination(self):
        path = self.root / "retail_ops/contracts/datasets.v1.json"
        original = json.loads(path.read_text())
        for field, value in (("key_fields", ["store_id", "period_month"]),
                             ("grain", "store_sku_period")):
            with self.subTest(field=field):
                contracts = deepcopy(original)
                contract = next(item for item in contracts["datasets"] if item["dataset_id"] == "store_a_monthly_metrics")
                contract[field] = value
                path.write_text(json.dumps(contracts))
                with self.assertRaises(ValueError):
                    self.resolve()
        contracts = deepcopy(original)
        contract = next(item for item in contracts["datasets"] if item["dataset_id"] == "store_a_monthly_metrics")
        contract["source_path"] = "retail_ops/data/registered_relocated_source.csv"
        path.write_text(json.dumps(contracts))
        result = self.resolve()
        self.assertEqual(result["context"].dataset_id, "store_a_monthly_metrics")
        self.assertFalse((self.root / contract["source_path"]).exists())

    def test_identity_bytes_must_match_both_the_hash_and_binding(self):
        self.identity_path.write_bytes(self.identity_bytes + b"\n")
        with self.assertRaises(ValueError):
            self.resolve()
        changed = dict(self.identity, source_store_id="synthetic_different_source_store")
        changed_bytes = json.dumps(changed).encode()
        self.identity_path.write_bytes(changed_bytes)
        self.registry["bindings"][0]["identity_evidence_sha256"] = hashlib.sha256(changed_bytes).hexdigest()
        self.write_registry()
        with self.assertRaises(ValueError):
            self.resolve()

    def test_identity_evidence_contains_only_the_three_registered_identity_fields(self):
        for mutate in (lambda data: data.update(transaction_amount=999),
                       lambda data: data.pop("source_account_id"),
                       lambda data: data.update(source_store_id="")):
            evidence = deepcopy(self.identity)
            mutate(evidence)
            encoded = json.dumps(evidence).encode()
            self.identity_path.write_bytes(encoded)
            self.registry["bindings"][0]["identity_evidence_sha256"] = hashlib.sha256(encoded).hexdigest()
            self.write_registry()
            with self.assertRaises(ValueError):
                self.resolve()

    def test_identity_absolute_traversal_symlink_and_missing_files_are_rejected(self):
        outside = Path(self.temp.name) / "outside.json"
        outside.write_bytes(self.identity_bytes)
        linked = self.registry_path.parent / "identity-link.json"
        linked.symlink_to(self.identity_path)
        original = deepcopy(self.registry)
        for evidence_path in (str(outside), "../../outside.json", "identity-link.json", "missing.json"):
            with self.subTest(path=evidence_path):
                data = deepcopy(original)
                data["bindings"][0]["identity_evidence_path"] = evidence_path
                self.write_registry(data)
                with self.assertRaises((ValueError, OSError)):
                    self.resolve()

    def test_duplicate_source_identity_is_ambiguous_but_multiple_accounts_can_map_to_one_store(self):
        duplicate = deepcopy(self.binding)
        duplicate["binding_id"] = "synthetic_binding_duplicate"
        duplicate["store_id"] = "B"
        self.registry["bindings"].append(duplicate)
        self.write_registry()
        with self.assertRaises(ValueError):
            self.resolve()
        second_identity = dict(self.identity, source_account_id="synthetic_test_account_2")
        second_bytes = json.dumps(second_identity).encode()
        (self.registry_path.parent / "identity-second.json").write_bytes(second_bytes)
        duplicate.update(second_identity, store_id="A", identity_evidence_path="identity-second.json",
                         identity_evidence_sha256=hashlib.sha256(second_bytes).hexdigest())
        self.write_registry()
        self.assertEqual(self.resolve()["context"].store_id, "A")


if __name__ == "__main__":
    unittest.main()
