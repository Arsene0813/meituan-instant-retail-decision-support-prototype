"""Persist reviewed CSV intake and explicit revisions without publishing data."""
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
    files = {
        "batch_store": Path(__file__), "intake_registry": Path(intake_registry.__file__),
        "preview": Path(preview_module.__file__), "contracts": Path(contract_module.__file__),
        "dataset_registry": root / contract_module.DEFAULT_REGISTRY_PATH,
        "dictionary": root / "retail_ops/data/DATA_DICTIONARY.md",
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
    return _json({
        "binding_id": binding["binding_id"], "source_system": binding["source_system"],
        "source_account_id": binding["source_account_id"], "source_store_id": binding["source_store_id"],
        "dataset_id": context["dataset_id"], "store_id": context["store_id"],
        "period_start": context["period_start"], "period_end": context["period_end"],
        "grain": context["grain"], "ranking_basis": context["ranking_basis"],
    })


def _retry_key(upload_id, file_hash, registration, registry_hash, proposals_hash, predecessor, provenance):
    return _hash(_json({
        "upload_id": upload_id, "file_sha256": file_hash,
        "registration": registration if registration is not None else {"unresolved_registry_sha256": registry_hash},
        "proposals_sha256": proposals_hash, "supersedes_batch_id": predecessor,
        "provenance": provenance,
    }).encode())


def receive_batch(root: Path, database: Path, registry_path: Path, upload_id: str, data: bytes, *,
                  supersedes_batch_id: str | None = None, proposals=None) -> dict:
    """Archive a reviewed receipt or quarantine; never select analysis versions."""
    root, database, registry_path = Path(root), Path(database), Path(registry_path)
    registry_path = registry_path.absolute()
    if not isinstance(data, bytes) or not isinstance(upload_id, str) or not upload_id.strip():
        raise ValueError("Intake requires source bytes and a non-empty upload_id.")
    if supersedes_batch_id is not None and (not isinstance(supersedes_batch_id, str) or not supersedes_batch_id):
        raise ValueError("supersedes_batch_id must be a non-empty batch ID or null.")
    _reject_excluded_columns(data)  # Excluded values must not enter even the raw archive.
    _reject_excluded_proposals(proposals)
    serialized_proposals = _json(proposals)
    proposals_hash = _hash(serialized_proposals.encode())
    provenance = _provenance(root)
    registry_bytes = None
    try:
        registry_bytes = intake_registry._regular_file(registry_path).read_bytes()
    except (ValueError, OSError):
        pass
    resolved = None
    preview = None
    metadata = None
    identity = None
    errors = []
    try:
        resolved = intake_registry.resolve_upload(root, registry_path, upload_id, data)
        registry_bytes = resolved["registry_bytes"]
        identity = resolved["identity_evidence"]
        preview = preview_module.preview_csv(root, data, resolved["context"], proposals)
        if preview["status"] != "validated":
            errors.append("Source rows or proposals require review; the complete batch is quarantined.")
    except (ValueError, OSError, KeyError, TypeError) as exc:
        errors.append("Registration or validation failed: " + str(exc))
    if provenance != _provenance(root):
        errors.append("The dictionary, dataset registration or validation code changed during intake.")

    registry_hash = _hash(registry_bytes) if registry_bytes is not None else None
    registration = None if resolved is None else {
        "receipt": resolved["receipt"], "binding": resolved["binding"],
        "context": asdict(resolved["context"]),
        "identity_evidence_sha256": _hash(identity),
    }
    file_hash = _hash(data)
    retry_key = _retry_key(upload_id, file_hash, registration, registry_hash,
                           proposals_hash, supersedes_batch_id, provenance)
    upload_payload_key = _hash(_json(registration).encode()) if registration is not None else None
    scope_key = _scope(registration) if registration is not None else None
    connection = _open(database)
    try:
        # A corrupt scope/retry column could hide a row from the indexed
        # lookup below. Check control columns under the same write transaction;
        # this scan does not reread archived source or registration blobs.
        for stored in connection.execute("SELECT " + ",".join(CONTROL_COLUMNS) + " FROM batches"):
            _verify_control(stored)
        existing = connection.execute("SELECT * FROM batches WHERE retry_key=?", (retry_key,)).fetchone()
        if existing is not None:
            result = _decode(existing)
            connection.commit()
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
                    errors.append("A revision must reference a validated batch with the same binding, dataset, store and window.")
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
        connection.commit()
        return {**json.loads(serialized), "idempotent": False}
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
