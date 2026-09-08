"""Exercise the local console against real intake/publication, using synthetic data."""
import base64
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import httpx

from api import retail_console_api as api
from retail_ops.ingestion import batch_store, operator_console as actions, publication

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "http://127.0.0.1:8001"
DATASET = "store_period_panel_metrics"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def fingerprint(base):
    return {str(p.relative_to(base)): sha(p.read_bytes()) for p in base.rglob("*") if p.is_file()}


class ConsoleAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.db, self.registry, self.directory = (self.base / name for name in
                                                  ("batches.sqlite3", "registry.json", "publications"))
        self.binding = {"binding_id": "synthetic-B", "source_system": "meituan_merchant_backend",
            "source_account_id": "synthetic-account", "source_store_id": "synthetic-store-B",
            "store_id": "B", "dataset_ids": [DATASET], "identity_evidence_path": "identity.json",
            "reviewed_by": "synthetic-reviewer"}
        identity = json.dumps({k:self.binding[k] for k in
                               ("source_system", "source_account_id", "source_store_id")}).encode()
        (self.base / "identity.json").write_bytes(identity)
        self.binding["identity_evidence_sha256"] = sha(identity)
        self.reg = {"registry_version": "3", "bindings": [self.binding], "uploads": []}
        self.save_registry()
        self.app = api.create_app(root=ROOT, database=self.db, registry=self.registry, directory=self.directory)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=ORIGIN)
        self.addAsyncCleanup(self.client.aclose)
        home = await self.client.get("/")
        self.token = re.search(r'name="console-token" content="([^"]+)"', home.text)[1]
        self.headers = {"Origin": ORIGIN, "X-Console-Token": self.token}

    def save_registry(self):
        self.registry.write_text(json.dumps(self.reg), encoding="utf-8")

    def registered(self, *, day="2026-03-01", amount="100.50", orders="0", serial=None):
        serial = serial or str(len(self.reg["uploads"]) + 1)
        data = ("\ufeff店铺：B\r\n时间范围"+day+"至"+day+"\r\n成交金额"+amount+
                "\r\n成交订单量"+orders+"\r\n搜索曝光人数\r\n").encode()
        receipt = {"upload_id": "upload-"+serial, "document_id": "document-"+serial,
            "source_block_line": 1, "binding_id": "synthetic-B", "dataset_id": DATASET,
            "period_start": day, "period_end": day, "file_sha256": sha(data),
            "source_page": "synthetic reviewed source", "extracted_at": "2026-05-01T00:00:00Z",
            "mapping_version": "manual_text_v3", "aggregation_scope": {"scope_version":"1",
                "timezone":"Asia/Shanghai", "selection_conditions":"synthetic all conditions",
                "reviewed_by":"synthetic-reviewer"}}
        self.reg["uploads"].append(receipt)
        self.save_registry()
        return {"kind":"document", "selection_id":receipt["document_id"],
                "registry_sha256":sha(self.registry.read_bytes()),
                "source_base64":base64.b64encode(data).decode(), "supersedes":{}}

    async def post(self, endpoint, body, expected=200):
        response = await self.client.post(endpoint, json=body, headers=self.headers)
        self.assertEqual(response.status_code, expected, response.text)
        return response.json().get("data") if expected == 200 else response

    async def ingest(self, **kwargs):
        return await self.post("/console/receive", self.registered(**kwargs))

    async def publish(self, *results):
        ids = [b["batch_id"] for r in results for b in r["batches"]]
        return await self.post("/console/publish", {"batch_ids":ids})

    def query_body(self, manifest, **kwargs):
        return {"publication_id":manifest["publication_id"], "dataset_id":DATASET,
                "store_ids":["B"], "period_start":"2026-03-01", "period_end":"2026-03-01",
                "fields":["transaction_amount", "transaction_orders", "search_exposure_users"], **kwargs}

    async def test_home_assets_health_and_empty_lists_do_not_create_runtime_files(self):
        before = fingerprint(self.base)
        for url in ("/console/catalog", "/console/batches", "/console/publications"):
            response = await self.client.get(url, headers=self.headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["number_encoding"], "decimal_text")
        for url in ("/", "/assets/console.js", "/assets/console.css", "/health"):
            response = await self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertIn("no-store", response.headers["cache-control"])
            self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        self.assertEqual(fingerprint(self.base), before)
        self.assertFalse(self.db.exists())
        self.assertFalse(self.directory.exists())

    async def test_real_text_intake_publication_and_exact_display_values(self):
        result = await self.ingest(amount="100.1234567890123456789", orders="9007199254740993")
        self.assertEqual(result["status"], "validated")
        manifest = await self.publish(result)
        query = await self.post("/console/query", self.query_body(manifest))
        values = query["stores"][0]["metrics"]
        self.assertEqual(values["transaction_orders"]["value"], "9007199254740993")
        self.assertEqual(values["transaction_amount"]["value"], "100.1234567890123456789")
        self.assertIsNone(values["search_exposure_users"]["value"])
        record = query["stores"][0]["records"][0]
        self.assertIn("source_locator", record["source"])
        self.assertEqual(record["record"]["transaction_orders"], "9007199254740993")

    async def test_zero_stays_zero_and_calendar_month_matches_same_selected_dates(self):
        manifest = await self.publish(await self.ingest(amount="0", orders="0"))
        day = await self.post("/console/query", self.query_body(manifest))
        self.assertEqual(day["stores"][0]["metrics"]["transaction_orders"]["value"], "0")
        explicit = self.query_body(manifest, period_end="2026-03-31")
        month = {k:v for k,v in explicit.items() if k not in {"period_start", "period_end"}}
        month["month"] = "2026-03"
        self.assertEqual(await self.post("/console/query", explicit), await self.post("/console/query", month))

    async def test_revision_recomputes_rac_and_read_calls_do_not_save_queries(self):
        baseline = await self.ingest(day="2026-02-28", amount="50", orders="5")
        current = await self.ingest(amount="100", orders="10")
        old = await self.publish(baseline, current)
        body = self.query_body(old, fields=["transaction_amount", "transaction_orders"],
                               baseline={"period_start":"2026-02-28", "period_end":"2026-02-28"})
        prior = await self.post("/console/review", body)
        correction = self.registered(amount="20", orders="2")
        correction["supersedes"] = {self.reg["uploads"][-1]["upload_id"]:current["batches"][0]["batch_id"]}
        revised = await self.post("/console/receive", correction)
        new = await self.publish(baseline, revised)
        body["publication_id"] = new["publication_id"]
        before = fingerprint(self.base)
        after = await self.post("/console/review", body)
        self.assertNotEqual(prior["rac"]["checks"], after["rac"]["checks"])
        self.assertNotEqual(prior["rac"]["belief_update"], after["rac"]["belief_update"])
        self.assertNotEqual(prior["rac"]["final_report"], after["rac"]["final_report"])
        for endpoint in ("/console/batches", "/console/publications", "/console/catalog"):
            self.assertEqual((await self.client.get(endpoint, headers=self.headers)).status_code, 200)
        self.assertEqual(fingerprint(self.base), before)

    async def test_quarantine_is_saved_but_cannot_be_published(self):
        body = self.registered()
        body["source_base64"] = base64.b64encode(b"unknown source text").decode()
        result = await self.post("/console/receive", body)
        self.assertEqual(result["status"], "quarantined")
        batch_id = result["quarantine_batch_id"]
        self.assertTrue(batch_id)
        self.assertEqual(result["batches"], [])
        await self.post("/console/publish", {"batch_ids":[batch_id]}, 409)
        detail = await self.client.get("/console/batches/"+batch_id, headers=self.headers)
        self.assertEqual(detail.json()["data"]["status"], "quarantined")

    async def test_unknown_selection_or_kind_mismatch_does_not_create_database(self):
        body = self.registered()
        for changes in ({"selection_id":"unknown"}, {"kind":"csv", "selection_id":self.reg["uploads"][0]["upload_id"]}):
            await self.post("/console/receive", {**body, **changes}, 409)
            self.assertFalse(self.db.exists())

    async def test_stale_registry_stops_without_creating_database(self):
        body = self.registered()
        self.reg["uploads"][0]["source_page"] = "changed reviewed page"
        self.save_registry()
        await self.post("/console/receive", body, 409)
        self.assertFalse(self.db.exists())

    async def test_checked_registry_snapshot_is_used_when_live_file_changes(self):
        body = self.registered()
        original = self.registry.read_bytes()
        real = batch_store.receive_document
        def change_live(root, database, registry, *args, **kwargs):
            self.registry.write_text("changed after the checked snapshot")
            self.assertEqual(Path(registry).read_bytes(), original)
            return real(root, database, registry, *args, **kwargs)
        with patch.object(batch_store, "receive_document", side_effect=change_live):
            result = await self.post("/console/receive", body)
        self.assertEqual(result["status"], "validated")
        archive = batch_store.read_batch_artifacts(self.db, result["batches"][0]["batch_id"])
        self.assertEqual(archive["registry_bytes"], original)

    async def test_host_origin_and_token_fail_before_mutation(self):
        body = self.registered()
        bad = [{"Host":"evil.example", **self.headers}, {"Origin":"null", "X-Console-Token":self.token},
               {"Origin":"http://evil.example", "X-Console-Token":self.token},
               {"X-Console-Token":self.token}, {"Origin":ORIGIN},
               {"Origin":ORIGIN, "X-Console-Token":"wrong"}]
        with patch.object(actions, "receive") as receive:
            for headers in bad:
                result = await self.client.post("/console/receive", json=body, headers=headers)
                self.assertEqual(result.status_code, 403)
            for key,value in (("Host","127.0.0.1:8001"), ("Origin",ORIGIN), ("X-Console-Token",self.token)):
                headers = [("Host","127.0.0.1:8001"), *self.headers.items(), (key,value)]
                result = await self.client.post("/console/receive", json=body, headers=headers)
                self.assertEqual(result.status_code, 403)
            receive.assert_not_called()
        self.assertFalse(self.db.exists())

    async def test_chunked_oversize_and_huge_length_are_rejected_before_json_parsing(self):
        async def chunks():
            yield b"x"*600
            yield b"x"*600
        with patch.object(api, "MAX_BODY_BYTES", 1024):
            response = await self.client.post("/console/receive", content=chunks(),
                headers={**self.headers, "Content-Type":"application/json"})
            self.assertEqual(response.status_code, 413)
        response = await self.client.post("/console/receive", content=b"{}",
            headers={**self.headers,"Content-Type":"application/json","Content-Length":"9"*5000})
        self.assertEqual(response.status_code, 413)
        self.assertFalse(self.db.exists())

    async def test_duplicate_json_invalid_base64_and_client_paths_are_rejected(self):
        body = self.registered()
        for changes in ({"source_base64":"!"}, {"source_base64":""}, {"registry":"/tmp/forged"},
                        {"proposals":[]}, {"database":"/tmp/forged"}):
            await self.post("/console/receive", {**body, **changes}, 422)
        text = json.dumps(body)[:-1]+',"kind":"csv"}'
        response = await self.client.post("/console/receive", content=text,
            headers={**self.headers,"Content-Type":"application/json"})
        self.assertEqual(response.status_code, 422)
        self.assertFalse(self.db.exists())

    async def test_publish_cannot_accept_a_client_path_or_duplicate_selection(self):
        batch = (await self.ingest())["batches"][0]["batch_id"]
        await self.post("/console/publish", {"batch_ids":[batch],"directory":"/tmp/forged"}, 422)
        await self.post("/console/publish", {"batch_ids":[batch,batch]}, 422)
        self.assertFalse(self.directory.exists())

    async def test_corrupt_batch_is_not_listed_as_available(self):
        await self.ingest()
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE batches SET raw_data=?", (b"corrupt",))
        response = await self.client.get("/console/batches", headers=self.headers)
        self.assertEqual(response.status_code, 409)

    async def test_corrupt_publication_is_not_selectable(self):
        manifest = await self.publish(await self.ingest())
        target = self.directory / manifest["publication_id"] / "manifest.json"
        target.write_text("corrupt")
        result = await self.client.get("/console/publications", headers=self.headers)
        item = result.json()["data"]["items"][0]
        self.assertFalse(item["available"])
        await self.post("/console/query", self.query_body(manifest), 409)

    async def test_list_pagination_uses_verified_rows_without_new_writes(self):
        await self.ingest()
        before = fingerprint(self.base)
        first = actions.batch_list(self.db, 0, 1)
        empty = actions.batch_list(self.db, 1, 1)
        self.assertEqual(len(first["items"]), 1)
        self.assertEqual(empty["items"], [])
        self.assertEqual(fingerprint(self.base), before)

    async def test_missing_registry_does_not_block_other_read_functions(self):
        self.registry.unlink()
        result = await self.client.get("/console/catalog", headers=self.headers)
        self.assertIsNone(result.json()["data"]["registry"])
        self.assertIn("registry", result.json()["data"]["issues"])
        self.assertEqual((await self.client.get("/console/batches", headers=self.headers)).status_code, 200)

    async def test_batch_detail_omits_identity_byte_bundle_but_keeps_record_sources(self):
        batch = (await self.ingest())["batches"][0]["batch_id"]
        response = await self.client.get("/console/batches/"+batch, headers=self.headers)
        data = response.json()["data"]
        self.assertNotIn("document_identity_evidence", data["registration"])
        self.assertIn("source_locator", data["preview"]["validated_records"][0])
        self.assertIn("document_identity_evidence", batch_store.read_batch(self.db, batch)["registration"])

    def test_display_encoding_keeps_boolean_null_and_exact_numbers(self):
        self.assertEqual(api.display_value({"i":9007199254740993,"z":0,"n":None,"b":True}),
                         {"i":"9007199254740993","z":"0","n":None,"b":True})

    def test_catalog_only_exposes_registered_fields_without_gross_revenue_alias(self):
        dataset = next(x for x in actions.dataset_catalog(ROOT) if x["dataset_id"]==DATASET)
        fields = {item["name"]:item["label"] for item in dataset["fields"]}
        self.assertNotIn("gross_revenue", fields)
        self.assertEqual(fields["transaction_amount"], "成交金额")

    def test_paths_inside_repository_or_symlinks_are_rejected(self):
        with self.assertRaises(ValueError):
            actions.external_path(ROOT, ROOT / "data.sqlite3")
        link = self.base / "link"
        link.symlink_to(self.registry)
        with self.assertRaises(ValueError):
            actions.external_path(ROOT, link)


if __name__ == "__main__":
    unittest.main()
