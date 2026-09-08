"""Resolve operator-reviewed upload registrations independently of upload proposals."""
from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .contracts import _aware_datetime, load_dataset_contracts
from .preview import ROUTES, SCHEMAS, UploadContext
from .source_windows import validate_source_dataset, validate_source_window


REGISTRY_KEYS = {"registry_version", "bindings", "uploads"}
BINDING_KEYS = {
    "binding_id", "source_system", "source_account_id", "source_store_id", "store_id",
    "dataset_ids", "identity_evidence_path", "identity_evidence_sha256", "reviewed_by",
}
UPLOAD_KEYS = {
    "upload_id", "binding_id", "dataset_id", "period_start", "period_end",
    "file_sha256", "source_page", "extracted_at", "mapping_version",
}
TEXT_MAPPINGS = frozenset({"manual_text_v2", "manual_text_v3"})
TEXT_DATASETS = frozenset({"store_period_panel_metrics", "demo2_top_search_terms",
                           "demo2_top_skus_by_sales_volume", "demo2_top_skus_by_transaction_amount"})
DOCUMENT_UPLOAD_KEYS = {"document_id", "source_block_line"}
IDENTITY_KEYS = {"source_system", "source_account_id", "source_store_id"}
AGGREGATION_SCOPE_KEYS = {"scope_version", "timezone", "selection_conditions", "reviewed_by"}


class DocumentIntakeRequired(ValueError):
    """The selected receipt can only enter through complete document intake."""


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
    return validate_source_window(start, end, "canonical_csv_v1")


def validate_aggregation_scope(value):
    """Check a trusted operator declaration of complete non-date conditions.

    This review is independent of CSV rows and model proposals. It does not
    authenticate backend filters; the operator must verify the source export.
    """
    if value is None:
        return None
    _object(value, AGGREGATION_SCOPE_KEYS, "aggregation_scope")
    for key in AGGREGATION_SCOPE_KEYS:
        _text(value[key], key)
    if value["scope_version"] != "1":
        raise ValueError("aggregation scope version is not registered")
    if value["timezone"] in {"localtime", "posixrules"} or value["timezone"].startswith(("posix/", "right/")):
        raise ValueError("aggregation_scope timezone must be an explicit IANA timezone")
    try:
        ZoneInfo(value["timezone"])
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("aggregation_scope timezone must be an explicit IANA timezone") from exc
    return value


def aggregation_scope_sha256(scope):
    """Identify the complete reviewed declaration, never a scope name alone."""
    value = validate_aggregation_scope(scope)
    if value is None:
        return None
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def validate_upload_window(upload, registry_version):
    """Validate an explicitly registered format and its actual source coverage."""
    if registry_version not in ("1", "2", "3"):
        raise ValueError("intake registry version is not registered")
    if not isinstance(upload, dict):
        raise ValueError("upload requires the registered fields")
    manual = upload.get("mapping_version") in TEXT_MAPPINGS
    expected = UPLOAD_KEYS | (DOCUMENT_UPLOAD_KEYS if manual and registry_version == "3" else set())
    if registry_version in ("2", "3") and "aggregation_scope" in upload:
        expected = expected | {"aggregation_scope"}
    _object(upload, expected, "upload")
    for key in UPLOAD_KEYS:
        _text(upload[key], key)
    allowed = {"canonical_csv_v1"}
    if registry_version in ("2", "3"):
        allowed.add("canonical_csv_v2")
    if registry_version == "3":
        allowed.update(TEXT_MAPPINGS)
    if upload["mapping_version"] not in allowed:
        raise ValueError("source format is not registered for this intake registry version")
    window_mapping = upload["mapping_version"]
    if manual:
        _text(upload["document_id"], "document_id")
        if type(upload["source_block_line"]) is not int or upload["source_block_line"] < 1:
            raise ValueError("source_block_line must be a positive source line number")
        if upload["dataset_id"] not in TEXT_DATASETS:
            raise ValueError("manual text dataset is not registered")
        window_mapping = "canonical_csv_v1" if window_mapping == "manual_text_v2" else "canonical_csv_v2"
    if "aggregation_scope" in upload:
        if upload["mapping_version"] == "canonical_csv_v1" or upload["aggregation_scope"] is None:
            raise ValueError("aggregation_scope requires a reviewed object with a registered format")
        validate_aggregation_scope(upload["aggregation_scope"])
    _sha(upload["file_sha256"], "file_sha256")
    month = validate_source_dataset(upload["dataset_id"], window_mapping,
                                    upload["period_start"], upload["period_end"])
    _aware_datetime("extracted_at", upload["extracted_at"])
    return month


