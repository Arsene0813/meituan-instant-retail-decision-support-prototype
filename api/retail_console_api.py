"""Local operator UI; all data processing delegates to registered intake/readers."""
from __future__ import annotations

import base64
import binascii
from decimal import Decimal
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from api.retail_range_api import QueryRequest, ReviewRequest, strict_json_body
from retail_ops.ingestion import operator_console as actions
from retail_ops.ingestion import publication, preview
from retail_ops.ingestion.contracts import load_dataset_contracts
from retail_ops.ingestion.range_analysis import analyze_publication_range
from retail_ops.ingestion.source_query import query_publication


MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_BODY_BYTES = 12 * 1024 * 1024
ASSETS = Path(__file__).with_name("retail_console")


def display_value(value):
    """Console-only transport: preserve numeric text before browser JSON parsing."""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, (int, Decimal)):
        return str(value) if isinstance(value, int) else format(value, "f")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("展示数据包含非有限数值。")
        return repr(value)
    if isinstance(value, list):
        return [display_value(item) for item in value]
    if isinstance(value, dict):
        return {key: display_value(item) for key, item in value.items()}
    raise ValueError("展示数据类型未登记。")


def displayed(value):
    return {"number_encoding": "decimal_text", "data": display_value(value)}


class LocalBoundary:
    """Reject cross-site requests and bound the body before FastAPI parses it."""
    def __init__(self, app, authority, token):
        self.app, self.authority, self.token = app, authority, token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = scope.get("headers", [])
        def values(key):
            return [value for name, value in headers if name.lower() == key]
        async def reject(code, text):
            await Response(text, status_code=code, media_type="text/plain",
                           headers={"Cache-Control": "no-store"})(scope, receive, send)
        if values(b"host") != [self.authority.encode()]:
            return await reject(403, "请使用配置的本机地址打开工作台。")
        method, path = scope["method"], scope["path"]
        if method not in {"GET", "POST"}:
            return await reject(405, "此操作不支持该请求方式。")
        if path.startswith("/console/"):
            tokens = values(b"x-console-token")
            if len(tokens) != 1 or not hmac.compare_digest(tokens[0], self.token.encode()):
                return await reject(403, "页面会话已失效，请刷新工作台。")
        if method == "POST":
            if values(b"origin") != [("http://" + self.authority).encode()]:
                return await reject(403, "上传和查询必须从当前工作台页面发起。")
            content_types = values(b"content-type")
            if len(content_types) != 1 or content_types[0].split(b";")[0].strip().lower() != b"application/json":
                return await reject(415, "请求需要 application/json。")
        lengths = values(b"content-length")
        if len(lengths) > 1 or (lengths and (len(lengths[0]) > 12 or not lengths[0].isdigit() or int(lengths[0]) > MAX_BODY_BYTES)):
            return await reject(413, "请求超过本地入口大小限制。")
        body = bytearray()
        while True:
            event = await receive()
            if event["type"] == "http.disconnect":
                return
            body.extend(event.get("body", b""))
            if len(body) > MAX_BODY_BYTES:
                return await reject(413, "请求超过本地入口大小限制。")
            if not event.get("more_body", False):
                break
        sent = False
        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()
        async def protected_send(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).extend([
                    (b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"content-security-policy", b"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")])
            await send(message)
        await self.app(scope, replay, protected_send)


class ReceiveRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    kind: Literal["csv", "document"]
    selection_id: str = Field(min_length=1, max_length=512)
    registry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_base64: str = Field(max_length=((MAX_SOURCE_BYTES + 2) // 3) * 4)
    supersedes: dict[str, str] = Field(default_factory=dict)

    @field_validator("supersedes")
    @classmethod
    def explicit_predecessors(cls, value):
        if len(value) > 500 or any(not key.strip() or not re.fullmatch(publication.BATCH_PATTERN, item)
                                   for key, item in value.items()):
            raise ValueError("请提供明确的上传标识与完整前序批次 ID。")
        return value


class PublishRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    batch_ids: list[str] = Field(min_length=1, max_length=500)

    @field_validator("batch_ids")
    @classmethod
    def exact_batches(cls, value):
        if len(set(value)) != len(value) or any(not re.fullmatch(publication.BATCH_PATTERN, key) for key in value):
            raise ValueError("请选择不重复的完整批次 ID。")
        return value


def create_app(*, root=None, database=None, registry=None, directory=None, authority=None):
    root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    database = database if database is not None else os.getenv("MEITUAN_INTAKE_DATABASE")
    registry = registry if registry is not None else os.getenv("MEITUAN_INTAKE_REGISTRY")
    directory = directory if directory is not None else os.getenv("MEITUAN_PUBLICATION_DIRECTORY")
    authority = authority if authority is not None else os.getenv("MEITUAN_CONSOLE_AUTHORITY", "127.0.0.1:8001")
    if not re.fullmatch(r"127\.0\.0\.1:([1-9][0-9]{0,4})", authority) or int(authority.split(":")[1]) > 65535:
        raise ValueError("控制台地址必须是 127.0.0.1 加明确端口。")
    token = secrets.token_urlsafe(32)
    app = FastAPI(title="美团数据工作台", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(LocalBoundary, authority=authority, token=token)

    def path(value):
        return actions.external_path(root, value)

    def execute(function, *args, **kwargs):
        try:
            return displayed(function(*args, **kwargs))
        except actions.StaleRegistry as exc:
            raise HTTPException(409, detail=str(exc)) from None
        except (ValueError, OSError, sqlite3.Error) as exc:
            raise HTTPException(409, detail=str(exc)) from None

    def configured(value):
        try:
            return path(value)
        except ValueError as exc:
            raise HTTPException(503, detail=str(exc)) from None

    @app.get("/", response_class=HTMLResponse)
    def home():
        return (ASSETS / "index.html").read_text().replace("__CONSOLE_TOKEN__", token)

    @app.get("/assets/{name}")
    def asset(name: str):
        types = {"console.js": "text/javascript", "console.css": "text/css"}
        if name not in types:
            raise HTTPException(404)
        return Response((ASSETS / name).read_bytes(), media_type=types[name])

    @app.get("/health")
    def health():
        return {"status": "ready", "service": "retail_operator_console"}

    @app.get("/console/catalog")
    def catalog():
        data = {"datasets": actions.dataset_catalog(root), "registry": None, "issues": {}}
        try:
            data["registry"] = actions.registry_catalog(root, path(registry))
        except (ValueError, OSError) as exc:
            data["issues"]["registry"] = str(exc)
        for name, value in (("database", database), ("publications", directory)):
            try:
                path(value)
            except ValueError as exc:
                data["issues"][name] = str(exc)
        return displayed(data)

    @app.get("/console/batches")
    def batches(offset: int = 0):
        if offset < 0 or offset > 10_000_000:
            raise HTTPException(422, detail="无效的列表位置。")
        return execute(actions.batch_list, configured(database), offset)

    @app.get("/console/batches/{batch_id}")
    def detail(batch_id: str):
        if not re.fullmatch(publication.BATCH_PATTERN, batch_id):
            raise HTTPException(422, detail="请提供完整批次 ID。")
        return execute(actions.batch_detail, configured(database), batch_id)

    @app.get("/console/publications")
    def versions(offset: int = 0):
        if offset < 0 or offset > 10_000_000:
            raise HTTPException(422, detail="无效的列表位置。")
        return execute(actions.publication_list, root, configured(directory), offset)

    @app.post("/console/receive", dependencies=[Depends(strict_json_body)])
    def receive(request: ReceiveRequest):
        try:
            source = base64.b64decode(request.source_base64, validate=True)
            if not source or len(source) > MAX_SOURCE_BYTES or base64.b64encode(source).decode() != request.source_base64:
                raise ValueError("原文件为空、超过 8 MiB 或编码不正确。")
        except (ValueError, binascii.Error):
            raise HTTPException(422, detail="请选择不超过 8 MiB 的原文件。") from None
        return execute(actions.receive, root, configured(database), configured(registry), request.kind,
                       request.selection_id, request.registry_sha256, source, request.supersedes)

    @app.post("/console/publish", dependencies=[Depends(strict_json_body)])
    def publish(request: PublishRequest):
        return execute(publication.publish, root, configured(database), configured(directory),
                       request.batch_ids, source_records=True)

    def query_args(request):
        contract = load_dataset_contracts(root)[request.dataset_id]
        allowed = preview.SCHEMAS[request.dataset_id] - preview.KEYS - set(contract.key_fields)
        if request.fields is not None and not set(request.fields) <= allowed:
            raise HTTPException(422, detail="字段不属于所选数据分类。")
        start, end = request.dates()
        return (root, configured(directory), request.publication_id, request.dataset_id,
                request.store_ids, start, end, request.fields)

    @app.post("/console/query", dependencies=[Depends(strict_json_body)])
    def query(request: QueryRequest):
        return execute(query_publication, *query_args(request))

    @app.post("/console/review", dependencies=[Depends(strict_json_body)])
    def review(request: ReviewRequest):
        first, last = request.baseline.dates() if request.baseline else (None, None)
        return execute(analyze_publication_range, *query_args(request), baseline_start=first, baseline_end=last)

    return app


app = create_app()
