"""Persist reviewed source intake and explicit revisions without publishing data."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from . import contracts as contract_module
from . import intake_registry
from . import preview as preview_module
from . import source_windows
from .contracts import build_batch_metadata, load_dataset_contracts

APPLICATION_ID = 0x4D545249
SCHEMA_VERSION = 1
CONTROL_COLUMNS = (
    "batch_id", "upload_id", "status", "retry_key", "file_sha256", "registry_sha256",
    "identity_sha256", "scope_key", "upload_payload_key", "supersedes_batch_id",
    "result_json", "result_sha256",
)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False, default=lambda item: format(item, "f")
                      if isinstance(item, Decimal) else _unsupported(item))


def _unsupported(value):
    raise ValueError(f"Unsupported intake JSON type: {type(value).__name__}")


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _file_bytes(path):
    try:
        return path.read_bytes()
    except OSError:
        return None


def _provenance(root):
    from . import text_document, text_preview
    files = {
        "batch_store": Path(__file__), "intake_registry": Path(intake_registry.__file__),
        "preview": Path(preview_module.__file__), "contracts": Path(contract_module.__file__),
        "source_windows": Path(source_windows.__file__),
        "dataset_registry": root / contract_module.DEFAULT_REGISTRY_PATH,
        "dictionary": root / "retail_ops/data/DATA_DICTIONARY.md",
        "text_preview": Path(text_preview.__file__),
        "text_document": Path(text_document.__file__),
        "manual_text_v2": root / "retail_ops/contracts/manual_text.v2.json",
        "manual_text_v3": root / "retail_ops/contracts/manual_text.v3.json",
    }
    return {name + "_sha256": _hash(data) if (data := _file_bytes(path)) is not None else None
            for name, path in files.items()}


def _reject_excluded_proposals(value):
    if isinstance(value, dict):
        if set(value) & preview_module.IGNORED:
            raise ValueError("Excluded order-count fields cannot be archived in proposals.")
        for item in value.values():
            _reject_excluded_proposals(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_excluded_proposals(item)


def _reject_excluded_columns(data):
    try:
        header = next(csv.reader(io.StringIO(data.decode("utf-8-sig"), newline=""), strict=True), [])
    except (UnicodeError, csv.Error) as exc:
        raise ValueError("Cannot verify excluded columns in malformed CSV; no bytes were archived.") from exc
    if set(header) & preview_module.IGNORED:
        raise ValueError("Excluded order-count columns cannot be archived; remove those columns before intake.")
    if not {"store_id", "period_start", "period_end"} <= set(header) and any(
            label.encode("utf-8") in data for label in preview_module.IGNORED):
        # A text prelude or unknown upload ID must not route excluded source
        # labels into the CSV quarantine archive. Canonical cell text retains
        # its existing field semantics, including literal words in SKU names.
        from .text_document import reject_excluded_text
        reject_excluded_text(data)


def _open(database):
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN IMMEDIATE")
        application_id = connection.execute("PRAGMA application_id").fetchone()[0]
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if (application_id, version) == (0, 0) and not tables:
            connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            connection.execute("""CREATE TABLE batches (
                batch_id TEXT PRIMARY KEY, upload_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('validated','quarantined')),
                retry_key TEXT NOT NULL UNIQUE, file_sha256 TEXT NOT NULL,
                raw_data BLOB NOT NULL, registry_bytes BLOB, registry_sha256 TEXT,
                identity_evidence BLOB, identity_sha256 TEXT,
                scope_key TEXT, upload_payload_key TEXT,
                supersedes_batch_id TEXT, result_json TEXT NOT NULL,
                result_sha256 TEXT NOT NULL)""")
            connection.execute("CREATE INDEX batches_upload ON batches(upload_id)")
            connection.execute("CREATE INDEX batches_scope ON batches(scope_key,status)")
            connection.execute("""CREATE UNIQUE INDEX batches_one_validated_successor
                ON batches(supersedes_batch_id)
                WHERE status='validated' AND supersedes_batch_id IS NOT NULL""")
        elif (application_id, version) != (APPLICATION_ID, SCHEMA_VERSION):
            raise ValueError("The database is not a supported retail intake database.")
    except BaseException:
        connection.rollback()
        connection.close()
        raise
    return connection


def _verify_control(row):
    """Bind lookup and revision columns to the checked stored result."""
    if row is None:
        raise ValueError("Unknown intake batch_id.")
    serialized = row["result_json"]
    if _hash(serialized.encode()) != row["result_sha256"]:
        raise ValueError("Archived intake result does not match its SHA-256.")
    result = json.loads(serialized)
    for key in ("batch_id", "upload_id", "status", "file_sha256", "supersedes_batch_id"):
        if result.get(key) != row[key]:
            raise ValueError("Archived intake result conflicts with ledger field " + key + ".")
    if result.get("registry_sha256") != row["registry_sha256"]:
        raise ValueError("Archived registry digest conflicts with the intake result.")
    if _hash(_json(result.get("proposals")).encode()) != result.get("proposals_sha256"):
        raise ValueError("Archived proposals do not match their SHA-256.")
    registration = result.get("registration")
    try:
        expected_scope = _scope(registration) if registration is not None else None
        expected_payload = _hash(_json(registration).encode()) if registration is not None else None
        expected_identity = registration["identity_evidence_sha256"] if registration is not None else None
        expected_retry = _retry_key(
            result["upload_id"], result["file_sha256"], registration,
            result["registry_sha256"], result["proposals_sha256"],
            result["supersedes_batch_id"], result["provenance"],
        )
    except (KeyError, TypeError) as exc:
        raise ValueError("Archived intake control metadata is incomplete.") from exc
    for key, expected in (("scope_key", expected_scope), ("upload_payload_key", expected_payload),
                          ("identity_sha256", expected_identity), ("retry_key", expected_retry)):
        if row[key] != expected:
            raise ValueError("Archived intake control field conflicts with the checked result: " + key + ".")
    return result


def _decode(row):
    result = _verify_control(row)
    if _hash(bytes(row["raw_data"])) != row["file_sha256"]:
        raise ValueError("Archived source bytes do not match their SHA-256.")
    for column, digest in (("registry_bytes", "registry_sha256"), ("identity_evidence", "identity_sha256")):
        data = row[column]
        if (data is None) != (row[digest] is None) or (data is not None and _hash(bytes(data)) != row[digest]):
            raise ValueError("Archived " + column + " do not match their SHA-256.")
    return result


def _scope(registration):
    context = registration["context"]
    binding = registration["binding"]
    scope = {
        "binding_id": binding["binding_id"], "source_system": binding["source_system"],
        "source_account_id": binding["source_account_id"], "source_store_id": binding["source_store_id"],
        "dataset_id": context["dataset_id"], "store_id": context["store_id"],
        "period_start": context["period_start"], "period_end": context["period_end"],
        "grain": context["grain"], "ranking_basis": context["ranking_basis"],
    }
    # Absent review keeps the exact legacy scope encoding. A reviewed filter
    # change is a different source scope and cannot overwrite its predecessor.
    receipt = registration["receipt"]
    if "aggregation_scope" in receipt:
        digest = intake_registry.aggregation_scope_sha256(receipt["aggregation_scope"])
        if digest is None:
            raise ValueError("A recorded aggregation_scope must be a reviewed object.")
        scope["aggregation_scope_sha256"] = digest
    return _json(scope)


def _retry_key(upload_id, file_hash, registration, registry_hash, proposals_hash, predecessor, provenance):
    return _hash(_json({
        "upload_id": upload_id, "file_sha256": file_hash,
        "registration": registration if registration is not None else {"unresolved_registry_sha256": registry_hash},
        "proposals_sha256": proposals_hash, "supersedes_batch_id": predecessor,
        "provenance": provenance,
    }).encode())


def _registration(resolved):
    if resolved is None:
        return None
    registration = {"receipt": resolved["receipt"], "binding": resolved["binding"],
                    "context": asdict(resolved["context"]),
                    "identity_evidence_sha256": _hash(resolved["identity_evidence"])}
    if "document_identity_evidence" in resolved:
        registration["document_identity_evidence"] = resolved["document_identity_evidence"]
    return registration


def _prepare(upload_id, data, registry_bytes, resolved, checked, proposals, predecessor, provenance, errors):
    registration = _registration(resolved)
    registry_hash = _hash(registry_bytes) if registry_bytes is not None else None
    proposals = json.loads(_json(proposals))
    proposals_hash = _hash(_json(proposals).encode())
    file_hash = _hash(data)
    return {"upload_id": upload_id, "data": data, "registry_bytes": registry_bytes,
            "resolved": resolved, "preview": checked, "proposals": proposals,
            "supersedes_batch_id": predecessor, "provenance": provenance, "errors": list(errors),
            "registration": registration, "registry_hash": registry_hash,
            "proposals_hash": proposals_hash, "file_hash": file_hash,
            "retry_key": _retry_key(upload_id, file_hash, registration, registry_hash,
                                     proposals_hash, predecessor, provenance)}


def _verify_ledger(connection):
    # A corrupt indexed control column must not hide any prior scope or revision.
    for stored in connection.execute("SELECT " + ",".join(CONTROL_COLUMNS) + " FROM batches"):
        _verify_control(stored)


def _store_one(root, connection, prepared):
    """Write under the caller's transaction; common revision rules for all formats."""
    upload_id, data, registry_bytes = (prepared[key] for key in ("upload_id", "data", "registry_bytes"))
    resolved, preview, proposals = (prepared[key] for key in ("resolved", "preview", "proposals"))
    supersedes_batch_id, provenance = (prepared[key] for key in ("supersedes_batch_id", "provenance"))
    errors = list(prepared["errors"])
    registration, registry_hash, proposals_hash, file_hash, retry_key = (
        prepared[key] for key in ("registration", "registry_hash", "proposals_hash", "file_hash", "retry_key"))
    serialized_proposals = _json(proposals)
    identity = resolved["identity_evidence"] if resolved is not None else None
    metadata = None
    upload_payload_key = _hash(_json(registration).encode()) if registration is not None else None
    scope_key = _scope(registration) if registration is not None else None
    existing = connection.execute("SELECT * FROM batches WHERE retry_key=?", (retry_key,)).fetchone()
    if existing is not None:
        result = _decode(existing)
        return {**result, "idempotent": True}
    for prior in connection.execute("SELECT * FROM batches WHERE upload_id=?", (upload_id,)):
        _decode(prior)
        if prior["upload_payload_key"] is not None and (prior["file_sha256"] != file_hash or (
                upload_payload_key is not None and prior["upload_payload_key"] != upload_payload_key)):
            errors.append("upload_id was already used with different source bytes or reviewed registration.")
            break

    batch_id = "batch_" + uuid.uuid4().hex
    received_at = datetime.now(timezone.utc).isoformat()
    if resolved is not None:
        try:
            payload = {**resolved["metadata"], "batch_id": batch_id,
                       "received_at": received_at, "status": "quarantined" if errors else "validated"}
            metadata_object = build_batch_metadata(payload, load_dataset_contracts(root))
            metadata = dict(metadata_object.__dict__)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            errors.append("Batch metadata failed: " + str(exc))
    if supersedes_batch_id is not None:
        previous = connection.execute("SELECT * FROM batches WHERE batch_id=?", (supersedes_batch_id,)).fetchone()
        if previous is None:
            errors.append("The requested predecessor does not exist.")
        else:
            previous_result = _decode(previous)
            if previous["status"] != "validated" or previous["scope_key"] != scope_key:
                errors.append("A revision must reference a validated batch with the same binding, dataset, store, window and reviewed aggregation scope.")
            if connection.execute("SELECT 1 FROM batches WHERE supersedes_batch_id=? AND status='validated'", (supersedes_batch_id,)).fetchone():
                errors.append("The predecessor already has a validated successor; select the intended revision explicitly.")
            if metadata is not None and previous_result.get("metadata") is not None:
                current_time = datetime.fromisoformat(metadata["extracted_at"].replace("Z", "+00:00"))
                previous_time = datetime.fromisoformat(previous_result["metadata"]["extracted_at"].replace("Z", "+00:00"))
                if current_time < previous_time:
                    errors.append("A revision cannot move the extraction timestamp backwards.")
    elif scope_key is not None and connection.execute(
            "SELECT 1 FROM batches WHERE scope_key=? AND status='validated'", (scope_key,)).fetchone():
        errors.append("This store/window already has validated intake; an explicit predecessor is required.")

    if provenance != _provenance(root):
        errors.append("The dictionary, dataset registration or validation code changed before intake commit.")
    status = "quarantined" if errors else "validated"
    if metadata is not None:
        metadata["status"] = status
    result = {"batch_id": batch_id, "upload_id": upload_id, "received_at": received_at,
              "status": status, "file_sha256": file_hash, "metadata": metadata,
              "supersedes_batch_id": supersedes_batch_id, "registry_sha256": registry_hash,
              "registration": registration, "proposals_sha256": proposals_hash,
              "proposals": json.loads(serialized_proposals),
              "provenance": provenance, "preview": preview, "errors": errors}
    serialized = _json(result)
    connection.execute("INSERT INTO batches VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
        batch_id, upload_id, status, retry_key, file_hash, data, registry_bytes, registry_hash,
        identity, _hash(identity) if identity is not None else None, scope_key, upload_payload_key,
        supersedes_batch_id, serialized, _hash(serialized.encode()),
    ))
    return {**json.loads(serialized), "idempotent": False}


