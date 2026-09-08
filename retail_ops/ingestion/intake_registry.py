"""Resolve operator-reviewed upload registrations independently of upload proposals."""
from __future__ import annotations

import calendar
import hashlib
import json
import re
from datetime import date
from pathlib import Path

from .contracts import _aware_datetime, load_dataset_contracts
from .preview import ROUTES, SCHEMAS, UploadContext


REGISTRY_KEYS = {"registry_version", "bindings", "uploads"}
BINDING_KEYS = {
    "binding_id", "source_system", "source_account_id", "source_store_id", "store_id",
    "dataset_ids", "identity_evidence_path", "identity_evidence_sha256", "reviewed_by",
}
UPLOAD_KEYS = {
    "upload_id", "binding_id", "dataset_id", "period_start", "period_end",
    "file_sha256", "source_page", "extracted_at", "mapping_version",
}
IDENTITY_KEYS = {"source_system", "source_account_id", "source_store_id"}


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate registration JSON key")
        result[key] = value
    return result


def _object(value, keys, label):
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"{label} requires exactly the registered fields")


def _text(value, label):
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be non-empty text without surrounding whitespace")
    return value


def _sha(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError(f"{label} must be lowercase SHA-256")


def _regular_file(path):
    path = Path(path).absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("registration and identity files must not use symlinks")
    if not path.is_file():
        raise ValueError("registration or identity file is missing")
    return path


def _identity(base, binding):
    relative = Path(binding["identity_evidence_path"])
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("identity_evidence_path must stay within the registry directory")
    path = _regular_file(base / relative)
    if base.resolve() not in path.resolve().parents:
        raise ValueError("identity_evidence_path must stay within the registry directory")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != binding["identity_evidence_sha256"]:
        raise ValueError("identity evidence hash does not match reviewed binding")
    identity = json.loads(data, object_pairs_hook=_unique)
    _object(identity, IDENTITY_KEYS, "identity evidence")
    for key in IDENTITY_KEYS:
        if _text(identity[key], key) != binding[key]:
            raise ValueError("identity evidence conflicts with reviewed binding")
    return data


def _month(start, end):
    for value in (start, end):
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError("upload window requires YYYY-MM-DD")
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if first.day != 1 or last != date(first.year, first.month, calendar.monthrange(first.year, first.month)[1]):
        raise ValueError("current intake requires a complete calendar-month window")


def resolve_upload(root: Path, registry_path: Path, upload_id: str, data: bytes):
    """Read a trusted operator registry; no mapping/context is accepted from the model.

    The identity record is a reviewed local declaration. It does not authenticate a
    Meituan account or verify that a manually prepared CSV matches a backend export.
    """
    registry_path = _regular_file(registry_path)
    registry_bytes = registry_path.read_bytes()
    registry = json.loads(registry_bytes, object_pairs_hook=_unique)
    _object(registry, REGISTRY_KEYS, "registry")
    if registry["registry_version"] != "1":
        raise ValueError("intake registry version is not registered")
    if not isinstance(registry["bindings"], list) or not isinstance(registry["uploads"], list):
        raise ValueError("bindings and uploads must be lists")
    contracts = load_dataset_contracts(root)
    bindings, identities, evidence = {}, set(), {}
    for binding in registry["bindings"]:
        _object(binding, BINDING_KEYS, "binding")
        for key in BINDING_KEYS - {"dataset_ids"}:
            _text(binding[key], key)
        key = binding["binding_id"]
        source = tuple(binding[field] for field in sorted(IDENTITY_KEYS))
        if key in bindings or source in identities:
            raise ValueError("duplicate binding ID or source identity")
        datasets = binding["dataset_ids"]
        if not isinstance(datasets, list) or not datasets or any(not isinstance(item, str) for item in datasets):
            raise ValueError("dataset_ids must be a non-empty list of registered datasets")
        if len(datasets) != len(set(datasets)):
            raise ValueError("duplicate dataset in binding")
        for dataset_id in datasets:
            contract = contracts.get(dataset_id)
            if contract is None or dataset_id not in SCHEMAS or dataset_id not in ROUTES:
                raise ValueError("dataset requires a registered contract, field schema and route")
            if contract.source_system != binding["source_system"]:
                raise ValueError("binding source_system conflicts with dataset")
            if (contract.grain, contract.ranking_basis) != ROUTES[dataset_id]:
                raise ValueError("dataset route requires review")
            keys = ("store_id", "period_start", "period_end")
            if contract.grain == "store_sku_period":
                keys += ("sku_rank",)
            elif contract.grain == "store_search_term_period":
                keys += ("search_term_rank",)
            if contract.key_fields != keys:
                raise ValueError("dataset key fields require review")
            if contract.snapshot_semantics != "cumulative_period_snapshot":
                raise ValueError("incremental extracts require a separately registered intake workflow")
        _sha(binding["identity_evidence_sha256"], "identity_evidence_sha256")
        evidence[key] = _identity(registry_path.parent, binding)
        bindings[key] = binding
        identities.add(source)
    uploads = {}
    for upload in registry["uploads"]:
        _object(upload, UPLOAD_KEYS, "upload")
        for key in UPLOAD_KEYS:
            _text(upload[key], key)
        if upload["upload_id"] in uploads:
            raise ValueError("duplicate upload_id")
        binding = bindings.get(upload["binding_id"])
        if binding is None or upload["dataset_id"] not in binding["dataset_ids"]:
            raise ValueError("upload binding or dataset is not registered")
        if upload["mapping_version"] != "canonical_csv_v1":
            raise ValueError("source format is not registered for persistent intake")
        _sha(upload["file_sha256"], "file_sha256")
        _month(upload["period_start"], upload["period_end"])
        _aware_datetime("extracted_at", upload["extracted_at"])
        uploads[upload["upload_id"]] = upload
    _text(upload_id, "upload_id")
    if upload_id not in uploads:
        raise ValueError("upload_id has no reviewed registration")
    upload = uploads[upload_id]
    if hashlib.sha256(data).hexdigest() != upload["file_sha256"]:
        raise ValueError("upload bytes differ from the reviewed file_sha256")
    binding = bindings[upload["binding_id"]]
    contract = contracts[upload["dataset_id"]]
    context = UploadContext(contract.dataset_id, binding["store_id"], upload["period_start"],
                            upload["period_end"], contract.grain, contract.ranking_basis)
    metadata = {
        "dataset_id": contract.dataset_id, "source_system": contract.source_system,
        "source_name": contract.source_name, "source_page": upload["source_page"],
        "extracted_at": upload["extracted_at"], "file_sha256": upload["file_sha256"],
        "mapping_version": upload["mapping_version"], "snapshot_semantics": contract.snapshot_semantics,
        "coverage_start": upload["period_start"], "coverage_end": upload["period_end"],
    }
    return {"context": context, "metadata": metadata, "receipt": upload, "binding": binding,
            "registry_sha256": hashlib.sha256(registry_bytes).hexdigest(), "registry_bytes": registry_bytes,
            "identity_evidence": evidence[binding["binding_id"]]}
