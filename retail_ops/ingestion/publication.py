"""Publish explicit reviewed batch selections and pin verified evidence per run."""
from __future__ import annotations

from contextlib import contextmanager
import base64
import binascii
from dataclasses import asdict, dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile

from . import batch_store, intake_registry, publication_recipe, source_view
from .contracts import build_batch_metadata, load_dataset_contracts
from .preview import preview_csv
from .text_document import TEXT_MAPPINGS


VERSION = "1"
BATCH_PATTERN = r"batch_[0-9a-f]{32}"
PUBLICATION_PATTERN = r"publication_[0-9a-f]{64}"
ARCHIVE_NAMES = {"result.json", "source.csv", "registry.json", "identity.json"}
MANIFEST_KEYS = {"publication_version", "publication_id", "batch_ids", "recipe_sha256", "summary", "files"}
RESULT_KEYS = {"batch_id", "upload_id", "received_at", "status", "file_sha256", "metadata",
               "supersedes_batch_id", "registry_sha256", "registration", "proposals_sha256",
               "proposals", "provenance", "preview", "errors"}


def _bytes(value):
    return batch_store._json(value).encode("utf-8")


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _json(data):
    return json.loads(data, object_pairs_hook=intake_registry._unique)


def _source_name(result):
    version = result["metadata"]["mapping_version"]
    if not isinstance(version, str):
        raise ValueError("Publication source format must be registered text.")
    if version in TEXT_MAPPINGS:
        return "source.txt"
    if version in {"canonical_csv_v1", "canonical_csv_v2"}:
        return "source.csv"
    raise ValueError("Publication source format is not registered.")


def _document_identities(base, item, registry):
    """Restore exact independently checked files, never synthesize identities."""
    bundle = item["result"]["registration"].get("document_identity_evidence")
    paths = {binding["identity_evidence_path"] for binding in registry["bindings"]}
    if not isinstance(bundle, dict) or set(bundle) != paths:
        raise ValueError("Document identity archive must cover every registered binding exactly.")
    decoded = {}
    try:
        for name, value in bundle.items():
            relative = _relative(name)
            if not isinstance(value, str):
                raise ValueError("Document identity bytes require base64 text.")
            data = base64.b64decode(value, validate=True)
            if base64.b64encode(data).decode("ascii") != value:
                raise ValueError("Document identity bytes require canonical base64 text.")
            decoded[name] = data
            _write(base, relative.as_posix(), data)
    except (binascii.Error, UnicodeError) as exc:
        raise ValueError("Document identity archive contains invalid encoded bytes.") from exc
    for binding in registry["bindings"]:
        if _sha(decoded[binding["identity_evidence_path"]]) != binding["identity_evidence_sha256"]:
            raise ValueError("Document identity archive disagrees with its reviewed binding.")
    own = item["result"]["registration"]["binding"]
    if decoded[own["identity_evidence_path"]] != item["identity_evidence"]:
        raise ValueError("Selected identity bytes differ from their document archive.")


def _relative(value):
    if not isinstance(value, str):
        raise ValueError("Publication paths must be relative text.")
    path = Path(value)
    if (not value or value == "." or "\\" in value or path.is_absolute() or path.as_posix() != value or
            any(part in {".", ".."} for part in path.parts)):
        raise ValueError("Publication paths must stay within their directory.")
    return path


def _no_symlinks(path):
    path = Path(path).absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("Publication storage must not use symlinks.")
    return path


def _directory(root, directory, *, create=False):
    directory = _no_symlinks(Path(directory).expanduser())
    if root.resolve() == directory.resolve() or root.resolve() in directory.resolve().parents:
        raise ValueError("Keep publication storage outside the repository.")
    if create:
        directory.mkdir(parents=True, exist_ok=True)
    if not directory.is_dir():
        raise ValueError("Publication directory does not exist.")
    return directory