def _registry_bytes(path):
    try:
        return intake_registry._regular_file(path).read_bytes()
    except (ValueError, OSError):
        return None


def receive_batch(root: Path, database: Path, registry_path: Path, upload_id: str, data: bytes, *,
                  supersedes_batch_id: str | None = None, proposals=None) -> dict:
    """Archive one CSV receipt; manual text requires atomic document intake."""
    root, database, registry_path = Path(root), Path(database), Path(registry_path).absolute()
    if not isinstance(data, bytes) or not isinstance(upload_id, str) or not upload_id.strip():
        raise ValueError("Intake requires source bytes and a non-empty upload_id.")
    if supersedes_batch_id is not None and (not isinstance(supersedes_batch_id, str) or not supersedes_batch_id):
        raise ValueError("supersedes_batch_id must be a non-empty batch ID or null.")
    _reject_excluded_columns(data)
    _reject_excluded_proposals(proposals)
    if data.decode("utf-8-sig").lstrip().startswith("店铺"):
        from .text_document import reject_excluded_text
        reject_excluded_text(data)
        raise intake_registry.DocumentIntakeRequired("manual text requires receive-document and whole-document validation")
    provenance = _provenance(root)
    registry_bytes = _registry_bytes(registry_path)
    resolved, checked, errors = None, None, []
    try:
        resolved = intake_registry.resolve_upload(root, registry_path, upload_id, data)
        registry_bytes = resolved["registry_bytes"]
        checked = preview_module.preview_csv(root, data, resolved["context"], proposals,
                                              mapping_version=resolved["metadata"]["mapping_version"])
        if checked["status"] != "validated":
            errors.append("Source rows or proposals require review; the complete batch is quarantined.")
    except intake_registry.DocumentIntakeRequired:
        raise
    except (ValueError, OSError, KeyError, TypeError) as exc:
        errors.append("Registration or validation failed: " + str(exc))
    if provenance != _provenance(root):
        errors.append("The dictionary, dataset registration or validation code changed during intake.")
    prepared = _prepare(upload_id, data, registry_bytes, resolved, checked, proposals,
                        supersedes_batch_id, provenance, errors)
    connection = _open(database)
    try:
        _verify_ledger(connection)
        result = _store_one(root, connection, prepared)
        connection.commit()
        return result
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _document_groups(root, registry_path, document_id, data, proposals):
    from .text_document import parse_document
    document = intake_registry.resolve_document(root, registry_path, document_id, data)
    parsed = parse_document(root, data, document["mapping_version"], proposals)
    if parsed["status"] != "validated" or parsed["errors"]:
        raise ValueError("The complete source document requires review: " + "; ".join(parsed["errors"]))
    groups = {}
    for group in parsed["groups"]:
        selector = (group["source_block_line"], group["dataset_id"])
        if selector in groups:
            raise ValueError("Source document repeats a registered group.")
        groups[selector] = group
    expected = {(value["receipt"]["source_block_line"], value["receipt"]["dataset_id"])
                for value in document["resolved_uploads"]}
    if not expected or expected != set(groups):
        raise ValueError("Reviewed document receipts must cover every parsed source group exactly once.")
    prepared = []
    for resolved in document["resolved_uploads"]:
        receipt = resolved["receipt"]
        group = groups[(receipt["source_block_line"], receipt["dataset_id"])]
        context = asdict(resolved["context"])
        if group["context"] != context or group["preview"]["context"] != context:
            raise ValueError("Source store, window or dataset conflicts with independently reviewed receipt.")
        if group["preview"]["status"] != "validated":
            raise ValueError("Every document group must pass its registered field checks.")
        prepared.append({"resolved": resolved, "preview": group["preview"]})
    return document, prepared


