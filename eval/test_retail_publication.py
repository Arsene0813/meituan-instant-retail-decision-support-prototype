"""Synthetic publication selections, archived replay, and pinned-file integrity."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from retail_ops.ingestion import batch_store, publication


ROOT = Path(__file__).resolve().parents[1]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def source(amount="12.3400", *, month="03", orders="0", store="B"):
    end = "31" if month == "03" else "30"
    return ("store_id,period_start,period_end,period_month,transaction_amount,transaction_orders,entry_users\n"
            f"{store},2026-{month}-01,2026-{month}-{end},2026-{month},{amount},{orders},\n").encode()


class RetailPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "project"
        self.database = self.base / "batches.sqlite3"
        self.directory = self.base / "publications"
        self.registry_path = self.base / "registry.json"
        for name in ("retail_ops/contracts/datasets.v1.json", "retail_ops/data/DATA_DICTIONARY.md",
                     "retail_ops/contracts/manual_text.v2.json", "retail_ops/contracts/manual_text.v3.json"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, path)
        identity = {"source_system": "meituan_merchant_backend", "source_account_id": "synthetic_account",
                    "source_store_id": "synthetic_store_b"}
        data = json.dumps(identity).encode()
        (self.base / "identity.json").write_bytes(data)
        self.registry = {"registry_version": "1", "bindings": [{
            **identity, "binding_id": "synthetic_binding_b", "store_id": "B",
            "dataset_ids": ["demo2_store_period_metrics", "store_period_panel_metrics"],
            "identity_evidence_path": "identity.json", "identity_evidence_sha256": sha(data),
            "reviewed_by": "synthetic_operator",
        }], "uploads": []}
        self.runtime = b'{"test_recipe":"1"}'
        self.addCleanup(patch.stopall)
        patch.object(publication.publication_recipe, "recipe_files", side_effect=self.recipe_files).start()
        patch.object(publication.publication_recipe, "build_evidence_view", side_effect=self.build_view).start()

    def recipe_files(self, root):
        return {**{name: (root / name).read_bytes() for name in (
            "retail_ops/contracts/datasets.v1.json", "retail_ops/data/DATA_DICTIONARY.md")},
            "publication_runtime.json": self.runtime}

    def build_view(self, root, selected):
        records = {item["result"]["batch_id"]: item["result"]["preview"]["validated_records"]
                   for item in selected}
        (root / "selected.json").write_text(json.dumps(records), encoding="utf-8")
        return {"views": ["selected.json"], "row_lineage": [], "queries": [], "facts": 0}

    def receive(self, upload="u1", data=None, *, month="03", dataset="demo2_store_period_metrics",
                predecessor=None, binding_id="synthetic_binding_b"):
        data = source(month=month) if data is None else data
        self.registry["uploads"].append({
            "upload_id": upload, "binding_id": binding_id, "dataset_id": dataset,
            "period_start": f"2026-{month}-01", "period_end": f"2026-{month}-{'31' if month == '03' else '30'}",
            "file_sha256": sha(data), "source_page": "synthetic_page", "extracted_at": "2026-05-01T00:00:00Z",
            "mapping_version": "canonical_csv_v1",
        })
        self.registry_path.write_text(json.dumps(self.registry), encoding="utf-8")
        return batch_store.receive_batch(self.root, self.database, self.registry_path, upload, data,
                                         supersedes_batch_id=predecessor)

    def publish(self, *batches):
        return publication.publish(self.root, self.database, self.directory, [item["batch_id"] for item in batches])

    def edit_result(self, batch_id, update):
        with sqlite3.connect(self.database) as connection:
            result = json.loads(connection.execute("SELECT result_json FROM batches WHERE batch_id=?", (batch_id,)).fetchone()[0])
            update(result)
            serialized = batch_store._json(result)
            connection.execute("UPDATE batches SET result_json=?,result_sha256=? WHERE batch_id=?",
                               (serialized, sha(serialized.encode()), batch_id))

    def test_round_trip_archives_raw_source_and_pins_exact_values(self):
        batch = self.receive()
        result = self.publish(batch)
        archive = self.directory / result["publication_id"] / "archive" / batch["batch_id"]
        self.assertEqual((archive / "source.csv").read_bytes(), source())
        self.assertEqual((archive / "registry.json").read_bytes(), self.registry_path.read_bytes())
        self.database.unlink()  # A published version can be read independently of the intake ledger.
        with publication.open_publication(self.root, self.directory, result["publication_id"]) as pin:
            self.assertEqual(pin.publication_id, result["publication_id"])
            record = json.loads((pin.root / "selected.json").read_text())[batch["batch_id"]][0]["record"]
            self.assertEqual(record["transaction_amount"], "12.3400")
            self.assertEqual(record["transaction_orders"], 0)
            self.assertIsNone(record["entry_users"])
            private_root = pin.root
        self.assertFalse(private_root.exists())

    def test_selection_order_and_exact_retries_have_one_identity(self):
        first = self.receive()
        second = self.receive("april", month="04")
        left = self.publish(first, second)
        right = self.publish(second, first)
        self.assertEqual(left, right)
        self.assertEqual(len(list(self.directory.glob("publication_*/manifest.json"))), 1)

    def test_explicit_older_version_remains_selectable_after_revision(self):
        first = self.receive()
        old = self.publish(first)
        second = self.receive("revision", source("", orders="2"), predecessor=first["batch_id"])
        revised = self.publish(second)
        self.assertNotEqual(old["publication_id"], revised["publication_id"])
        self.assertEqual(self.publish(first), old)
        for manifest, batch, value in ((old, first, "12.3400"), (revised, second, None)):
            with publication.open_publication(self.root, self.directory, manifest["publication_id"]) as pin:
                record = json.loads((pin.root / "selected.json").read_text())[batch["batch_id"]][0]["record"]
                self.assertEqual(record["transaction_amount"], value)

    def test_old_and_new_versions_cannot_be_selected_together(self):
        first = self.receive()
        second = self.receive("revision", source("99"), predecessor=first["batch_id"])
        with self.assertRaisesRegex(ValueError, "Overlapping"):
            self.publish(first, second)
        self.assertFalse(self.directory.exists())

    def test_different_registered_datasets_cannot_duplicate_an_overlap_partition(self):
        first = self.receive()
        second = self.receive("panel", dataset="store_period_panel_metrics")
        self.assertEqual(second["status"], "validated")
        with self.assertRaisesRegex(ValueError, "Overlapping"):
            self.publish(first, second)

    def test_different_source_bindings_cannot_duplicate_an_overlap_partition(self):
        first = self.receive()
        binding = deepcopy(self.registry["bindings"][0])
        binding.update(binding_id="synthetic_other_binding", source_account_id="synthetic_other_account",
                       identity_evidence_path="other.json")
        data = json.dumps({key: binding[key] for key in publication.intake_registry.IDENTITY_KEYS}).encode()
        (self.base / "other.json").write_bytes(data)
        binding["identity_evidence_sha256"] = sha(data)
        self.registry["bindings"].append(binding)
        second = self.receive("other", binding_id=binding["binding_id"])
        self.assertEqual(second["status"], "validated")
        with self.assertRaisesRegex(ValueError, "Overlapping"):
            self.publish(first, second)

    def test_two_partial_rankings_cannot_form_one_publication(self):
        dataset = "demo2_top_skus_by_sales_volume"
        self.registry["bindings"][0]["dataset_ids"].append(dataset)
        header = "store_id,period_start,period_end,period_month,sku_rank,sku_name,sales_volume\n"
        first = self.receive(data=(header + "B,2026-03-01,2026-03-31,2026-03,1,synthetic_first,2\n").encode(), dataset=dataset)
        second = self.receive("revision", (header + "B,2026-03-01,2026-03-31,2026-03,2,synthetic_second,3\n").encode(),
                              dataset=dataset, predecessor=first["batch_id"])
        with self.assertRaisesRegex(ValueError, "Overlapping"):
            self.publish(first, second)

    def test_quarantined_batch_cannot_publish(self):
        batch = self.receive(data=source(store="C"))
        self.assertEqual(batch["status"], "quarantined")
        with self.assertRaisesRegex(ValueError, "validated batches"):
            self.publish(batch)
        self.assertFalse(self.directory.exists())

    def test_raw_archive_corruption_is_detected(self):
        batch = self.receive()
        with sqlite3.connect(self.database) as connection:
            connection.execute("UPDATE batches SET raw_data=? WHERE batch_id=?", (source("999"), batch["batch_id"]))
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            self.publish(batch)

    def test_rehashed_forged_preview_still_fails_source_replay(self):
        batch = self.receive()
        self.edit_result(batch["batch_id"], lambda item: item["preview"]["validated_records"][0]["record"].update(transaction_amount="999"))
        # The generic archive reader accepts a self-consistent result digest.
        self.assertEqual(batch_store.read_batch(self.database, batch["batch_id"])["status"], "validated")
        with self.assertRaisesRegex(ValueError, "independent source replay"):
            self.publish(batch)

    def test_rehashed_forged_metadata_still_fails_receipt_replay(self):
        batch = self.receive()
        self.edit_result(batch["batch_id"], lambda item: item["metadata"].update(source_page="invented_source"))
        with self.assertRaisesRegex(ValueError, "batch metadata"):
            self.publish(batch)

    def test_replay_uses_archived_selected_identity_without_live_registry_files(self):
        binding = deepcopy(self.registry["bindings"][0])
        binding.update(binding_id="synthetic_unused_binding", source_account_id="synthetic_unused_account",
                       identity_evidence_path="unused.json")
        data = json.dumps({key: binding[key] for key in publication.intake_registry.IDENTITY_KEYS}).encode()
        (self.base / "unused.json").write_bytes(data)
        binding["identity_evidence_sha256"] = sha(data)
        self.registry["bindings"].append(binding)
        batch = self.receive()
        for name in ("registry.json", "identity.json", "unused.json"):
            (self.base / name).unlink()
        self.assertTrue(self.publish(batch)["publication_id"].startswith("publication_"))

    def test_changed_dictionary_requires_review_before_publish(self):
        batch = self.receive()
        dictionary = self.root / "retail_ops/data/DATA_DICTIONARY.md"
        dictionary.write_bytes(dictionary.read_bytes() + b"\nChanged synthetic rule\n")
        with self.assertRaisesRegex(ValueError, "processing rules changed"):
            self.publish(batch)

    def test_changed_recipe_or_runtime_cannot_open_an_old_publication(self):
        manifest = self.publish(self.receive())
        self.runtime = b'{"test_recipe":"2"}'
        with self.assertRaisesRegex(ValueError, "analysis rules or runtime"):
            with publication.open_publication(self.root, self.directory, manifest["publication_id"]):
                self.fail("Changed rules were accepted")

    def test_live_csv_and_old_outputs_do_not_change_pinned_evidence(self):
        batch = self.receive()
        manifest = self.publish(batch)
        for name in ("retail_ops/data/demo2_store_period_metrics.csv", "retail_ops/outputs/unselected.csv"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"unselected synthetic values,999\n")
        with publication.open_publication(self.root, self.directory, manifest["publication_id"]) as pin:
            record = json.loads((pin.root / "selected.json").read_text())[batch["batch_id"]][0]["record"]
            self.assertEqual(record["transaction_amount"], "12.3400")
            self.assertFalse((pin.root / "retail_ops/outputs/unselected.csv").exists())

    def test_artifact_changes_after_pin_cannot_change_an_active_run(self):
        manifest = self.publish(self.receive())
        target = self.directory / manifest["publication_id"] / "evidence/selected.json"
        with publication.open_publication(self.root, self.directory, manifest["publication_id"]) as pin:
            original = (pin.root / "selected.json").read_bytes()
            target.write_bytes(b"{}")
            self.assertEqual((pin.root / "selected.json").read_bytes(), original)
        with self.assertRaisesRegex(ValueError, "artifact bytes"):
            with publication.open_publication(self.root, self.directory, manifest["publication_id"]):
                self.fail("Changed artifact was accepted")

    def test_missing_added_and_symlinked_files_are_rejected(self):
        manifest = self.publish(self.receive())
        base = self.directory / manifest["publication_id"]
        target = base / "evidence/selected.json"
        original = target.read_bytes()
        for mutation in ("missing", "added", "symlink"):
            with self.subTest(mutation=mutation):
                if mutation == "missing":
                    target.unlink()
                elif mutation == "added":
                    (base / "unexpected.json").write_text("{}")
                else:
                    target.unlink()
                    target.symlink_to(self.registry_path)
                with self.assertRaises(ValueError):
                    with publication.open_publication(self.root, self.directory, manifest["publication_id"]):
                        self.fail("Unverified files were accepted")
                if target.is_symlink() or not target.exists():
                    target.unlink(missing_ok=True)
                    target.write_bytes(original)
                (base / "unexpected.json").unlink(missing_ok=True)

    def test_manifest_changes_cannot_reuse_the_original_publication_id(self):
        manifest = self.publish(self.receive())
        path = self.directory / manifest["publication_id"] / "manifest.json"
        changed = json.loads(path.read_bytes())
        changed["summary"]["facts"] = 999
        path.write_text(json.dumps(changed))
        with self.assertRaisesRegex(ValueError, "content identity"):
            with publication.open_publication(self.root, self.directory, manifest["publication_id"]):
                self.fail("Changed manifest was accepted")

    def test_build_failure_leaves_no_visible_or_staging_version(self):
        batch = self.receive()
        def fail(root, selected):
            (root / "partial.json").write_text("{}")
            raise ValueError("Synthetic interrupted build")
        with patch.object(publication.publication_recipe, "build_evidence_view", side_effect=fail):
            with self.assertRaisesRegex(ValueError, "interrupted build"):
                self.publish(batch)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_build_rule_change_leaves_no_visible_version(self):
        batch = self.receive()
        def change(root, selected):
            result = self.build_view(root, selected)
            self.runtime = b'{"test_recipe":"changed mid-build"}'
            return result
        with patch.object(publication.publication_recipe, "build_evidence_view", side_effect=change):
            with self.assertRaisesRegex(ValueError, "changed during publication"):
                self.publish(batch)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_staging_fsync_failure_keeps_previous_version_and_publishes_nothing_new(self):
        first = self.receive()
        original = self.publish(first)
        revised = self.receive("revision", source("999"), predecessor=first["batch_id"])
        with patch.object(publication.os, "fsync", side_effect=OSError("synthetic staging fsync failure")):
            with self.assertRaisesRegex(OSError, "staging fsync failure"):
                self.publish(revised)
        self.assertEqual(len(list(self.directory.glob("publication_*/manifest.json"))), 1)
        self.assertFalse(list(self.directory.glob("publication-staging-*")))
        with publication.open_publication(self.root, self.directory, original["publication_id"]) as pin:
            record = json.loads((pin.root / "selected.json").read_text())[first["batch_id"]][0]["record"]
            self.assertEqual(record["transaction_amount"], "12.3400")

    def test_concurrent_exact_publications_create_one_version(self):
        batch = self.receive()
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _: self.publish(batch), range(3)))
        self.assertEqual(len({item["publication_id"] for item in results}), 1)
        self.assertEqual(len(list(self.directory.glob("publication_*/manifest.json"))), 1)
        self.assertFalse(list(self.directory.glob("publication-staging-*")))

    def test_retry_does_not_replace_corrupted_existing_version(self):
        batch = self.receive()
        manifest = self.publish(batch)
        path = self.directory / manifest["publication_id"] / "evidence/selected.json"
        path.write_bytes(b"corrupted")
        with self.assertRaisesRegex(ValueError, "artifact bytes"):
            self.publish(batch)
        self.assertEqual(path.read_bytes(), b"corrupted")

    def test_empty_duplicate_unknown_and_path_like_batch_ids_are_rejected(self):
        batch = self.receive()
        for ids in ([], [batch["batch_id"]] * 2, ["../outside"], ["batch_" + "0" * 32]):
            with self.subTest(ids=ids):
                with self.assertRaises(ValueError):
                    publication.publish(self.root, self.database, self.directory, ids)

    def test_runtime_publication_storage_cannot_be_inside_project_or_symlinked(self):
        batch = self.receive()
        for directory in (self.root / "runtime", self.base / "linked"):
            if directory.name == "linked":
                directory.symlink_to(self.root, target_is_directory=True)
            with self.subTest(directory=directory):
                with self.assertRaises(ValueError):
                    publication.publish(self.root, self.database, directory, [batch["batch_id"]])


if __name__ == "__main__":
    unittest.main()
