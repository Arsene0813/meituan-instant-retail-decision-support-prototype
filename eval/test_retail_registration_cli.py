"""Local registration commands must expose checks before writes."""
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import unittest

from retail_ops.ingestion import registration_cli
from eval import test_retail_registration_workflow as fixtures


class RegistrationCLITests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.RegistrationWorkflowTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def args(self,command):
        f=self.fixture
        return [command,"--registry",str(f.registry),"--plan",str(f.plan_path),"--input",str(f.source)]

    def invoke(self,args):
        out=StringIO()
        with redirect_stdout(out):
            code=registration_cli.main(args)
        return code,json.loads(out.getvalue())

    def test_inspect_check_append_and_console_catalog_sequence(self):
        f=self.fixture
        code,observed=self.invoke(["inspect-text","--input",str(f.source),"--mapping-version","manual_text_v3"])
        self.assertEqual(code,0)
        self.assertFalse(observed["independent_registration_created"])
        code,checked=self.invoke(self.args("check"))
        self.assertEqual(code,0)
        self.assertEqual(checked["status"],"checked")
        code,result=self.invoke(self.args("append")+["--expected-check-sha256",checked["check_sha256"]])
        self.assertEqual(code,0)
        self.assertEqual(result["status"],"registered")
        self.assertEqual(len(json.loads(f.registry.read_bytes())["uploads"]),1)
        self.assertFalse((f.base/"batches.sqlite3").exists())

    def test_malformed_plan_exits_two_without_partial_stdout_or_mutation(self):
        f=self.fixture
        f.plan_path.write_text('{"plan_version":"1", "plan_version":"1"}')
        before=fixtures.fingerprint(f.base)
        out,err=StringIO(),StringIO()
        with redirect_stdout(out),redirect_stderr(err),self.assertRaises(SystemExit) as raised:
            registration_cli.main(self.args("check"))
        self.assertEqual(raised.exception.code,2)
        self.assertEqual(out.getvalue(),"")
        self.assertIn("duplicate",err.getvalue())
        self.assertEqual(fixtures.fingerprint(f.base),before)

    def test_unknown_text_label_exits_two_with_observed_errors(self):
        f=self.fixture
        f.source.write_bytes(f.source.read_bytes()+"营业额999\n".encode())
        code,result=self.invoke(["inspect-text","--input",str(f.source),"--mapping-version","manual_text_v3"])
        self.assertEqual(code,2)
        self.assertEqual(result["status"],"needs_review")

    def test_append_requires_check_digest_and_rejects_unregistered_flags(self):
        for extra in ([],["--expected-check-sha256","0"*64,"--database","not-supported"]):
            with redirect_stderr(StringIO()),self.assertRaises(SystemExit) as raised:
                registration_cli.main(self.args("append")+extra)
            self.assertEqual(raised.exception.code,2)

    def test_cli_does_not_repair_missing_identity(self):
        f=self.fixture
        f.identity.unlink()
        with redirect_stderr(StringIO()),self.assertRaises(SystemExit) as raised:
            registration_cli.main(self.args("check"))
        self.assertEqual(raised.exception.code,2)
        self.assertFalse(f.identity.exists())

    def test_review_plan_schema_matches_explicit_runtime_contract(self):
        import jsonschema
        f=self.fixture
        schema=json.loads((fixtures.ROOT/"retail_ops/contracts/registration_plan.v1.schema.json").read_bytes())
        jsonschema.Draft202012Validator.check_schema(schema)
        validator=jsonschema.Draft202012Validator(schema)
        validator.validate(f.plan)
        f.receipt["aggregation_scope"]={"scope_version":"1","timezone":"Asia/Shanghai",
            "selection_conditions":"synthetic reviewed complete filters","reviewed_by":"synthetic-operator"}
        validator.validate(f.plan)
        f.save_plan()
        self.assertEqual(f.check()["status"],"checked")
        f.receipt["file_sha256"]="0"*64
        self.assertTrue(list(validator.iter_errors(f.plan)))


if __name__=="__main__":
    unittest.main()