def _selection(database, batch_ids):
    if (not isinstance(batch_ids, list) or not batch_ids or
            any(not isinstance(item, str) or not re.fullmatch(BATCH_PATTERN, item) for item in batch_ids) or
            len(batch_ids) != len(set(batch_ids))):
        raise ValueError("Select a non-empty list of distinct explicit batch IDs.")
    database = Path(database).expanduser().resolve()
    if not database.is_file():
        raise ValueError("The intake database does not exist.")
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN")
        if (connection.execute("PRAGMA application_id").fetchone()[0],
                connection.execute("PRAGMA user_version").fetchone()[0]) != (
                    batch_store.APPLICATION_ID, batch_store.SCHEMA_VERSION):
            raise ValueError("The database is not a supported retail intake database.")
        selected = []
        for batch_id in sorted(batch_ids):
            row = connection.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
            try:
                result = batch_store._decode(row)
            except (KeyError, TypeError, AttributeError) as exc:
                raise ValueError("Archived intake result is malformed.") from exc
            if not isinstance(result, dict) or set(result) != RESULT_KEYS:
                raise ValueError("Archived intake result fields are incomplete or unregistered.")
            if result["status"] != "validated" or result["errors"]:
                raise ValueError("Publication requires validated batches without pending errors.")
            if (not all(isinstance(result[key], dict) for key in ("registration", "metadata", "provenance", "preview"))
                    or row["registry_bytes"] is None or row["identity_evidence"] is None):
                raise ValueError("Validated intake requires complete archived registration and evidence.")
            selected.append({"result": result, "data": bytes(row["raw_data"]),
                             "registry_bytes": bytes(row["registry_bytes"]),
                             "identity_evidence": bytes(row["identity_evidence"])})
        return selected
    finally:
        connection.rollback()
        connection.close()


def _selected_registration(root, item):
    """Check registration structure before restoring archived identity evidence.

    CSV batches contain the selected identity. Text batches additionally retain
    every reviewed identity needed to replay the complete registered document.
    """
    registry = _json(item["registry_bytes"])
    intake_registry._object(registry, intake_registry.REGISTRY_KEYS, "archived registry")
    if registry["registry_version"] not in ("1", "2", "3") or not all(
            isinstance(registry[key], list) for key in ("bindings", "uploads")):
        raise ValueError("Archived intake registry structure is unsupported.")
    contracts = load_dataset_contracts(root)
    bindings, sources = {}, set()
    for binding in registry["bindings"]:
        intake_registry._object(binding, intake_registry.BINDING_KEYS, "archived binding")
        for key in intake_registry.BINDING_KEYS - {"dataset_ids"}:
            intake_registry._text(binding[key], key)
        datasets = binding["dataset_ids"]
        if (not isinstance(datasets, list) or not datasets or
                any(not isinstance(value, str) or value not in contracts for value in datasets) or
                len(datasets) != len(set(datasets))):
            raise ValueError("Archived binding datasets require review.")
        source = tuple(binding[key] for key in sorted(intake_registry.IDENTITY_KEYS))
        if binding["binding_id"] in bindings or source in sources:
            raise ValueError("Archived registry contains duplicate bindings or source identities.")
        intake_registry._sha(binding["identity_evidence_sha256"], "identity_evidence_sha256")
        _relative(binding["identity_evidence_path"])
        bindings[binding["binding_id"]] = binding
        sources.add(source)
    uploads = {}
    for receipt in registry["uploads"]:
        intake_registry.validate_upload_window(receipt, registry["registry_version"])
        binding = bindings.get(receipt["binding_id"])
        if (receipt["upload_id"] in uploads or binding is None or
                receipt["dataset_id"] not in binding["dataset_ids"]):
            raise ValueError("Archived upload registration is ambiguous or unsupported.")
        intake_registry._sha(receipt["file_sha256"], "file_sha256")
        intake_registry._aware_datetime("extracted_at", receipt["extracted_at"])
        uploads[receipt["upload_id"]] = receipt
    receipt = uploads.get(item["result"]["upload_id"])
    if receipt is None:
        raise ValueError("The archived registry does not contain the selected upload.")
    return bindings[receipt["binding_id"]], receipt


