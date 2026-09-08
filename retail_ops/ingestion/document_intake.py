"""Replay existing documents before treating unrelated registration growth as a revision."""
from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
import sqlite3

from . import batch_store, intake_registry, publication


def _prior(database, document_id, data):
    if not database.exists():
        return []
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN")
        if (connection.execute("PRAGMA application_id").fetchone()[0],
                connection.execute("PRAGMA user_version").fetchone()[0]) != (
                    batch_store.APPLICATION_ID, batch_store.SCHEMA_VERSION):
            raise ValueError("The database is not a supported retail intake database")
        batch_store._verify_ledger(connection)
        selected = []
        for row in connection.execute("SELECT * FROM batches"):
            result = batch_store._decode(row)
            registration = result.get("registration")
            if (registration is None and result["upload_id"] == "document:" + document_id and
                    result["file_sha256"] != batch_store._hash(data)):
                return []
            if (result["status"] == "validated" and registration is not None and
                    registration["receipt"].get("document_id") == document_id):
                if (set(result) != publication.RESULT_KEYS or result["errors"] or
                        not all(isinstance(result[key],dict) for key in
                                ("registration","metadata","provenance","preview")) or
                        row["registry_bytes"] is None or row["identity_evidence"] is None):
                    raise ValueError("Existing validated document has incomplete archived evidence")
                selected.append({"result":result, "data":bytes(row["raw_data"]),
                                 "registry_bytes":bytes(row["registry_bytes"]),
                                 "identity_evidence":bytes(row["identity_evidence"])})
        return selected
    finally:
        connection.close()


def _replayed_retry(root, database, registry_path, document_id, data, supersedes, proposals):
    selected = _prior(database, document_id, data)
    if not selected:
        return None
    try:
        _, current = batch_store._document_groups(root, registry_path, document_id, data, proposals)
    except (ValueError, OSError, KeyError, TypeError):
        return None  # The original intake path records the concrete validation error.
    groups = {item["resolved"]["receipt"]["upload_id"]:item for item in current}
    if set(supersedes) - set(groups) or len(selected) != len(groups):
        return None
    provenance = batch_store._provenance(root)
    if {item["result"]["upload_id"] for item in selected} != set(groups):
        return None
    proposals_json = batch_store._json(proposals)
    for item in selected:
        prior = item["result"]
        current = groups[prior["upload_id"]]
        registration = batch_store._registration(current["resolved"])
        old = dict(prior["registration"])
        # Archive/replay still uses the COMPLETE identity bundle. Only the
        # selected-document comparison excludes identities for unrelated stores.
        old_bundle = old.pop("document_identity_evidence", None)
        current_bundle = registration.pop("document_identity_evidence", None)
        if not isinstance(old_bundle,dict) or not isinstance(current_bundle,dict):
            raise ValueError("Existing document identity evidence is incomplete")
        if any(current_bundle.get(name) != value for name,value in old_bundle.items()):
            return None  # Only identical existing entries plus unrelated additions qualify.
        if (item["data"] != data or old != registration or prior["provenance"] != provenance or
                prior["supersedes_batch_id"] != supersedes.get(prior["upload_id"]) or
                batch_store._json(prior["proposals"]) != proposals_json or
                batch_store._json(prior["preview"]) != batch_store._json(current["preview"])):
            return None
        publication._replay(root, item, provenance)
    if batch_store._provenance(root) != provenance:
        raise ValueError("Processing rules changed while replaying the existing document")
    batches = [{**item["result"], "idempotent":True} for item in selected]
    batches.sort(key=lambda item:item["upload_id"])
    return batch_store._document_result(document_id, batches, idempotent=True)


def receive_document(root, database, registry_path, document_id, data, *, supersedes=None, proposals=None):
    """Return verified original batch IDs for unchanged documents, otherwise receive normally."""
    from .operator_console import _registry_snapshot
    from .text_document import reject_excluded_text
    root, database, registry_path = Path(root), Path(database), Path(registry_path)
    intake_registry._text(document_id, "document_id")
    reject_excluded_text(data)
    batch_store._reject_excluded_proposals(proposals)
    supersedes = {} if supersedes is None else supersedes
    if (not isinstance(supersedes, dict) or any(not isinstance(key,str) or not key or
            not isinstance(value,str) or not value for key,value in supersedes.items())):
        raise ValueError("supersedes must map reviewed upload IDs to explicit predecessor batch IDs")
    with ExitStack() as stack:
        try:
            original = intake_registry._regular_file(registry_path).read_bytes()
            snapshot, _ = stack.enter_context(_registry_snapshot(root, registry_path, batch_store._hash(original)))
        except (ValueError, OSError, KeyError, TypeError):
            # Preserve the core's quarantine behavior for invalid registrations.
            return batch_store.receive_document(root,database,registry_path,document_id,data,
                                                supersedes=supersedes,proposals=proposals)
        retry = _replayed_retry(root,database,snapshot,document_id,data,supersedes,proposals)
        if retry is not None:
            return retry
        return batch_store.receive_document(root,database,snapshot,document_id,data,
                                            supersedes=supersedes,proposals=proposals)
