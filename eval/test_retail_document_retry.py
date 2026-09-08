"""Existing text archives remain authoritative after unrelated registry additions."""
from copy import deepcopy
from contextlib import redirect_stdout
from io import StringIO
import json
import sqlite3
import stat
import unittest
from unittest.mock import patch

from eval import test_retail_registration_workflow as fixtures
from retail_ops.ingestion import batch_cli, batch_store, document_intake, operator_console
from retail_ops.ingestion import registration_workflow as workflow

ROOT=fixtures.ROOT


class DocumentRetryTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.RegistrationWorkflowTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.append(self.f.check()["check_sha256"])
        self.db=self.f.base/"batches.sqlite3"
        self.document=self.f.receipt["document_id"]
        self.original=self.f.source.read_bytes()
        # The old core produces the exact archives that existed before the facade.
        self.first=batch_store.receive_document(ROOT,self.db,self.f.registry,self.document,self.original)
        self.assertEqual(self.first["status"],"validated")

    def add_unrelated_store(self):
        f=self.f
        identity={**f.identity_data,"source_store_id":"synthetic-source-store-C"}
        (f.base/"identity-C.json").write_text(json.dumps(identity))
        binding={**f.binding,**identity,"binding_id":"synthetic-binding-C","store_id":"C",
                 "identity_evidence_path":"identity-C.json"}
        receipt={**f.receipt,"binding_id":"synthetic-binding-C","document_id":"synthetic-document-C",
                 "upload_id":"synthetic-upload-C"}
        f.plan={"plan_version":"1","bindings":[binding],"uploads":[receipt]}
        f.save_plan()
        f.source.write_bytes(self.original.replace("店铺：B".encode(),"店铺：C".encode()))
        f.append(f.check()["check_sha256"])
        f.source.write_bytes(self.original)

    def retry(self,**kwargs):
        return document_intake.receive_document(ROOT,self.db,self.f.registry,self.document,self.original,**kwargs)

    def test_unrelated_new_store_preserves_original_ids_and_all_archive_bytes(self):
        self.add_unrelated_store()
        before=fixtures.fingerprint(self.f.base)
        result=self.retry()
        self.assertEqual(result["status"],"validated")
        self.assertTrue(result["idempotent"])
        self.assertEqual([b["batch_id"] for b in result["batches"]],[b["batch_id"] for b in self.first["batches"]])
        for actual,expected in zip(result["batches"],self.first["batches"]):
            self.assertEqual(actual,{**expected,"idempotent":True})
        self.assertEqual(fixtures.fingerprint(self.f.base),before)

    def test_console_and_cli_both_use_the_replayed_retry(self):
        self.add_unrelated_store()
        before=fixtures.fingerprint(self.f.base)
        result=operator_console.receive(ROOT,self.db,self.f.registry,"document",self.document,
                                         fixtures.sha(self.f.registry.read_bytes()),self.original,{})
        self.assertTrue(result["idempotent"])
        out=StringIO()
        with redirect_stdout(out):
            status=batch_cli.main(["receive-document","--database",str(self.db),"--registry",str(self.f.registry),
                                   "--document-id",self.document,"--input",str(self.f.source)])
        self.assertEqual(status,0)
        self.assertTrue(json.loads(out.getvalue())["idempotent"])
        self.assertEqual(fixtures.fingerprint(self.f.base),before)

    def test_changed_selected_receipt_does_not_return_old_success(self):
        self.add_unrelated_store()
        registry=json.loads(self.f.registry.read_bytes())
        registry["uploads"][0]["source_page"]="different reviewed page"
        self.f.registry.write_text(json.dumps(registry))
        result=self.retry()
        self.assertEqual(result["status"],"quarantined")
        self.assertEqual(result["batches"],[])

    def test_preexisting_unrelated_identity_cannot_be_removed_or_changed(self):
        self.add_unrelated_store()
        registry_bytes=self.f.registry.read_bytes()
        identity_path=self.f.base/"identity-C.json"
        identity_bytes=identity_path.read_bytes()
        for mode in ("remove","change"):
            with self.subTest(mode=mode):
                self.f.registry.write_bytes(registry_bytes)
                identity_path.write_bytes(identity_bytes)
                db=self.f.base/("retry-"+mode+".sqlite3")
                initial=batch_store.receive_document(ROOT,db,self.f.registry,self.document,self.original)
                self.assertEqual(initial["status"],"validated")
                registry=json.loads(registry_bytes)
                if mode=="remove":
                    registry["bindings"]=registry["bindings"][:1]
                    registry["uploads"]=registry["uploads"][:1]
                else:
                    identity_path.write_bytes(identity_bytes+b"\n")
                    registry["bindings"][1]["identity_evidence_sha256"]=fixtures.sha(identity_path.read_bytes())
                self.f.registry.write_text(json.dumps(registry))
                retry=document_intake.receive_document(ROOT,db,self.f.registry,self.document,self.original)
                self.assertEqual(retry["status"],"quarantined")

    def test_changed_source_or_source_identity_does_not_return_old_success(self):
        self.add_unrelated_store()
        changed=self.original.replace(b"100.",b"200.")
        result=document_intake.receive_document(ROOT,self.db,self.f.registry,self.document,changed)
        self.assertEqual(result["status"],"quarantined")
        self.f.identity.write_bytes(self.f.identity.read_bytes()+b"\n")
        self.assertEqual(self.retry()["status"],"quarantined")

    def test_current_missing_receipt_and_extra_member_are_not_ignored(self):
        registry=json.loads(self.f.registry.read_bytes())
        for uploads in ([],registry["uploads"]+[{**registry["uploads"][0],"upload_id":"synthetic-extra"}]):
            changed={**registry,"uploads":uploads}
            self.f.registry.write_text(json.dumps(changed))
            self.assertEqual(self.retry()["status"],"quarantined")

    def test_changed_proposals_or_revision_map_requires_normal_intake(self):
        self.add_unrelated_store()
        self.assertEqual(self.retry(proposals=[])["status"],"quarantined")
        self.assertEqual(self.retry(supersedes={self.f.receipt["upload_id"]:"batch_"+"a"*32})["status"],"quarantined")
        for supersedes in ([],{"x":False}):
            with self.assertRaises(ValueError):self.retry(supersedes=supersedes)

    def test_excluded_fields_stop_before_existing_success_can_be_returned(self):
        before=fixtures.fingerprint(self.f.base)
        with self.assertRaises(ValueError):
            document_intake.receive_document(ROOT,self.db,self.f.registry,self.document,self.original+"有效订单数1".encode())
        with self.assertRaises(ValueError):
            self.retry(proposals={"有效订单数":1})
        self.assertEqual(fixtures.fingerprint(self.f.base),before)

    def test_archived_replay_failure_cannot_fall_back_to_a_successful_hash_lookup(self):
        before=fixtures.fingerprint(self.f.base)
        with patch.object(document_intake.publication,"_replay",side_effect=ValueError("synthetic damaged replay")):
            with self.assertRaisesRegex(ValueError,"damaged replay"):
                self.retry()
        self.assertEqual(fixtures.fingerprint(self.f.base),before)

    def test_corrupt_unrelated_ledger_row_blocks_retry(self):
        self.add_unrelated_store()
        registry=json.loads(self.f.registry.read_bytes())
        other=next(row for row in registry["uploads"] if row["document_id"]!=self.document)
        data=self.original.replace("店铺：B".encode(),"店铺：C".encode())
        result=batch_store.receive_document(ROOT,self.db,self.f.registry,other["document_id"],data)
        with sqlite3.connect(self.db) as connection:
            connection.execute("UPDATE batches SET result_sha256=? WHERE batch_id=?",("0"*64,result["batches"][0]["batch_id"]))
        before=fixtures.fingerprint(self.f.base)
        with self.assertRaises(ValueError):self.retry()
        self.assertEqual(fixtures.fingerprint(self.f.base),before)

    def test_prior_different_byte_document_quarantine_still_blocks_retry(self):
        batch_store.receive_document(ROOT,self.db,self.f.registry,self.document,self.original+b"different")
        self.assertEqual(self.retry()["status"],"quarantined")

    def test_provenance_mismatch_and_mid_replay_rule_change_stop_success(self):
        real=batch_store._provenance
        with patch.object(batch_store,"_provenance",side_effect=lambda root:{**real(root),"synthetic_rule":"changed"}):
            self.assertEqual(self.retry()["status"],"quarantined")
        # Only matching existing successful children are eligible for replay.
        replay=document_intake.publication._replay
        def change_rule(root,item,provenance):
            replay(root,item,provenance)
            provenance["synthetic-after-replay"]="changed"
        with patch.object(document_intake.publication,"_replay",side_effect=change_rule):
            with self.assertRaisesRegex(ValueError,"rules changed"):
                self.retry()

    def test_directory_sync_failure_reports_that_registration_was_replaced(self):
        self.f.next_plan()
        checked=self.f.check()
        real=workflow.os.fsync
        def fail_directory(fd):
            if stat.S_ISDIR(workflow.os.fstat(fd).st_mode):
                raise OSError("synthetic directory sync failure")
            return real(fd)
        old=self.f.registry.read_bytes()
        with patch.object(workflow.os,"fsync",side_effect=fail_directory):
            with self.assertRaisesRegex(OSError,"complete registry was replaced"):
                self.f.append(checked["check_sha256"])
        self.assertNotEqual(self.f.registry.read_bytes(),old)
        self.assertEqual(len(json.loads(self.f.registry.read_bytes())["uploads"]),2)


if __name__=="__main__":
    unittest.main()