def _replay(root, item, provenance):
    result = item["result"]
    if result["provenance"] != provenance or any(value is None for value in provenance.values()):
        raise ValueError("Intake processing rules changed; explicitly review the batch before publication.")
    binding, receipt = _selected_registration(root, item)
    text_source = receipt["mapping_version"] in TEXT_MAPPINGS
    with tempfile.TemporaryDirectory(prefix="retail-registration-replay-") as temp:
        base = Path(temp)
        archived_registry = _json(item["registry_bytes"])
        if text_source:
            _document_identities(base, item, archived_registry)
        else:
            identity = base / _relative(binding["identity_evidence_path"])
            identity.parent.mkdir(parents=True, exist_ok=True)
            identity.write_bytes(item["identity_evidence"])
        # Text requires all original receipts; CSV retains its selected entry.
        # Both restore reviewed identity bytes at their registered paths.
        registry = base / "publication-selected-registry.json"
        while registry.exists():
            registry = registry.with_name("selected-" + registry.name)
        registry.write_bytes(item["registry_bytes"] if text_source else _bytes({
            "registry_version": archived_registry["registry_version"],
            "bindings": [binding], "uploads": [receipt]}))
        if text_source:
            replayed = batch_store.replay_document_group(
                root, registry, result["upload_id"], item["data"], result["proposals"])
            resolved, checked = replayed["resolved"], replayed["preview"]
        else:
            resolved = intake_registry.resolve_upload(root, registry, result["upload_id"], item["data"])
            checked = preview_csv(root, item["data"], resolved["context"], result["proposals"],
                                  mapping_version=resolved["metadata"]["mapping_version"])
    registration = {"receipt": resolved["receipt"], "binding": resolved["binding"],
                    "context": asdict(resolved["context"]),
                    "identity_evidence_sha256": _sha(item["identity_evidence"])}
    if text_source:
        registration["document_identity_evidence"] = resolved["document_identity_evidence"]
    if registration != result["registration"]:
        raise ValueError("Archived registration conflicts with independently replayed source scope.")
    metadata = asdict(build_batch_metadata({**resolved["metadata"], "batch_id": result["batch_id"],
                     "received_at": result["received_at"], "status": "validated"}, load_dataset_contracts(root)))
    if metadata != result["metadata"]:
        raise ValueError("Archived batch metadata conflicts with the reviewed upload.")
    if checked["status"] != "validated" or _bytes(checked) != _bytes(result["preview"]):
        raise ValueError("Archived preview differs from independent source replay.")


def _check_overlaps(root, selected):
    contracts = load_dataset_contracts(root)
    seen = set()
    for item in selected:
        context = item["result"]["registration"]["context"]
        contract = contracts[context["dataset_id"]]
        partition = (contract.overlap_group, contract.grain, contract.ranking_basis,
                     context["store_id"], context["period_start"], context["period_end"])
        if partition in seen:
            raise ValueError("Overlapping batch selections require one explicit complete source version per store/window/ranking basis.")
        seen.add(partition)