def _load_registry(root: Path, registry_path: Path):
    """Read a trusted operator registry; no mapping/context is accepted from the model.

    The identity record is a reviewed local declaration. It does not authenticate a
    Meituan account or verify that a manually prepared CSV matches a backend export.
    """
    registry_path = _regular_file(registry_path)
    registry_bytes = registry_path.read_bytes()
    registry = json.loads(registry_bytes, object_pairs_hook=_unique)
    _object(registry, REGISTRY_KEYS, "registry")
    if registry["registry_version"] not in ("1", "2", "3"):
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
        validate_upload_window(upload, registry["registry_version"])
        if upload["upload_id"] in uploads:
            raise ValueError("duplicate upload_id")
        binding = bindings.get(upload["binding_id"])
        if binding is None or upload["dataset_id"] not in binding["dataset_ids"]:
            raise ValueError("upload binding or dataset is not registered")
        uploads[upload["upload_id"]] = upload
    return registry, registry_bytes, contracts, bindings, evidence, uploads


def _resolved(upload, binding, contract, registry_bytes, identity):
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
            "identity_evidence": identity}


def resolve_upload(root: Path, registry_path: Path, upload_id: str, data: bytes):
    """Resolve one CSV receipt; text documents require complete group review."""
    _, registry_bytes, contracts, bindings, evidence, uploads = _load_registry(root, registry_path)
    _text(upload_id, "upload_id")
    if upload_id not in uploads:
        raise ValueError("upload_id has no reviewed registration")
    upload = uploads[upload_id]
    if upload["mapping_version"] in TEXT_MAPPINGS:
        raise DocumentIntakeRequired("manual text requires receive-document and whole-document validation")
    if hashlib.sha256(data).hexdigest() != upload["file_sha256"]:
        raise ValueError("upload bytes differ from the reviewed file_sha256")
    binding = bindings[upload["binding_id"]]
    return _resolved(upload, binding, contracts[upload["dataset_id"]], registry_bytes,
                     evidence[binding["binding_id"]])


def resolve_document(root: Path, registry_path: Path, document_id: str, data: bytes):
    """Resolve all independently reviewed receipts and identity bytes for a document."""
    registry, registry_bytes, contracts, bindings, evidence, uploads = _load_registry(root, registry_path)
    _text(document_id, "document_id")
    if registry["registry_version"] != "3":
        raise ValueError("manual document intake requires registry version 3")
    receipts = [value for value in uploads.values() if value.get("document_id") == document_id]
    if not receipts:
        raise ValueError("document_id has no reviewed registration")
    mappings = {value["mapping_version"] for value in receipts}
    if len(mappings) != 1 or not mappings <= TEXT_MAPPINGS:
        raise ValueError("a document requires one registered manual text format")
    digest = hashlib.sha256(data).hexdigest()
    selectors = set()
    for receipt in receipts:
        if receipt["file_sha256"] != digest:
            raise ValueError("document bytes differ from the reviewed file_sha256")
        selector = (receipt["source_block_line"], receipt["dataset_id"])
        if selector in selectors:
            raise ValueError("document receipt selects the same source group more than once")
        selectors.add(selector)
    identities = {}
    for binding in bindings.values():
        name = binding["identity_evidence_path"]
        encoded = base64.b64encode(evidence[binding["binding_id"]]).decode("ascii")
        if name in identities and identities[name] != encoded:
            raise ValueError("identity paths must not refer to conflicting evidence")
        identities[name] = encoded
    resolved = []
    for receipt in sorted(receipts, key=lambda item: item["upload_id"]):
        binding = bindings[receipt["binding_id"]]
        item = _resolved(receipt, binding, contracts[receipt["dataset_id"]], registry_bytes,
                         evidence[binding["binding_id"]])
        item["document_identity_evidence"] = dict(identities)
        resolved.append(item)
    return {"document_id": document_id, "mapping_version": next(iter(mappings)),
            "registry_bytes": registry_bytes, "resolved_uploads": resolved}
