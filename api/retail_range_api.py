"""Local read-only HTTP access to explicitly selected retail publications."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import sqlite3

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from retail_ops.ingestion.contracts import load_dataset_contracts
from retail_ops.ingestion.intake_registry import _unique
from retail_ops.ingestion.preview import KEYS, ROUTES, SCHEMAS
from retail_ops.ingestion.publication import PUBLICATION_PATTERN, _directory
from retail_ops.ingestion.range_analysis import analyze_publication_range
from retail_ops.ingestion.source_query import _range, month_dates, query_publication


def _nonfinite_json(value):
    raise ValueError("JSON numbers must be finite.")


def _finite_json_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON numbers must be finite.")
    return number


async def strict_json_body(request: Request):
    """Reject conflicting selectors before the typed request reaches a reader."""
    try:
        json.loads(await request.body(), object_pairs_hook=_unique,
                   parse_constant=_nonfinite_json, parse_float=_finite_json_float)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(422, detail={"code": "invalid_json",
            "message": "Use valid JSON with unique object keys and finite numbers."}) from None


class WindowSelector(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    month: str | None = None
    period_start: str | None = None
    period_end: str | None = None

    @model_validator(mode="after")
    def complete_window(self):
        if self.month is not None:
            if self.period_start is not None or self.period_end is not None:
                raise ValueError("Use month or period_start with period_end, without mixing them.")
            month_dates(self.month)
        else:
            if self.period_start is None or self.period_end is None:
                raise ValueError("Provide month or both period_start and period_end.")
            _range(self.period_start, self.period_end)
        return self

    def dates(self) -> tuple[str, str]:
        if self.month is not None:
            return month_dates(self.month)
        return self.period_start, self.period_end


class QueryRequest(WindowSelector):
    publication_id: str
    dataset_id: str
    store_ids: list[str]
    fields: list[str] | None = None

    @field_validator("publication_id")
    @classmethod
    def explicit_publication(cls, value):
        if not re.fullmatch(PUBLICATION_PATTERN, value):
            raise ValueError("Provide one complete publication ID.")
        return value

    @field_validator("dataset_id")
    @classmethod
    def registered_dataset(cls, value):
        if value not in SCHEMAS:
            raise ValueError("dataset_id must be an explicitly registered canonical dataset.")
        return value

    @field_validator("store_ids", "fields")
    @classmethod
    def distinct_exact_names(cls, value):
        if value is None:
            return value
        if (not value or any(not item or item != item.strip() for item in value)
                or len(set(value)) != len(value)):
            raise ValueError("Provide distinct, non-empty canonical names without surrounding whitespace.")
        return value


class ReviewRequest(QueryRequest):
    baseline: WindowSelector | None = None

    @model_validator(mode="after")
    def earlier_same_store_baseline(self):
        if ROUTES[self.dataset_id][0] != "store_period":
            raise ValueError("Range review requires a registered store_period dataset.")
        if self.baseline is not None:
            if len(self.store_ids) != 1:
                raise ValueError("A baseline comparison requires exactly one store.")
            _, baseline_end = self.baseline.dates()
            current_start, _ = self.dates()
            if baseline_end >= current_start:
                raise ValueError("The baseline must end before the current window begins.")
        return self


def create_app(directory: Path | str | None = None, root: Path | None = None) -> FastAPI:
    """Configure paths on the server; requests carry only registered selectors."""
    server_root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    configured_directory = directory if directory is not None else os.getenv("MEITUAN_PUBLICATION_DIRECTORY")
    application = FastAPI(title="Meituan retail range queries", version="1.0.0")

    def service_directory():
        if not configured_directory:
            raise HTTPException(503, detail={"code": "publication_directory_unconfigured",
                "message": "Configure the publication directory on the server before querying."})
        try:
            return _directory(server_root, Path(configured_directory), create=False)
        except (ValueError, OSError):
            raise HTTPException(503, detail={"code": "publication_directory_unavailable",
                "message": "The configured external publication directory is unavailable."}) from None

    def request_arguments(request: QueryRequest):
        storage = service_directory()
        try:
            contracts = load_dataset_contracts(server_root)
            contract = contracts[request.dataset_id]
        except (ValueError, OSError, KeyError):
            raise HTTPException(503, detail={"code": "dataset_contract_unavailable",
                "message": "The server dataset contract is unavailable."}) from None
        allowed = SCHEMAS[request.dataset_id] - KEYS - set(contract.key_fields)
        if request.fields is not None and any(field not in allowed for field in request.fields):
            raise HTTPException(422, detail={"code": "invalid_fields",
                "message": "fields must select registered non-key fields for dataset_id."})
        start, end = request.dates()
        return (server_root, storage, request.publication_id, request.dataset_id,
                request.store_ids, start, end, request.fields)

    def execute(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except ValueError as exc:
            if str(exc) == "Unknown publication ID.":
                raise HTTPException(404, detail={"code": "unknown_publication",
                    "message": "The requested publication is unavailable."}) from None
            raise HTTPException(409, detail={"code": "publication_verification_failed",
                "message": "The publication or requested analysis did not pass verification; no evidence was returned."}) from None
        except (OSError, sqlite3.Error):
            raise HTTPException(503, detail={"code": "publication_read_failed",
                "message": "The selected publication could not be read."}) from None

    @application.get("/health")
    def health():
        service_directory()
        return {"status": "ready"}

    @application.post("/retail/query", dependencies=[Depends(strict_json_body)])
    def query(request: QueryRequest):
        return execute(query_publication, *request_arguments(request))

    @application.post("/retail/review", dependencies=[Depends(strict_json_body)])
    def review(request: ReviewRequest):
        baseline = request.baseline.dates() if request.baseline is not None else (None, None)
        return execute(analyze_publication_range, *request_arguments(request),
                       baseline_start=baseline[0], baseline_end=baseline[1])

    return application


app = create_app()