def _write(base, relative, data):
    target = base / _relative(relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def _files(base):
    files = {}
    for path in sorted(base.rglob("*")):
        _no_symlinks(path)
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError("Publication contains a non-regular file.")
        files[path.relative_to(base).as_posix()] = path.read_bytes()
    return files


def _recipe(root):
    files = publication_recipe.recipe_files(root)
    if not isinstance(files, dict) or not files:
        raise ValueError("Publication recipe requires explicitly registered static files.")
    for name, data in files.items():
        if _relative(name).suffix in {".py", ".pyc"} or not isinstance(data, bytes):
            raise ValueError("Publication recipes must not copy executable Python into evidence roots.")
    return files


def _digest_map(files):
    return {name: _sha(data) for name, data in sorted(files.items())}


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_tree(base):
    """Make all staged files and directory entries durable before exposure."""
    directories = [base]
    for path in sorted(base.rglob("*")):
        _no_symlinks(path)
        if path.is_dir():
            directories.append(path)
            continue
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    for path in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        _sync_directory(path)


def _read_publication(root, directory, publication_id):
    if not isinstance(publication_id, str) or not re.fullmatch(PUBLICATION_PATTERN, publication_id):
        raise ValueError("An explicit publication ID is required.")
    target = _no_symlinks(directory / publication_id)
    if not target.is_dir():
        raise ValueError("Unknown publication ID.")
    contents = _files(target)
    manifest = _json(contents.pop("manifest.json", b"null"))
    if not isinstance(manifest, dict) or set(manifest) != MANIFEST_KEYS:
        raise ValueError("Publication manifest fields are incomplete or unregistered.")
    if manifest["publication_version"] != VERSION or manifest["publication_id"] != publication_id:
        raise ValueError("Publication version or identity does not match.")
    core = {key: value for key, value in manifest.items() if key != "publication_id"}
    if "publication_" + _sha(_bytes(core)) != publication_id:
        raise ValueError("Publication manifest does not match its content identity.")
    ids = manifest["batch_ids"]
    if (not isinstance(ids, list) or not ids or
            any(not isinstance(item, str) or not re.fullmatch(BATCH_PATTERN, item) for item in ids) or
            ids != sorted(set(ids))):
        raise ValueError("Publication batch selection is invalid.")
    archives = set()
    for batch_id in ids:
        result = _json(contents.get(f"archive/{batch_id}/result.json", b"null"))
        if not isinstance(result, dict) or set(result) != RESULT_KEYS or result["batch_id"] != batch_id:
            raise ValueError("Publication batch result fields are incomplete or inconsistent.")
        try:
            source_name = _source_name(result)
        except (KeyError, TypeError) as exc:
            raise ValueError("Publication batch source format is missing.") from exc
        archives.update(f"archive/{batch_id}/{name}" for name in
                        (ARCHIVE_NAMES - {"source.csv"}) | {source_name})
    if not archives <= set(contents):
        raise ValueError("Publication batch archives are incomplete.")
    for name in contents:
        path = _relative(name)
        if name not in archives and (path.parts[0] != "evidence" or len(path.parts) < 2 or
                                     path.suffix in {".py", ".pyc"}):
            raise ValueError("Publication contains an unregistered artifact path.")
    if _digest_map(contents) != manifest["files"]:
        raise ValueError("Publication artifact bytes differ from the manifest SHA-256 values.")
    recipe = _recipe(root)
    if _digest_map(recipe) != manifest["recipe_sha256"]:
        raise ValueError("Publication analysis rules or runtime differ from the current registered recipe.")
    if any(contents.get("evidence/" + name) != data for name, data in recipe.items()):
        raise ValueError("Publication static evidence differs from its registered recipe.")
    return manifest, contents


def publish(root: Path, database: Path, directory: Path, batch_ids: list[str], *, source_records: bool = False) -> dict:
    """Atomically publish one explicit selection; existing versions are immutable."""
    root = Path(root).resolve()
    if not isinstance(source_records, bool):
        raise ValueError("source_records must be an explicit boolean.")
    selected = _selection(database, batch_ids)
    provenance = batch_store._provenance(root)
    recipe = _recipe(root)
    for item in selected:
        _replay(root, item, provenance)
        if not source_records:
            context = item["result"]["registration"]["context"]
            try:
                intake_registry._month(context["period_start"], context["period_end"])
            except ValueError as exc:
                raise ValueError("Monthly analysis requires complete month source records; "
                                 "use --source-records publication for actual-date queries.") from exc
    _check_overlaps(root, selected)
    directory = _directory(root, directory, create=True)
    staging = Path(tempfile.mkdtemp(prefix="publication-staging-", dir=directory))
    try:
        for name, data in recipe.items():
            _write(staging / "evidence", name, data)
        for item in selected:
            archive = staging / "archive" / item["result"]["batch_id"]
            for name, data in {"result.json": _bytes(item["result"]), _source_name(item["result"]): item["data"],
                               "registry.json": item["registry_bytes"],
                               "identity.json": item["identity_evidence"]}.items():
                _write(archive, name, data)
        summary = (source_view.build_source_view(staging / "evidence", selected) if source_records
                   else publication_recipe.build_evidence_view(staging / "evidence", selected))
        if provenance != batch_store._provenance(root) or recipe != _recipe(root):
            raise ValueError("Processing rules changed during publication; no version was published.")
        core = {"publication_version": VERSION, "batch_ids": sorted(batch_ids),
                "recipe_sha256": _digest_map(recipe), "summary": summary,
                "files": _digest_map(_files(staging))}
        publication_id = "publication_" + _sha(_bytes(core))
        manifest = {**core, "publication_id": publication_id}
        _write(staging, "manifest.json", _bytes(manifest))
        _sync_tree(staging)
        # Serialize only the final identity check and directory rename. A complete
        # sibling directory becomes visible at once; no prior directory is replaced.
        lock_path = _no_symlinks(directory / "publication.lock")
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            target = directory / publication_id
            if target.exists() or target.is_symlink():
                existing, _ = _read_publication(root, directory, publication_id)
                if existing != manifest:
                    raise ValueError("The existing publication conflicts with the selected content.")
                _sync_directory(directory)
                return existing
            os.rename(staging, target)
            try:
                _sync_directory(directory)
            except OSError as exc:
                raise OSError("Publication is visible but its parent directory could not be synced; "
                              "verify and retry the same explicit selection: " + publication_id) from exc
        return manifest
    finally:
        if staging.exists():
            shutil.rmtree(staging)


@dataclass(frozen=True)
class PinnedPublication:
    publication_id: str
    root: Path
    manifest: dict


@contextmanager
def open_publication(root: Path, directory: Path, publication_id: str):
    """Copy verified bytes into a private evidence root for one analysis run."""
    root = Path(root).resolve()
    directory = _directory(root, directory)
    manifest, contents = _read_publication(root, directory, publication_id)
    with tempfile.TemporaryDirectory(prefix="retail-pinned-publication-") as temp:
        evidence = Path(temp)
        for name, data in contents.items():
            if name.startswith("evidence/"):
                _write(evidence, name.removeprefix("evidence/"), data)
        if _digest_map(_recipe(root)) != manifest["recipe_sha256"]:
            raise ValueError("Processing rules changed while pinning the publication.")
        yield PinnedPublication(publication_id, evidence, manifest)
