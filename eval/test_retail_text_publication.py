"""Synthetic text replay, archive integrity and field locations at publication."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from retail_ops.ingestion import batch_store, publication, source_query, source_view
from retail_ops.ingestion.contracts import load_dataset_contracts


ROOT = Path(__file__).resolve().parents[1]
PANEL = "store_period_panel_metrics"
RANKS = "demo2_top_skus_by_transaction_amount"


def sha(data):
    return hashlib.sha256(data).hexdigest()


class RetailTextPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.database = self.base / "batches.sqlite3"
        self.directory = self.base / "publications"
        self.registry_path = self.base / "registry.json"
        self.source_path = self.base / "synthetic-source.txt"
        self.data = ("\ufeff  店铺：B\r\n时间范围：2026.3\r\n成交金额：0\r\n成交订单量：2\r\n"
                     "top3交易商品成交金额：商品A（成交金额：10元），商品A（成交金额：9元），商品C（成交金额：8元）\r\n").encode()
        self.source_path.write_bytes(self.data)
        identity = {"source_system": "meituan_merchant_backend", "source_account_id": "synthetic-account",
                    "source_store_id": "synthetic-store-B"}
        self.identity_data = (json.dumps(identity, ensure_ascii=False) + "\n").encode()
        self.identity_path = self.base / "identity.json"
        self.identity_path.write_bytes(self.identity_data)
        binding = {**identity, "binding_id": "synthetic-binding-B", "store_id": "B",
                   "dataset_ids": [PANEL, RANKS], "identity_evidence_path": "identity.json",
                   "identity_evidence_sha256": sha(self.identity_data), "reviewed_by": "synthetic-operator"}
        self.registry = {"registry_version": "3", "bindings": [binding], "uploads": []}
        for index, dataset in enumerate((PANEL, RANKS)):
            self.registry["uploads"].append({"upload_id": "synthetic-text-" + str(index),
                "document_id": "synthetic-document", "source_block_line": 1,
                "binding_id": binding["binding_id"], "dataset_id": dataset,
                "period_start": "2026-03-01", "period_end": "2026-03-31",
                "file_sha256": sha(self.data), "source_page": "synthetic-reviewed-page",
                "extracted_at": "2026-04-01T00:00:00Z", "mapping_version": "manual_text_v2"})
        self.registry_path.write_text(json.dumps(self.registry), encoding="utf-8")
        result = batch_store.receive_document(ROOT, self.database, self.registry_path, "synthetic-document", self.data)
        self.assertEqual(result["status"], "validated", result["errors"])
        self.batches = [batch["batch_id"] for batch in result["batches"]]

    def publish(self):
        return publication.publish(ROOT, self.database, self.directory, self.batches, source_records=True)

    def selected(self):
        return publication._selection(self.database, self.batches)

    def test_exact_text_archive_and_same_line_distinct_rank_locations(self):
        manifest = self.publish()
        archive = self.directory / manifest["publication_id"] / "archive"
        for batch in self.batches:
            self.assertEqual((archive / batch / "source.txt").read_bytes(), self.data)
            self.assertFalse((archive / batch / "source.csv").exists())
        result = source_query.query_publication(ROOT, self.directory, manifest["publication_id"], RANKS,
            ["B"], "2026-03-01", "2026-03-31", ["sku_name", "sku_transaction_amount"])
        rows = result["stores"][0]["records"]
        self.assertEqual([row["record"]["sku_rank"] for row in rows], [1, 2, 3])
        self.assertEqual([row["record"]["sku_name"] for row in rows], ["商品A", "商品A", "商品C"])
        self.assertEqual({row["source"]["source_line_end"] for row in rows}, {5})
        positions = [row["source"]["source_locator"]["values"][0]["span"] for row in rows]
        self.assertEqual(len({value["column_start"] for value in positions}), 3)
        self.assertEqual([row["source"]["source_locator"]["item_ordinal"] for row in rows], [1, 2, 3])
        for row in rows:
            self.assertEqual(row["source"]["file_sha256"], sha(self.data))
            locator = row["source"]["source_locator"]
            self.assertEqual(locator["store"]["column_start"], 3)
            for span in [locator["store"], locator["window"], *[item["span"] for item in locator["values"]]]:
                line = self.data.decode("utf-8-sig").splitlines()[span["line"] - 1]
                self.assertEqual(line[span["column_start"] - 1:span["column_end"] - 1], span["text"])

    def test_publication_replays_archived_registration_after_external_files_removed(self):
        self.identity_path.unlink()
        self.registry_path.unlink()
        self.source_path.unlink()
        manifest = self.publish()
        self.database.unlink()
        result = source_query.query_publication(ROOT, self.directory, manifest["publication_id"], PANEL,
            ["B"], "2026-03-01", "2026-03-31", ["transaction_amount", "transaction_orders"])
        self.assertEqual(result["stores"][0]["metrics"]["transaction_amount"]["value"], "0")
        self.assertEqual(result["stores"][0]["metrics"]["transaction_orders"]["value"], 2)

    def test_monthly_projection_retains_original_text_locations(self):
        original = source_view.source_records(ROOT, self.selected())
        manifest = publication.publish(ROOT, self.database, self.directory, self.batches, source_records=False)
        for target, source_dataset in ((PANEL, PANEL), ("demo2_store_period_metrics", PANEL), (RANKS, RANKS)):
            entries = [item for item in original if item["dataset_id"] == source_dataset]
            locations = manifest["summary"]["row_lineage"][target]
            self.assertEqual(len(locations), len(entries))
            for location, entry in zip(locations, entries):
                self.assertEqual(location["source_locator"], entry["source"]["source_locator"])
                self.assertEqual(location["source_line_end"], entry["source"]["source_line_end"])
                self.assertEqual(location["batch_id"], entry["source"]["batch_id"])
        with publication.open_publication(ROOT, self.directory, manifest["publication_id"]) as pinned:
            self.assertEqual(pinned.manifest, manifest)
        for batch in self.batches:
            source = self.directory / manifest["publication_id"] / "archive" / batch / "source.txt"
            self.assertEqual(source.read_bytes(), self.data)

    def test_replay_rejects_changed_or_incomplete_identity_bundle(self):
        for replacement in ({}, {"identity.json": "e30="}, {"../identity.json": "e30="}):
            with self.subTest(bundle=replacement):
                selected = self.selected()[0]
                selected["result"]["registration"]["document_identity_evidence"] = replacement
                with self.assertRaises(ValueError):
                    publication._replay(ROOT, selected, batch_store._provenance(ROOT))

    def test_source_view_rejects_changed_value_and_physical_locator(self):
        mutations = (
            lambda row: row["record"].__setitem__("transaction_amount", "1"),
            lambda row: row["source_locator"]["values"][0]["span"].__setitem__("line", 40),
            lambda row: row.__setitem__("source_line_end", 40),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                selected = self.selected()
                item = next(item for item in selected if item["result"]["metadata"]["dataset_id"] == PANEL)
                mutate(item["result"]["preview"]["validated_records"][0])
                with self.assertRaises(ValueError):
                    source_view.source_records(ROOT, selected)

    def test_source_view_rejects_changed_original_bytes(self):
        selected = self.selected()
        selected[0]["data"] = selected[0]["data"].replace(b"\r\n", b"\n")
        with self.assertRaises(ValueError):
            source_view.source_records(ROOT, selected)

    def test_text_archive_cannot_be_missing_or_disguised_as_csv(self):
        manifest = self.publish()
        archive = self.directory / manifest["publication_id"] / "archive" / self.batches[0]
        (archive / "source.txt").rename(archive / "source.csv")
        with self.assertRaisesRegex(ValueError, "archives are incomplete"):
            publication._read_publication(ROOT, self.directory, manifest["publication_id"])

    def test_unregistered_extra_csv_archive_is_rejected(self):
        manifest = self.publish()
        archive = self.directory / manifest["publication_id"] / "archive" / self.batches[0]
        (archive / "source.csv").write_bytes(self.data)
        with self.assertRaisesRegex(ValueError, "unregistered artifact"):
            publication._read_publication(ROOT, self.directory, manifest["publication_id"])

    def test_query_rejects_incomplete_text_locator_and_format_confusion(self):
        entries = source_view.source_records(ROOT, self.selected())
        contracts = load_dataset_contracts(ROOT)
        for mutate in (
            lambda source: source.pop("source_locator"),
            lambda source: source.__setitem__("mapping_version", "canonical_csv_v1"),
            lambda source: source["source_locator"]["window"].__setitem__("text", "时间范围：2026.4"),
        ):
            with self.subTest(mutation=mutate):
                altered = deepcopy(entries)
                mutate(altered[0]["source"])
                with self.assertRaises(ValueError):
                    source_query._validate_entries(altered, contracts)


if __name__ == "__main__":
    unittest.main()