def replay_document_group(root, registry_path, upload_id, data, proposals=None):
    """Recheck the whole original document before returning one registered group."""
    from .text_document import reject_excluded_text
    reject_excluded_text(data)
    registry = json.loads(intake_registry._regular_file(registry_path).read_bytes(),
                          object_pairs_hook=intake_registry._unique)
    receipts = [receipt for receipt in registry.get("uploads", [])
                if isinstance(receipt, dict) and receipt.get("upload_id") == upload_id]
    if len(receipts) != 1 or "document_id" not in receipts[0]:
        raise ValueError("The selected upload is not a unique registered document group.")
    _, groups = _document_groups(Path(root), Path(registry_path), receipts[0]["document_id"], data, proposals)
    return next(group for group in groups if group["resolved"]["receipt"]["upload_id"] == upload_id)


def _document_result(document_id, batches, *, errors=None, quarantine=None, idempotent=False):
    return {"document_id": document_id, "status": "quarantined" if errors else "validated",
            "errors": list(errors or []), "batches": batches,
            "quarantine_batch_id": quarantine, "idempotent": idempotent}


def receive_document(root: Path, database: Path, registry_path: Path, document_id: str, data: bytes, *,
                     supersedes=None, proposals=None) -> dict:
    """Archive all reviewed source groups atomically, or retain only quarantine."""
    from .text_document import reject_excluded_text
    root, database, registry_path = Path(root), Path(database), Path(registry_path).absolute()
    intake_registry._text(document_id, "document_id")
    if not isinstance(data, bytes):
        raise ValueError("Document intake requires original source bytes.")
    reject_excluded_text(data)
    _reject_excluded_proposals(proposals)
    supersedes = {} if supersedes is None else supersedes
    if (not isinstance(supersedes, dict) or
            any(not isinstance(key, str) or not key or not isinstance(value, str) or not value
                for key, value in supersedes.items())):
        raise ValueError("supersedes must map reviewed upload IDs to explicit predecessor batch IDs.")
    provenance = _provenance(root)
    registry_bytes = _registry_bytes(registry_path)
    errors, prepared = [], []
    try:
        document, groups = _document_groups(root, registry_path, document_id, data, proposals)
        registry_bytes = document["registry_bytes"]
        upload_ids = {item["resolved"]["receipt"]["upload_id"] for item in groups}
        if not set(supersedes) <= upload_ids:
            raise ValueError("supersedes includes an upload outside this reviewed document.")
        for item in groups:
            resolved = item["resolved"]
            upload_id = resolved["receipt"]["upload_id"]
            prepared.append(_prepare(upload_id, data, registry_bytes, resolved, item["preview"], proposals,
                                     supersedes.get(upload_id), provenance, []))
    except (ValueError, OSError, KeyError, TypeError) as exc:
        errors.append("Document registration or validation failed: " + str(exc))
    if provenance != _provenance(root):
        errors.append("Processing rules changed during document intake.")
    connection = _open(database)
    try:
        _verify_ledger(connection)
        prior_children = []
        quarantine_upload_id = "document:" + document_id
        for row in connection.execute("SELECT * FROM batches"):
            result = _decode(row)
            registration = result.get("registration")
            if registration is not None and registration["receipt"].get("document_id") == document_id:
                if result["status"] == "validated":
                    prior_children.append((row, result))
            if (result["upload_id"] == quarantine_upload_id and registration is None and
                    result["file_sha256"] != _hash(data)):
                errors.append("document_id already identifies different source bytes; register a new document ID.")
        if prior_children:
            expected_retries = {item["retry_key"] for item in prepared}
            if (errors or len(prior_children) != len(prepared) or
                    {row["retry_key"] for row, _ in prior_children} != expected_retries):
                errors.append("document_id already has a different complete reviewed intake; register a new document ID.")
            else:
                batches = [{**result, "idempotent": True} for _, result in prior_children]
                batches.sort(key=lambda item: item["upload_id"])
                connection.commit()
                return _document_result(document_id, batches, idempotent=True)
        batches = []
        if not errors:
            connection.execute("SAVEPOINT complete_document")
            for item in prepared:
                result = _store_one(root, connection, item)
                batches.append(result)
                if result["status"] != "validated":
                    errors.extend(result["errors"])
            if provenance != _provenance(root):
                errors.append("Processing rules changed before complete document commit.")
            if errors:
                connection.execute("ROLLBACK TO complete_document")
            connection.execute("RELEASE complete_document")
        if errors:
            # Keep the exact rejected input once; no valid child escapes a
            # document with an unknown line, missing receipt or revision conflict.
            held = _prepare(quarantine_upload_id, data, registry_bytes, None, None,
                            {"document_proposals": proposals, "supersedes": supersedes},
                            None, provenance, errors)
            archived = _store_one(root, connection, held)
            connection.commit()
            return _document_result(document_id, [], errors=archived["errors"],
                                    quarantine=archived["batch_id"], idempotent=archived["idempotent"])
        connection.commit()
        return _document_result(document_id, batches)
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()

def _read_row(database: Path, batch_id: str):
    database = Path(database).resolve()
    if not database.is_file():
        raise ValueError("The intake database does not exist.")
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        if (connection.execute("PRAGMA application_id").fetchone()[0],
                connection.execute("PRAGMA user_version").fetchone()[0]) != (APPLICATION_ID, SCHEMA_VERSION):
            raise ValueError("The database is not a supported retail intake database.")
        return connection.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
    finally:
        connection.close()


def read_batch(database: Path, batch_id: str) -> dict:
    """Read the stored result after checking all archived payload hashes."""
    return _decode(_read_row(database, batch_id))


def read_batch_artifacts(database: Path, batch_id: str) -> dict:
    """Return verified archived bytes for an explicit replay or source review."""
    row = _read_row(database, batch_id)
    _decode(row)
    return {"data": bytes(row["raw_data"]),
            "registry_bytes": bytes(row["registry_bytes"]) if row["registry_bytes"] is not None else None,
            "identity_evidence": bytes(row["identity_evidence"]) if row["identity_evidence"] is not None else None}
