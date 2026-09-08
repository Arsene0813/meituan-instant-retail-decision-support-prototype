"""Check explicit operator additions before appending a trusted intake registry."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

from . import batch_store, intake_registry, operator_console, preview, text_document

PLAN_VERSION = "1"
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_PLAN_BYTES = 1024 * 1024
PLAN_BINDING_KEYS = intake_registry.BINDING_KEYS - {"identity_evidence_sha256"}


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def _read(path, limit=None):
    path = intake_registry._regular_file(Path(path).expanduser())
    with path.open("rb") as stream:
        data = stream.read() if limit is None else stream.read(limit + 1)
    if limit is not None and len(data) > limit:
        raise ValueError("File exceeds the registered preparation size limit: " + path.name)
    return path, data


def _decode(data):
    def invalid(value):
        raise ValueError("Non-finite JSON constants are not registered")
    return json.loads(data, object_pairs_hook=intake_registry._unique, parse_constant=invalid)


def _rules(root):
    return {**batch_store._provenance(root), "registration_workflow_sha256":
            _hash(Path(__file__).read_bytes())}


def inspect_text(root, input_path, mapping_version):
    """Return source observations only; never create independent receipt fields."""
    _, data = _read(input_path, MAX_SOURCE_BYTES)
    text_document.reject_excluded_text(data)
    parsed = text_document.parse_document(Path(root), data, mapping_version)
    groups = []
    for group in parsed["groups"]:
        records = group["preview"]["validated_records"]
        fields = sorted({field for row in records for item in row["source_locator"]["values"]
                         for field in item["fields"]})
        groups.append({"source_block_line": group["source_block_line"],
                       "observed_context": group["context"], "observed_fields": fields,
                       "parsed_record_count": len(records)})
    return {"inspection_version": "1", "status": "source_checked" if not parsed["errors"] else "needs_review",
            "file_sha256": _hash(data), "size_bytes": len(data), "mapping_version": mapping_version,
            "errors": parsed["errors"], "observations": groups,
            "independent_registration_created": False}


@contextmanager
def _snapshot(registry, identities):
    with tempfile.TemporaryDirectory(prefix="retail-registration-check-") as temporary:
        base = Path(temporary)
        for name, data in identities.items():
            target = base / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        path = base / "checked-registry.json"
        while path.exists():
            path = path.with_name("snapshot-" + path.name)
        path.write_bytes(_encoded(registry))
        yield path


def _prepare(root, registry_path, plan_path, input_path):
    root = Path(root).resolve()
    registry_path = operator_console.external_path(root, registry_path)
    registry, original, _, old_bindings, old_evidence, old_uploads = intake_registry._load_registry(root, registry_path)
    if registry["registry_version"] != "3":
        raise ValueError("This helper requires an existing version 3 registry; it does not upgrade older registries")
    plan_path, plan_bytes = _read(plan_path, MAX_PLAN_BYTES)
    input_path, data = _read(input_path, MAX_SOURCE_BYTES)
    if len({path.resolve() for path in (registry_path, plan_path, input_path)}) != 3:
        raise ValueError("Registry, review plan and source must be separate files")
    plan = _decode(plan_bytes)
    intake_registry._object(plan, {"plan_version", "bindings", "uploads"}, "registration plan")
    if plan["plan_version"] != PLAN_VERSION or not isinstance(plan["bindings"], list) or not isinstance(plan["uploads"], list) or not plan["uploads"]:
        raise ValueError("A version 1 plan requires binding additions and non-empty explicit upload receipts")
    if not data:
        raise ValueError("Source file is empty")
    watched = {registry_path: original, plan_path: plan_bytes, input_path: data}
    identities, additions, seen = {}, [], set(old_bindings)
    for key, binding in old_bindings.items():
        name, identity = binding["identity_evidence_path"], old_evidence[key]
        if name in identities and identities[name] != identity:
            raise ValueError("Identity path contains conflicting reviewed bytes")
        identities[name] = identity
        watched[registry_path.parent / name] = identity
    for item in plan["bindings"]:
        intake_registry._object(item, PLAN_BINDING_KEYS, "new binding")
        for key in PLAN_BINDING_KEYS - {"dataset_ids"}:
            intake_registry._text(item[key], key)
        if item["binding_id"] in seen:
            raise ValueError("Existing or repeated binding_id cannot be edited or expanded")
        seen.add(item["binding_id"])
        relative = Path(item["identity_evidence_path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Identity evidence must be an existing file inside the registry directory")
        path, identity = _read(registry_path.parent / relative)
        binding = {**item, "identity_evidence_sha256": _hash(identity)}
        # The independent file must already exist and agree with all explicit identity values.
        if intake_registry._identity(registry_path.parent, binding) != identity:
            raise ValueError("Identity evidence changed during registration check")
        name = item["identity_evidence_path"]
        if name in identities and identities[name] != identity:
            raise ValueError("Identity path contains conflicting reviewed bytes")
        identities[name], watched[path] = identity, identity
        additions.append(binding)
    receipts, upload_ids = [], set(old_uploads)
    old_documents = {row.get("document_id") for row in old_uploads.values() if "document_id" in row}
    for item in plan["uploads"]:
        if not isinstance(item, dict) or "file_sha256" in item:
            raise ValueError("Plan receipts use explicit registered metadata; file_sha256 is computed from the original")
        receipt = {**item, "file_sha256": _hash(data)}
        intake_registry.validate_upload_window(receipt, "3")
        if receipt["upload_id"] in upload_ids:
            raise ValueError("Existing or repeated upload_id cannot be edited")
        if receipt.get("document_id") in old_documents:
            raise ValueError("An existing document_id cannot be extended; use a new document for new or revised bytes")
        upload_ids.add(receipt["upload_id"])
        receipts.append(receipt)
    used_bindings = {row["binding_id"] for row in receipts}
    if {row["binding_id"] for row in additions} - used_bindings:
        raise ValueError("New bindings must be used by this file's explicit receipts")
    mappings = {row["mapping_version"] for row in receipts}
    if len(mappings) != 1:
        raise ValueError("One plan must declare exactly one format for its source file")
    mapping = next(iter(mappings))
    manual = mapping in intake_registry.TEXT_MAPPINGS
    if manual:
        text_document.reject_excluded_text(data)
        documents = {row["document_id"] for row in receipts}
        if len(documents) != 1:
            raise ValueError("One source file must have one explicitly supplied document_id")
        kind, selection = "document", next(iter(documents))
    else:
        batch_store._reject_excluded_columns(data)
        if len(receipts) != 1:
            raise ValueError("One CSV source file requires one explicit receipt")
        kind, selection = "csv", receipts[0]["upload_id"]
    candidate = {"registry_version": "3", "bindings": registry["bindings"] + additions,
                 "uploads": registry["uploads"] + receipts}
    rules = _rules(root)
    with _snapshot(candidate, identities) as snapshot:
        intake_registry._load_registry(root, snapshot)
        if manual:
            _, prepared = batch_store._document_groups(root, snapshot, selection, data, None)
            record_count = sum(len(group["preview"]["validated_records"]) for group in prepared)
        else:
            resolved = intake_registry.resolve_upload(root, snapshot, selection, data)
            checked = preview.preview_csv(root, data, resolved["context"], mapping_version=mapping)
            if checked["status"] != "validated":
                errors = checked["errors"] + [error for row in checked["quarantined_records"] for error in row["errors"]]
                raise ValueError("CSV source requires review: " + "; ".join(errors))
            record_count = len(checked["validated_records"])
    candidate_bytes = _encoded(candidate)
    review = {"check_version": "1", "registry_sha256": _hash(original), "plan_sha256": _hash(plan_bytes),
              "file_sha256": _hash(data), "identity_sha256": {name:_hash(value) for name,value in identities.items()},
              "candidate_sha256": _hash(candidate_bytes), "rules": rules}
    result = {"status": "checked", "check_sha256": _hash(_encoded(review)), **review,
              "kind": kind, "selection_id": selection, "new_bindings": additions,
              "new_uploads": receipts, "record_count": record_count, "batch_created": False}
    state = {"root":root, "registry_path":registry_path, "watched":watched, "rules":rules,
             "original":original, "candidate":candidate_bytes, "result":result}
    _unchanged(state)
    return state


def _unchanged(state):
    for path, data in state["watched"].items():
        if _read(path)[1] != data:
            raise ValueError("A checked input changed; run check again before appending")
    if _rules(state["root"]) != state["rules"]:
        raise ValueError("Processing rules changed; run check again")


def check(root, registry_path, plan_path, input_path):
    return _prepare(root, registry_path, plan_path, input_path)["result"]


@contextmanager
def _directory_lock(directory):
    # A directory inode stays stable across atomic replacements of its registry file.
    import fcntl
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another registration writer is active; retry after it finishes") from exc
        yield
    finally:
        os.close(descriptor)


def _temporary(directory, data, mode):
    descriptor, name = tempfile.mkstemp(prefix=".registration-", dir=directory)
    path = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        return path
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _backup(state, mode):
    path = state["registry_path"]
    backup = path.with_name(path.name + ".before-" + _hash(state["original"]) + ".json")
    if os.path.lexists(backup):
        if _read(backup)[1] != state["original"]:
            raise ValueError("The existing registration backup has different bytes")
        return backup
    temporary = _temporary(path.parent, state["original"], mode)
    try:
        os.link(temporary, backup)
    finally:
        temporary.unlink(missing_ok=True)
    return backup


def append(root, registry_path, plan_path, input_path, expected_check_sha256):
    """Append checked declarations atomically; never receive data or edit old records."""
    intake_registry._sha(expected_check_sha256, "expected_check_sha256")
    path = operator_console.external_path(root, registry_path)
    intake_registry._regular_file(path)
    with _directory_lock(path.parent):
        state = _prepare(root, path, plan_path, input_path)
        if state["result"]["check_sha256"] != expected_check_sha256:
            raise ValueError("Registration, plan, source, identity or rules differ from the checked version; run check again")
        mode = stat.S_IMODE(path.stat().st_mode)
        _unchanged(state)
        backup = _backup(state, mode)
        temporary = _temporary(path.parent, state["candidate"], mode)
        try:
            _unchanged(state)
            os.replace(temporary, path)
            try:
                descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            except OSError as exc:
                raise OSError("The complete registry was replaced, but directory sync failed; "
                              "inspect the live registry before retrying. Backup: " + str(backup)) from exc
        finally:
            temporary.unlink(missing_ok=True)
        return {**state["result"], "status":"registered", "registry_sha256":_hash(state["candidate"]),
                "previous_registry_sha256":_hash(state["original"]), "backup_path":str(backup)}
