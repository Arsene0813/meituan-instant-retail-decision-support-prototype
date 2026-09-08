"""Parse a complete registered text upload with original-line record locators."""
from __future__ import annotations

import csv
import hashlib
import io
import re
from pathlib import Path

from . import preview, text_preview


TEXT_MAPPINGS = frozenset({"manual_text_v2", "manual_text_v3"})
LOCATOR_VERSION = "manual_text_locator_v1"
_SPAN_KEYS = {"line", "column_start", "column_end", "text"}
_LOCATOR_KEYS = {"locator_version", "source_block_line", "store", "window", "values", "item_ordinal"}
_METADATA = {"store_id", "period_start", "period_end", "period_month"}
_SOURCE_FIELDS = set().union(*preview.SCHEMAS.values()) - _METADATA


def reject_excluded_text(data):
    """Check the entire original before any persistent batch or archive write."""
    if not isinstance(data, bytes):
        raise ValueError("Text intake requires original UTF-8 bytes; no bytes were archived.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError as exc:
        raise ValueError("Cannot verify excluded fields in invalid UTF-8; no bytes were archived.") from exc
    if any(label in text for label in preview.IGNORED):
        raise ValueError("Text contains excluded order fields; no bytes were archived.")


def _require(condition, message):
    if not condition:
        raise ValueError("Text source locator: " + message)


def _validate_span(span):
    _require(isinstance(span, dict) and set(span) == _SPAN_KEYS, "unregistered span fields")
    _require(all(type(span[key]) is int and span[key] >= 1 for key in (
        "line", "column_start", "column_end")), "span positions must be positive integers")
    text = span["text"]
    _require(isinstance(text, str) and text and text == text.strip()
             and not any(char in text for char in "\r\n\ufeff"), "span requires original single-line text")
    _require(span["column_end"] - span["column_start"] == len(text),
             "end-exclusive columns must match the source text length")


def validate_text_locator(locator, record, source_line_end):
    """Check structure and record scope; archived replay verifies actual values.

    Columns are one-based Unicode code-point positions, with an exclusive end.
    A projected record is allowed: an evidenced field need not be requested.
    """
    _require(isinstance(locator, dict) and set(locator) == _LOCATOR_KEYS,
             "unregistered locator fields")
    _require(locator["locator_version"] == LOCATOR_VERSION, "unregistered locator version")
    _require(type(locator["source_block_line"]) is int and locator["source_block_line"] >= 1,
             "source block line must be a positive integer")
    _require(isinstance(record, dict) and all(isinstance(field, str) for field in record),
             "canonical record must be an object")
    _validate_span(locator["store"])
    _validate_span(locator["window"])
    _require(locator["store"]["line"] == locator["source_block_line"]
             and locator["window"]["line"] > locator["store"]["line"], "store/window order disagrees")
    store = re.fullmatch(r"店铺\s*[:：]\s*(\S+)", locator["store"]["text"])
    _require(store is not None and store[1] == record.get("store_id"), "store header disagrees")
    window = locator["window"]["text"]
    _require(window.startswith("时间范围"), "window header is absent")
    raw_window = window[len("时间范围"):].lstrip(" :：")
    version = "manual_text_v3" if "至" in raw_window else "manual_text_v2"
    try:
        period = text_preview._window(raw_window, version)
    except (ValueError, OverflowError) as exc:
        raise ValueError("Text source locator: invalid source window") from exc
    _require(all(record.get(field) == value for field, value in period.items()),
             "window header disagrees with record")
    values = locator["values"]
    _require(isinstance(values, list) and values, "source metric evidence is required")
    seen, lines = set(), [locator["store"]["line"], locator["window"]["line"]]
    for evidence in values:
        _require(isinstance(evidence, dict) and set(evidence) == {"fields", "span"},
                 "unregistered metric evidence fields")
        fields = evidence["fields"]
        _require(isinstance(fields, list) and fields and all(isinstance(field, str) for field in fields),
                 "metric evidence requires canonical field names")
        _require(len(set(fields)) == len(fields) and set(fields) <= _SOURCE_FIELDS
                 and not seen.intersection(fields), "metric evidence fields are unknown or repeated")
        seen.update(fields)
        _validate_span(evidence["span"])
        _require(evidence["span"]["line"] > locator["window"]["line"],
                 "metric evidence must follow its source window")
        lines.append(evidence["span"]["line"])
    _require(type(source_line_end) is int and source_line_end == max(lines),
             "source_line_end disagrees with the original evidence")
    _require(all(field in _METADATA or value is None or field in seen for field, value in record.items()),
             "non-null record field has no source evidence")
    ranks = [field for field in ("sku_rank", "search_term_rank") if field in seen]
    ordinal = locator["item_ordinal"]
    if ranks:
        _require(len(ranks) == 1 and type(ordinal) is int and 1 <= ordinal <= 3
                 and len(values) == 1, "ranked record requires one item and an ordinal")
        _require(record.get(ranks[0]) in (None, ordinal), "source item ordinal disagrees with rank")
    else:
        _require(ordinal is None, "store metrics do not have a list ordinal")


def _span(lines, number, text, start=0):
    original = lines[number - 1]
    column = original.find(text, start)
    if column < 0:
        raise ValueError("Parsed text is absent from its original source line")
    return {"line": number, "column_start": column + 1,
            "column_end": column + len(text) + 1, "text": text}


def _locator(lines, block, entry, cursors):
    values = []
    rank = entry["record"].get("sku_rank", entry["record"].get("search_term_rank"))
    for evidence in entry["lineage"]:
        number = evidence["source_line"]
        # Ordered rank items may share both a line and identical literal text.
        start = cursors.get(number, 0) if rank is not None else 0
        if rank is not None and number not in cursors:
            header = re.match(r"\s*[^:：]+[:：]\s*", lines[number - 1])
            if header is None:
                raise ValueError("Ranked source line has no registered section boundary")
            start = header.end()
        span = _span(lines, number, evidence["source_text"], start)
        if rank is not None:
            cursors[number] = span["column_end"] - 1
        values.append({"fields": list(evidence["fields"]), "span": span})
    store, window = block["source_store"], block["source_window"]
    locator = {"locator_version": LOCATOR_VERSION, "source_block_line": block["source_line_start"],
               "store": _span(lines, store["source_line"], store["source_text"]),
               "window": _span(lines, window["source_line"], window["source_text"]),
               "values": values, "item_ordinal": rank}
    last = max([locator["store"]["line"], locator["window"]["line"],
                *(value["span"]["line"] for value in values)])
    validate_text_locator(locator, entry["record"], last)
    return last, locator


def _csv(rows):
    output = io.StringIO(newline="")
    fields = list(dict.fromkeys(field for row in rows for field in row))
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def parse_document(root: Path, data: bytes, version: str, proposals=None):
    """Return all independently parsed source groups without saving any bytes."""
    result = {"status": "quarantined", "errors": [], "groups": []}
    try:
        reject_excluded_text(data)
        if not isinstance(version, str) or version not in TEXT_MAPPINGS:
            raise ValueError("Text format is not registered for persistent intake")
        parsed = text_preview.preview_text(Path(root), data, version)
        lines = data.decode("utf-8-sig").splitlines()
    except (ValueError, UnicodeError, OSError) as exc:
        result["errors"].append(str(exc))
        return result
    result["errors"].extend(parsed["errors"])
    selected = []
    for block in parsed["blocks"]:
        result["errors"].extend(issue["reason"] for issue in block["issues"])
        for group in block["groups"]:
            result["errors"].extend(group["issues"])
            entries = group["candidate_records"]
            if group["context"]["grain"] == "store_period" and not any(
                    entry["lineage"] for entry in entries):
                continue
            selected.append((block, group))
    if not selected:
        result["errors"].append("Text has no declared metric or ranked source records")
    count = sum(len(group["candidate_records"]) for _, group in selected)
    if proposals is not None and (not isinstance(proposals, list) or len(proposals) != count):
        result["errors"].append("Proposals must match the number and order of declared text records")
        proposals = None
    cursor = 0
    for block, group in selected:
        entries = group["candidate_records"]
        proposed = None if proposals is None else proposals[cursor:cursor + len(entries)]
        cursor += len(entries)
        checked = preview.preview_csv(Path(root), _csv([entry["record"] for entry in entries]),
            preview.UploadContext(**group["context"]), proposed,
            mapping_version="canonical_csv_v2" if version == "manual_text_v3" else "canonical_csv_v1")
        checked["file_sha256"] = hashlib.sha256(data).hexdigest()
        checked["mapping_version"] = version
        for field in ("source_profile_sha256", "dictionary_sha256", "adapter_sha256"):
            if field in parsed:
                checked[field] = parsed[field]
        result["errors"].extend(checked["errors"])
        for row in checked["quarantined_records"]:
            result["errors"].extend(row["errors"])
        original_rows, cursors = [], {}
        try:
            for entry in entries:
                last, locator = _locator(lines, block, entry, cursors)
                original_rows.append({"record": entry["record"], "source_line_end": last,
                                      "source_locator": locator})
        except (ValueError, KeyError, IndexError) as exc:
            result["errors"].append(str(exc))
        # Temporary generated CSV positions and raw cells are never source evidence.
        checked["validated_records"] = original_rows if checked["status"] == "validated" else []
        checked["quarantined_records"] = []
        result["groups"].append({"source_block_line": block["source_line_start"],
            "dataset_id": group["context"]["dataset_id"], "context": group["context"], "preview": checked})
    if parsed["status"] != "validated" and not result["errors"]:
        result["errors"].append("Another source block needs review; the complete document is held")
    result["errors"] = list(dict.fromkeys(result["errors"]))
    if not result["errors"]:
        result["status"] = "validated"
    else:
        for group in result["groups"]:
            checked = group["preview"]
            checked["status"] = "quarantined"
            checked["validated_records"] = []
            checked["errors"] = list(result["errors"])
    return result
