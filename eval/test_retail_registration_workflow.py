"""Synthetic reviewed declarations must remain independent of source observations."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from retail_ops.ingestion import batch_store, intake_registry, operator_console, publication, source_query
from retail_ops.ingestion import registration_workflow as workflow

ROOT = Path(__file__).resolve().parents[1]
DATASET = "store_period_panel_metrics"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def fingerprint(base):
    return {str(p.relative_to(base)):sha(p.read_bytes()) for p in base.rglob("*") if p.is_file()}


class RegistrationWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.registry, self.plan_path, self.source, self.identity = (
            self.base/name for name in ("registry.json", "plan.json", "source.txt", "identity.json"))
        self.identity_data = {"source_system":"meituan_merchant_backend", "source_account_id":"synthetic-account",
                              "source_store_id":"synthetic-source-store-B"}
        self.identity.write_text(json.dumps(self.identity_data))
        self.binding = {**self.identity_data, "binding_id":"synthetic-binding-B", "store_id":"B",
                        "dataset_ids":[DATASET], "identity_evidence_path":"identity.json", "reviewed_by":"synthetic-operator"}
        self.receipt = {"upload_id":"synthetic-upload-1", "binding_id":"synthetic-binding-B", "dataset_id":DATASET,
                       "document_id":"synthetic-document-1", "source_block_line":1,
                       "period_start":"2026-03-01", "period_end":"2026-03-01", "source_page":"synthetic-page",
                       "extracted_at":"2026-04-01T00:00:00Z", "mapping_version":"manual_text_v3"}
        self.plan = {"plan_version":"1", "bindings":[self.binding], "uploads":[self.receipt]}
        self.registry.write_text(json.dumps({"registry_version":"3", "bindings":[], "uploads":[]}))
        self.source.write_bytes("\ufeff店铺：B\r\n时间范围2026-03-01至2026-03-01\r\n成交金额100.1234567890123456789\r\n成交订单量0\r\n搜索曝光人数\r\n".encode())
        self.save_plan()

    def save_plan(self):
        self.plan_path.write_text(json.dumps(self.plan))

    def check(self):
        return workflow.check(ROOT, self.registry, self.plan_path, self.source)

    def append(self, digest):
        return workflow.append(ROOT, self.registry, self.plan_path, self.source, digest)

    def next_plan(self):
        self.plan["bindings"] = []
        self.receipt["upload_id"] = "synthetic-upload-2"
        self.receipt["document_id"] = "synthetic-document-2"
        self.save_plan()

    def test_inspection_and_check_do_not_write_files_or_trusted_context(self):
        before = fingerprint(self.base)
        observed = workflow.inspect_text(ROOT,self.source,"manual_text_v3")
        self.assertEqual(observed["status"],"source_checked")
        self.assertFalse(observed["independent_registration_created"])
        self.assertEqual(observed["observations"][0]["observed_context"]["store_id"],"B")
        self.assertNotIn("bindings",observed)
        checked = self.check()
        self.assertEqual(checked["status"],"checked")
        self.assertEqual(checked["record_count"],1)
        self.assertEqual(checked["new_uploads"][0]["file_sha256"],sha(self.source.read_bytes()))
        self.assertEqual(checked["new_bindings"][0]["identity_evidence_sha256"],sha(self.identity.read_bytes()))
        self.assertFalse(checked["batch_created"])
        self.assertEqual(fingerprint(self.base),before)

    def test_append_then_real_intake_publish_and_query_preserve_values(self):
        old = self.registry.read_bytes()
        self.registry.chmod(0o640)
        checked = self.check()
        result = self.append(checked["check_sha256"])
        self.assertEqual(result["status"],"registered")
        self.assertEqual(Path(result["backup_path"]).read_bytes(),old)
        self.assertEqual(self.registry.stat().st_mode & 0o777,0o640)
        catalog = operator_console.registry_catalog(ROOT,self.registry)
        self.assertEqual(catalog["selections"][0]["selection_id"],self.receipt["document_id"])
        db,directory=self.base/"batches.sqlite3",self.base/"publications"
        received=batch_store.receive_document(ROOT,db,self.registry,self.receipt["document_id"],self.source.read_bytes())
        self.assertEqual(received["status"],"validated")
        manifest=publication.publish(ROOT,db,directory,[b["batch_id"] for b in received["batches"]],source_records=True)
        before=fingerprint(self.base)
        query=source_query.query_publication(ROOT,directory,manifest["publication_id"],DATASET,["B"],
            "2026-03-01","2026-03-01",["transaction_amount","transaction_orders","search_exposure_users"])
        metrics=query["stores"][0]["metrics"]
        self.assertEqual(metrics["transaction_amount"]["value"],"100.1234567890123456789")
        self.assertEqual(metrics["transaction_orders"]["value"],0)
        self.assertIsNone(metrics["search_exposure_users"]["value"])
        self.assertEqual(fingerprint(self.base),before)

    def test_old_bindings_and_uploads_remain_identical_after_append(self):
        self.append(self.check()["check_sha256"])
        old=json.loads(self.registry.read_bytes())
        self.next_plan()
        self.append(self.check()["check_sha256"])
        new=json.loads(self.registry.read_bytes())
        self.assertEqual(old["bindings"],new["bindings"])
        self.assertEqual(old["uploads"],new["uploads"][:1])

    def test_source_change_after_check_is_rejected_before_backup(self):
        checked=self.check()
        self.source.write_bytes(self.source.read_bytes().replace(b"100.",b"200."))
        before=fingerprint(self.base)
        with self.assertRaisesRegex(ValueError,"differ from the checked"):
            self.append(checked["check_sha256"])
        self.assertEqual(fingerprint(self.base),before)

    def test_identity_byte_change_after_check_is_rejected(self):
        checked=self.check()
        self.identity.write_bytes(self.identity.read_bytes()+b"\n")
        before=fingerprint(self.base)
        with self.assertRaisesRegex(ValueError,"differ from the checked"):
            self.append(checked["check_sha256"])
        self.assertEqual(fingerprint(self.base),before)

    def test_plan_registry_or_rules_change_after_check_is_rejected(self):
        for target in (self.plan_path,self.registry):
            with self.subTest(path=target.name):
                checked=self.check()
                target.write_bytes(target.read_bytes()+b"\n")
                before=fingerprint(self.base)
                with self.assertRaisesRegex(ValueError,"differ from the checked"):
                    self.append(checked["check_sha256"])
                self.assertEqual(fingerprint(self.base),before)
        checked=self.check()
        real=workflow._rules
        with patch.object(workflow,"_rules",side_effect=lambda root:{**real(root),"synthetic-change":"new"}):
            with self.assertRaisesRegex(ValueError,"differ from the checked"):
                self.append(checked["check_sha256"])

    def test_input_changes_during_check_or_before_replace_are_detected(self):
        original=self.source.read_bytes()
        real=workflow._snapshot
        from contextlib import contextmanager
        @contextmanager
        def changing(*args):
            with real(*args) as path:
                yield path
            self.source.write_bytes(original+b"\n")
        with patch.object(workflow,"_snapshot",side_effect=changing):
            with self.assertRaisesRegex(ValueError,"checked input changed"):
                self.check()
        self.source.write_bytes(original)
        checked=self.check()
        old=self.registry.read_bytes()
        backup=workflow._backup
        def changed_before_replace(*args):
            result=backup(*args)
            self.source.write_bytes(original+b"\n")
            return result
        with patch.object(workflow,"_backup",side_effect=changed_before_replace):
            with self.assertRaisesRegex(ValueError,"checked input changed"):
                self.append(checked["check_sha256"])
        self.assertEqual(self.registry.read_bytes(),old)
        self.assertFalse(list(self.base.glob(".registration-*")))

    def test_cannot_edit_binding_or_upload_or_extend_existing_document(self):
        self.append(self.check()["check_sha256"])
        original=deepcopy(self.plan)
        for mutate in (
            lambda: self.binding["dataset_ids"].append("demo2_top_search_terms"),
            lambda: self.plan.update(bindings=[]),
            lambda: (self.plan.update(bindings=[]), self.receipt.update(upload_id="synthetic-new-upload")),
        ):
            self.plan=deepcopy(original)
            self.binding=self.plan["bindings"][0]
            self.receipt=self.plan["uploads"][0]
            mutate(); self.save_plan()
            with self.assertRaises(ValueError):self.check()

    def test_manual_scope_must_be_independently_supplied_and_match_source(self):
        for field,value in (("period_start","2026-02-28"),("period_end","2026-03-02"),("source_block_line",2),
                            ("dataset_id","demo2_top_search_terms")):
            with self.subTest(field=field):
                old=self.receipt[field]; self.receipt[field]=value; self.save_plan()
                with self.assertRaises(ValueError):self.check()
                self.receipt[field]=old
        self.binding["store_id"]="C";self.save_plan()
        with self.assertRaisesRegex(ValueError,"conflicts with independently reviewed"):
            self.check()

    def test_missing_identity_or_wrong_identity_does_not_get_created_or_repaired(self):
        self.identity.unlink()
        with self.assertRaises(ValueError):self.check()
        self.assertFalse(self.identity.exists())
        self.identity.write_text(json.dumps({**self.identity_data,"source_store_id":"different"}))
        with self.assertRaisesRegex(ValueError,"conflicts with reviewed"):
            self.check()

    def test_missing_metadata_unknown_fields_and_duplicate_json_are_rejected(self):
        for key in ("source_page","extracted_at","period_start","binding_id","dataset_id"):
            with self.subTest(field=key):
                value=self.receipt.pop(key);self.save_plan()
                with self.assertRaises(ValueError):self.check()
                self.receipt[key]=value
        self.save_plan()
        for changed in (
            self.plan_path.read_text().replace('"plan_version": "1"','"plan_version":"1", "plan_version":"1"'),
            self.plan_path.read_text().replace('"source_block_line": 1','"source_block_line": true'),
            self.plan_path.read_text().replace('"source_block_line": 1','"source_block_line": NaN'),
        ):
            self.plan_path.write_text(changed)
            with self.assertRaises(ValueError):self.check()
        self.receipt["file_sha256"]="0"*64;self.save_plan()
        with self.assertRaises(ValueError):self.check()

    def test_missing_or_extra_text_group_and_duplicate_receipts_rejected(self):
        original=self.source.read_bytes()
        self.source.write_bytes(original+"top3交易商品成交金额：甲（成交金额1元），乙（成交金额2元），丙（成交金额3元）\n".encode())
        with self.assertRaisesRegex(ValueError,"cover every parsed"):
            self.check()
        self.source.write_bytes(original)
        self.plan["uploads"].append(deepcopy(self.receipt));self.save_plan()
        with self.assertRaisesRegex(ValueError,"repeated upload_id"):
            self.check()

    def test_excluded_or_invalid_source_never_creates_backup(self):
        checked=self.check()
        original=self.source.read_bytes()
        for data in (original+"有效订单数1\n".encode(),original+"无效订单数1\n".encode(),b"\xff",b""):
            self.source.write_bytes(data)
            before=fingerprint(self.base)
            with self.assertRaises(ValueError):self.append(checked["check_sha256"])
            self.assertEqual(fingerprint(self.base),before)

    def test_inspect_unknown_fields_remains_needs_review_without_trusted_receipts(self):
        self.source.write_bytes(self.source.read_bytes()+"营业额500\n".encode())
        result=workflow.inspect_text(ROOT,self.source,"manual_text_v3")
        self.assertEqual(result["status"],"needs_review")
        self.assertTrue(result["errors"])
        self.assertFalse(result["independent_registration_created"])

    def test_old_registry_version_and_unused_binding_rejected_without_upgrade(self):
        old=self.registry.read_bytes()
        for version in ("1","2"):
            self.registry.write_text(json.dumps({"registry_version":version,"bindings":[],"uploads":[]}))
            with self.assertRaisesRegex(ValueError,"does not upgrade"):
                self.check()
            self.assertEqual(json.loads(self.registry.read_bytes())["registry_version"],version)
        self.registry.write_bytes(old)
        unused={**self.binding,"binding_id":"synthetic-unused"}
        self.plan["bindings"].append(unused);self.save_plan()
        with self.assertRaisesRegex(ValueError,"must be used"):
            self.check()

    def test_source_plan_and_identity_symlinks_or_traversal_rejected(self):
        for path in (self.source,self.plan_path,self.identity):
            with self.subTest(path=path.name):
                real=path.with_suffix(path.suffix+".actual")
                path.rename(real);path.symlink_to(real)
                with self.assertRaises(ValueError):self.check()
                path.unlink();real.rename(path)
        self.binding["identity_evidence_path"]="../identity.json";self.save_plan()
        with self.assertRaises(ValueError):self.check()

    def test_check_and_append_do_not_accept_registry_in_repository(self):
        with self.assertRaisesRegex(ValueError,"项目目录之外"):
            workflow.check(ROOT,ROOT/"registry.json",self.plan_path,self.source)
        with self.assertRaises(ValueError):
            workflow.check(ROOT,self.registry,self.registry,self.source)

    def test_append_requires_exact_digest_and_repeat_cannot_rewrite(self):
        checked=self.check()
        with self.assertRaises(ValueError):self.append("bad")
        with self.assertRaises(ValueError):self.append("0"*64)
        self.append(checked["check_sha256"])
        before=fingerprint(self.base)
        with self.assertRaises(ValueError):self.append(checked["check_sha256"])
        self.assertEqual(fingerprint(self.base),before)

    def test_conflicting_or_symlink_backup_is_not_overwritten(self):
        checked=self.check()
        backup=self.registry.with_name(self.registry.name+".before-"+sha(self.registry.read_bytes())+".json")
        backup.write_bytes(b"conflicting")
        before=fingerprint(self.base)
        with self.assertRaises(ValueError):self.append(checked["check_sha256"])
        self.assertEqual(fingerprint(self.base),before)
        backup.unlink();backup.symlink_to(self.registry)
        with self.assertRaises(ValueError):self.append(checked["check_sha256"])
        self.assertTrue(backup.is_symlink())

    def test_replace_failure_retains_live_registry_and_complete_backup(self):
        checked=self.check();old=self.registry.read_bytes()
        with patch.object(workflow.os,"replace",side_effect=OSError("synthetic replacement failure")):
            with self.assertRaises(OSError):self.append(checked["check_sha256"])
        self.assertEqual(self.registry.read_bytes(),old)
        backups=list(self.base.glob("registry.json.before-*.json"))
        self.assertEqual(len(backups),1)
        self.assertEqual(backups[0].read_bytes(),old)
        self.assertFalse(list(self.base.glob(".registration-*")))
        self.assertEqual(self.append(checked["check_sha256"])["status"],"registered")

    def test_concurrent_cooperative_writer_cannot_overwrite(self):
        checked=self.check()
        before=fingerprint(self.base)
        with workflow._directory_lock(self.base):
            with self.assertRaisesRegex(ValueError,"Another registration writer"):
                self.append(checked["check_sha256"])
        self.assertEqual(fingerprint(self.base),before)

    def test_csv_actual_window_and_arbitrary_explicit_store(self):
        self.binding["store_id"]="synthetic-store-48"
        self.receipt.pop("document_id");self.receipt.pop("source_block_line")
        self.receipt["mapping_version"]="canonical_csv_v2"
        self.source.write_bytes(b"store_id,period_start,period_end,transaction_amount,transaction_orders\nsynthetic-store-48,2026-03-01,2026-03-01,,0\n")
        self.save_plan()
        checked=self.check()
        self.assertEqual(checked["kind"],"csv")
        self.assertEqual(checked["record_count"],1)
        self.append(checked["check_sha256"])
        result=batch_store.receive_batch(ROOT,self.base/"batches.sqlite3",self.registry,self.receipt["upload_id"],self.source.read_bytes())
        self.assertEqual(result["status"],"validated")

    def test_one_plan_cannot_register_multiple_documents_or_csvs(self):
        second={**self.receipt,"upload_id":"synthetic-upload-2","document_id":"synthetic-document-2"}
        self.plan["uploads"].append(second);self.save_plan()
        with self.assertRaisesRegex(ValueError,"one explicitly supplied document_id"):
            self.check()
        for item in self.plan["uploads"]:
            item.pop("document_id");item.pop("source_block_line");item["mapping_version"]="canonical_csv_v2"
        self.save_plan()
        with self.assertRaisesRegex(ValueError,"one explicit receipt"):
            self.check()

    def test_size_limits_fail_without_persistent_changes(self):
        with patch.object(workflow,"MAX_SOURCE_BYTES",2):
            with self.assertRaisesRegex(ValueError,"size limit"):
                self.check()
        with patch.object(workflow,"MAX_PLAN_BYTES",2):
            with self.assertRaisesRegex(ValueError,"size limit"):
                self.check()


if __name__=="__main__":
    unittest.main()
