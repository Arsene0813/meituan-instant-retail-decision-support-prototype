"""Local operator actions over the existing intake and publication contracts."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import tempfile

from . import batch_store, intake_registry, publication, preview
from .contracts import load_dataset_contracts


class StaleRegistry(ValueError):
    pass


def external_path(root, value):
    if value is None or not str(value).strip():
        raise ValueError("服务端尚未配置此位置。")
    path = Path(value).expanduser().absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("运行位置不能使用符号链接。")
    if Path(root).resolve() == path.resolve() or Path(root).resolve() in path.resolve().parents:
        raise ValueError("运行数据必须放在项目目录之外。")
    return path


def dataset_catalog(root):
    """Labels describe existing canonical fields; they never create mappings."""
    root = Path(root)
    dictionary = (root / "retail_ops/data/DATA_DICTIONARY.md").read_text()
    labels = dict(re.findall(r"^### `([a-z0-9_]+)` / ([^\n]+)", dictionary, re.M))
    profile = json.loads((root / "retail_ops/contracts/manual_text.v3.json").read_bytes())
    for label, rule in profile["fields"].items():
        labels.setdefault(rule["field"], label)
    contracts = load_dataset_contracts(root)
    return [{"dataset_id": name, "source_name": contracts[name].source_name,
             "grain": contracts[name].grain,
             "fields": [{"name": field, "label": labels.get(field, field)} for field in
                        sorted(preview.SCHEMAS[name] - preview.KEYS - set(contracts[name].key_fields))]}
            for name in sorted(preview.SCHEMAS)]


def registry_catalog(root, registry_path):
    _, data, _, bindings, _, uploads = intake_registry._load_registry(root, registry_path)
    selections = {}
    for receipt in uploads.values():
        kind = "document" if "document_id" in receipt else "csv"
        identity = receipt.get("document_id", receipt["upload_id"])
        binding = bindings[receipt["binding_id"]]
        selection = selections.setdefault((kind, identity), {"kind": kind, "selection_id": identity,
                                                              "receipts": []})
        selection["receipts"].append({**receipt, "store_id": binding["store_id"],
            "source_account_id": binding["source_account_id"], "source_store_id": binding["source_store_id"]})
    return {"registry_sha256": hashlib.sha256(data).hexdigest(),
            "selections": [selections[key] for key in sorted(selections)]}


@contextmanager
def _registry_snapshot(root, path, expected_sha256):
    _, data, _, bindings, identities, uploads = intake_registry._load_registry(root, path)
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise StaleRegistry("登记已变化，请刷新并重新核对后上传。")
    with tempfile.TemporaryDirectory(prefix="retail-console-registry-") as temporary:
        base = Path(temporary)
        copied = {}
        for key, binding in bindings.items():
            name = binding["identity_evidence_path"]
            evidence = identities[key]
            if name in copied and copied[name] != evidence:
                raise ValueError("身份文件路径对应互相冲突的内容。")
            copied[name] = evidence
            target = base / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(evidence)
        snapshot = base / "console-registry.json"
        while snapshot.exists():
            snapshot = snapshot.with_name("snapshot-" + snapshot.name)
        snapshot.write_bytes(data)
        yield snapshot, uploads


def receive(root, database, registry_path, kind, selection_id, expected_sha256, data, supersedes):
    """Freeze independently checked registration bytes before calling intake."""
    with _registry_snapshot(root, registry_path, expected_sha256) as (snapshot, uploads):
        known = ({receipt.get("document_id") for receipt in uploads.values()} if kind == "document"
                 else {key for key, receipt in uploads.items() if "document_id" not in receipt})
        if selection_id not in known:
            raise ValueError("请选择当前独立登记中的上传来源。")
        if kind == "document":
            from .document_intake import receive_document
            return receive_document(root, database, snapshot, selection_id, data,
                                    supersedes=supersedes)
        if kind != "csv" or set(supersedes) - {selection_id}:
            raise ValueError("修订关系必须对应所选上传标识。")
        result = batch_store.receive_batch(root, database, snapshot, selection_id, data,
                                           supersedes_batch_id=supersedes.get(selection_id))
        return {"status": result["status"], "errors": result["errors"],
                "batches": [result], "quarantine_batch_id": None, "idempotent": result["idempotent"]}


def batch_list(database, offset=0, limit=50):
    database = Path(database)
    if not database.exists():
        return {"items": [], "offset": offset, "next_offset": None}
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN")
        if (connection.execute("PRAGMA application_id").fetchone()[0],
                connection.execute("PRAGMA user_version").fetchone()[0]) != (
                    batch_store.APPLICATION_ID, batch_store.SCHEMA_VERSION):
            raise ValueError("批次库格式不匹配。")
        rows = connection.execute("SELECT * FROM batches ORDER BY rowid DESC LIMIT ? OFFSET ?",
                                  (limit + 1, offset)).fetchall()
        items = []
        for row in rows[:limit]:
            result = batch_store._decode(row)
            registration = result["registration"] or {}
            items.append({key: result[key] for key in
                ("batch_id", "upload_id", "status", "received_at", "supersedes_batch_id", "errors")})
            items[-1].update(context=registration.get("context"),
                              extracted_at=(result["metadata"] or {}).get("extracted_at"))
        return {"items": items, "offset": offset, "next_offset": offset + limit if len(rows) > limit else None}
    finally:
        connection.close()


def publication_list(root, directory, offset=0, limit=20):
    directory = Path(directory)
    if not directory.exists():
        return {"items": [], "offset": offset, "next_offset": None}
    publication._directory(root, directory, create=False)
    names = sorted((path.name for path in directory.iterdir()
                    if re.fullmatch(publication.PUBLICATION_PATTERN, path.name)))
    items = []
    for name in names[offset:offset + limit]:
        try:
            manifest, _ = publication._read_publication(root, directory, name)
            queryable = manifest["summary"].get("profile") == "source_records_v1"
            items.append({"publication_id": name, "available": queryable,
                "batch_ids": manifest["batch_ids"], "summary": manifest["summary"],
                "message": None if queryable else "此版本为月度分析视图，请用来源记录版本查询。"})
        except (ValueError, OSError, KeyError, TypeError) as exc:
            items.append({"publication_id": name, "available": False, "message": str(exc)})
    return {"items": items, "offset": offset,
            "next_offset": offset + limit if len(names) > offset + limit else None}


def batch_detail(database, batch_id):
    """Keep identity-file byte bundles out of routine browser display."""
    result = batch_store.read_batch(database, batch_id)
    if result["registration"]:
        result["registration"].pop("document_identity_evidence", None)
    return result
