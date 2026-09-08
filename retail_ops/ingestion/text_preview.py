"""Read the registered Chinese notes format and preview its source-backed rows."""

from __future__ import annotations

import argparse
import calendar
import csv
import hashlib
import io
import json
import re
from collections import Counter
from dataclasses import asdict
from datetime import date
from pathlib import Path

from . import preview, source_windows


PROFILE_PATH = Path("retail_ops/contracts/manual_text.v1.json")
PROFILE_PATHS = {
    "manual_text_v1": PROFILE_PATH,
    "manual_text_v2": Path("retail_ops/contracts/manual_text.v2.json"),
    "manual_text_v3": Path("retail_ops/contracts/manual_text.v3.json"),
}
PARSERS = {
    "search_terms": ("store_search_term_period", None),
    "sku_sales_volume": ("store_sku_period", "sales_volume"),
    "sku_transaction_amount": ("store_sku_period", "transaction_amount"),
}
SEARCH_FIELDS = {"曝光次数": "search_term_exposure_times", "点击次数": "search_term_click_times",
                 "成单次数": "search_term_order_times"}


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate source-profile JSON key")
        result[key] = value
    return result


def _load_profile(root, version):
    if version not in PROFILE_PATHS:
        raise ValueError("source format is not registered")
    data = (root / PROFILE_PATHS[version]).read_bytes()
    profile = json.loads(data, object_pairs_hook=_unique)
    if profile["mapping_version"] != version:
        raise ValueError("source format is not registered")
    if version == "manual_text_v3" and (
        profile.get("window_format") != "iso_date_inclusive_range"
        or profile.get("canonical_mapping_version") != "canonical_csv_v2"
        or profile.get("sku_amount_format") != "labelled_amount_with_yuan"
        or set(profile["fields"]) & {
            "搜索曝光", "搜索入店", "商家列表曝光", "商家列表入店",
            "活动专区曝光", "活动专区入店", "订单页入店", "其他入店",
        }
    ):
        raise ValueError("manual_text_v3 requires its registered dates and explicit units")
    dataset = profile["store_dataset_id"]
    if dataset not in preview.SCHEMAS or preview.ROUTES[dataset] != ("store_period", None):
        raise ValueError("store source profile requires an explicit store-period schema")
    aliases = profile["store_aliases"]
    if not isinstance(aliases, dict) or not aliases or any(
        not isinstance(k, str) or not k.strip() or not isinstance(v, str) or not v.strip()
        for k, v in aliases.items()
    ):
        raise ValueError("source profile requires registered store aliases")
    for label, rule in profile["fields"].items():
        field = rule["field"]
        if label in preview.IGNORED or field not in preview.SCHEMAS[dataset] - preview.KEYS:
            raise ValueError("source field is excluded or absent from the dataset schema")
        if rule["format"] not in {"number", "percent", "value"}:
            raise ValueError("source field format is not registered")
        if (rule["format"] == "percent") != field.endswith("_pct"):
            raise ValueError("percent display format must agree with the canonical field")
        if rule["format"] == "value" and (field not in preview.TEXT or not rule.get("values")):
            raise ValueError("text aliases require an explicit value mapping")
    if set(profile["pending_labels"]) & (set(profile["fields"]) | preview.IGNORED):
        raise ValueError("pending labels overlap mapped or excluded labels")
    headers = set()
    for section in profile["sections"]:
        route = (section["grain"], section["ranking_basis"])
        if section["header"] in headers or not section["header"]:
            raise ValueError("source section headings must be unique")
        headers.add(section["header"])
        if section["dataset_id"] not in preview.SCHEMAS or preview.ROUTES[section["dataset_id"]] != route:
            raise ValueError("source section route is not registered")
        if PARSERS.get(section["parser"]) != route or section["expected_items"] != 3:
            raise ValueError("source section parser is not registered")
    return profile, hashlib.sha256(data).hexdigest()


def _value_tail(line, label):
    """Exact label boundary; a longer unfamiliar Chinese label cannot match."""
    if not line.startswith(label):
        return None
    rest = line[len(label):]
    if rest and not (rest[0].isspace() or rest[0] in ":：+-0123456789"):
        return None
    return rest.lstrip().lstrip(":：").strip()


def _window(text, version="manual_text_v1"):
    if version == "manual_text_v3":
        matched = re.fullmatch(r"([0-9]{4}-[0-9]{2}-[0-9]{2})至([0-9]{4}-[0-9]{2}-[0-9]{2})", text)
        if not matched:
            raise ValueError("manual_text_v3 requires YYYY-MM-DD至YYYY-MM-DD")
        start, end = matched.groups()
        month = source_windows.validate_source_window(start, end, "canonical_csv_v2")
        return {"period_start": start, "period_end": end, "period_month": month}
    month = re.fullmatch(r"([0-9]{4})[.-]([0-9]{1,2})", text)
    if month:
        year, number = map(int, month.groups())
        start = date(year, number, 1)
        end = date(year, number, calendar.monthrange(year, number)[1])
    else:
        window = re.fullmatch(r"([0-9]{4})\.([0-9]{1,2})\.([0-9]{1,2})-([0-9]{1,2})\.([0-9]{1,2})", text)
        if not window:
            raise ValueError("source window format is not registered")
        year, month, day, end_month, end_day = map(int, window.groups())
        start, end = date(year, month, day), date(year, end_month, end_day)
        if start.day != 1 or end != date(year, month, calendar.monthrange(year, month)[1]):
            raise ValueError("current source profile requires a complete calendar-month window")
    return {"period_start": start.isoformat(), "period_end": end.isoformat(), "period_month": start.strftime("%Y-%m")}


def _split_items(body):
    stack, items, start = [], [], 0
    closing = {"）": "（", ")": "("}
    for index, char in enumerate(body):
        if char in "（(":
            stack.append(char)
        elif char in closing:
            if not stack or stack.pop() != closing[char]:
                raise ValueError("unbalanced parentheses in source list")
        elif char in "，," and not stack:
            items.append(body[start:index].strip())
            start = index + 1
    if stack:
        raise ValueError("unbalanced parentheses in source list")
    items.append(body[start:].strip())
    return items


def _list_rows(body, parser, *, labelled_amount=False):
    items = _split_items(body)
    if len(items) != 3 or any(not item for item in items):
        raise ValueError("the registered Top 3 list needs three explicit source entries")
    rows = []
    for rank, item in enumerate(items, 1):
        if parser == "search_terms":
            match = re.fullmatch(r"(.+)[（(]([^（）()]*)[）)]", item)
            if not match:
                raise ValueError("search-term entry does not match the registered format")
            name, values = match.groups()
            row = {"search_term_rank": rank, "search_term": name.strip()}
            for pair in re.split("[，,]", values) if values.strip() else []:
                found = [(field, _value_tail(pair.strip(), label)) for label, field in SEARCH_FIELDS.items()]
                found = [(field, value) for field, value in found if value is not None]
                if len(found) != 1 or found[0][0] in row:
                    raise ValueError("search-term metric is unknown or repeated")
                field, value = found[0]
                row[field] = preview._value(field, value)
        elif parser == "sku_sales_volume":
            match = re.fullmatch(r"(.+)[（(]销量\s*[:：]?\s*([^（）()]*)[）)]", item)
            if not match:
                raise ValueError("SKU sales entry requires an explicit 销量 label")
            name, value = match.groups()
            row = {"sku_rank": rank, "sku_name": name.strip(), "sales_volume": preview._value("sales_volume", value)}
        else:
            # In this registered legacy format the trailing numeric run is the amount.
            # Digit-ending names require the explicit labelled format below.
            match = re.fullmatch(r"(.+)[（(]成交金额\s*[:：]?\s*([^（）()]*)[）)]", item)
            if match:
                name, value = match.groups()
                value = value.strip()
                if value and not value.endswith("元"):
                    raise ValueError("labelled SKU amount requires 元")
                value = value[:-1] if value else value
            else:
                if labelled_amount:
                    raise ValueError("manual_text_v3 SKU amounts require an explicit 成交金额 label")
                match = re.fullmatch(r"(.+?[^0-9.])([+-]?[0-9]+(?:\.[0-9]+)?)元", item)
                if not match:
                    raise ValueError("SKU amount entry needs a name followed by an amount and 元")
                name, value = match.groups()
            row = {"sku_rank": rank, "sku_name": name.strip(),
                   "sku_transaction_amount": preview._value("sku_transaction_amount", value)}
        if not row.get("sku_name", row.get("search_term")):
            raise ValueError("source list name is missing")
        rows.append((row, item))
    return rows


def preview_text(root: Path, data: bytes, version: str, expected_store_ids=None, proposals=None):
    result = {"mode": "preview", "status": "quarantined", "file_sha256": hashlib.sha256(data).hexdigest(),
              "mapping_version": version, "errors": [], "blocks": [], "validated_records": []}
    try:
        profile, profile_hash = _load_profile(root, version)
        lines = data.decode("utf-8-sig").splitlines()
        result["source_profile_sha256"] = profile_hash
        result["dictionary_sha256"] = hashlib.sha256((root / "retail_ops/data/DATA_DICTIONARY.md").read_bytes()).hexdigest()
        result["adapter_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        if expected_store_ids is not None and (
            not isinstance(expected_store_ids, (list, tuple)) or not expected_store_ids
            or any(not isinstance(store, str) or store not in profile["store_aliases"].values() for store in expected_store_ids)
        ):
            raise ValueError("confirmed upload scope includes an unregistered store_id")
    except (ValueError, KeyError, TypeError, OSError) as exc:
        result["errors"].append(str(exc))
        return result

    block = None
    frozen = False
    for number, original in enumerate(lines, 1):
        line = original.strip()
        if not line or line in profile["ignored_notes"]:
            continue
        if any(_value_tail(line, label) is not None for label in preview.IGNORED):
            continue
        store_header = re.fullmatch(r"店铺\s*[:：]\s*(\S+)", line)
        if store_header:
            alias = store_header[1]
            store = profile["store_aliases"].get(alias)
            block = {"source_line_start": number, "store_id": store, "period": None,
                     "source_store": {"source_line": number, "source_text": line, "source_alias": alias},
                     "issues": [], "unmapped_lines": [], "groups": [], "store_record": {}, "store_lineage": []}
            result["blocks"].append(block)
            frozen = False
            if store is None or (expected_store_ids is not None and store not in expected_store_ids):
                block["issues"].append({"source_line": number, "source_text": line, "reason": "store is outside the registered or confirmed upload scope"})
                frozen = True
            continue
        if block is None:
            result["errors"].append(f"line {number}: content has no registered store header")
            continue
        if frozen:
            block["unmapped_lines"].append({"source_line": number, "source_text": line})
            continue
        if line.startswith("时间范围"):
            try:
                if block["period"] is not None:
                    raise ValueError("a second source window requires a new store block")
                block["period"] = _window(line[len("时间范围"):].lstrip(" :："), version)
                block["source_window"] = {"source_line": number, "source_text": line}
            except ValueError as exc:
                block["issues"].append({"source_line": number, "source_text": line, "reason": str(exc)})
                frozen = True
            continue
        if block["period"] is None:
            block["issues"].append({"source_line": number, "source_text": line, "reason": "report data appears before a confirmed source window"})
            frozen = True
            continue
        section_match = [(section, re.fullmatch(re.escape(section["header"]) + r"\s*[:：]\s*(.*)", line))
                         for section in profile["sections"]]
        section_match = [(section, match) for section, match in section_match if match]
        if section_match:
            section, match = section_match[0]
            try:
                if any(group["context"]["dataset_id"] == section["dataset_id"] for group in block["groups"]):
                    raise ValueError("source list is repeated in the same store window")
                rows = _list_rows(match[1], section["parser"], labelled_amount=version == "manual_text_v3")
                context = preview.UploadContext(section["dataset_id"], block["store_id"],
                    block["period"]["period_start"], block["period"]["period_end"], section["grain"], section["ranking_basis"])
                block["groups"].append({"context": asdict(context), "candidate_records": [
                    {"record": {"store_id": block["store_id"], **block["period"], **row},
                     "lineage": [{"source_line": number, "source_text": item, "fields": list(row)}]}
                    for row, item in rows], "issues": []})
            except ValueError as exc:
                block["issues"].append({"source_line": number, "source_text": line, "reason": str(exc)})
                frozen = True
            continue
        pending = next((label for label in profile["pending_labels"] if _value_tail(line, label) is not None), None)
        if pending:
            if version == "manual_text_v3" or _value_tail(line, pending):
                block["issues"].append({"source_line": number, "source_text": line, "reason": "source label needs a confirmed unit", "source_label": pending})
            continue
        mapped = False
        for label, rule in profile["fields"].items():
            tail = _value_tail(line, label)
            if rule["format"] == "value" and line.startswith(label):
                tail = line[len(label):].lstrip(" :：")
            if tail is None:
                continue
            mapped = True
            field = rule["field"]
            try:
                if field in block["store_record"]:
                    raise ValueError("source metric is repeated in the same store window")
                block["store_record"][field] = None
                value = tail
                if tail and rule["format"] == "percent":
                    if not tail.endswith("%"):
                        raise ValueError("source percentage requires an explicit % sign")
                    value = tail[:-1]
                elif tail and rule["format"] == "value":
                    if tail not in rule["values"]:
                        raise ValueError("source text value is not registered")
                    value = rule["values"][tail]
                block["store_record"][field] = preview._value(field, value)
                block["store_lineage"].append({"source_line": number, "source_text": line,
                                                "fields": [field], "source_label": label, "source_value": tail})
            except ValueError as exc:
                block["issues"].append({"source_line": number, "source_text": line, "reason": str(exc)})
                frozen = True
            break
        if not mapped:
            block["issues"].append({"source_line": number, "source_text": line, "reason": "unknown source field or section; remaining block is held"})
            frozen = True

    if not result["blocks"]:
        result["errors"].append("no registered store blocks found")
    for block in result["blocks"]:
        if block["period"] is None:
            block["issues"].append({"source_line": block["source_line_start"], "reason": "store block has no confirmed window"})
        if block["period"] is not None and block["store_id"] is not None:
            context = preview.UploadContext(profile["store_dataset_id"], block["store_id"],
                block["period"]["period_start"], block["period"]["period_end"], "store_period")
            block["groups"].insert(0, {"context": asdict(context), "candidate_records": [{
                "record": {"store_id": block["store_id"], **block["period"], **block["store_record"]},
                "lineage": block["store_lineage"]}], "issues": []})
        del block["store_record"], block["store_lineage"]

    groups = [(block, group) for block in result["blocks"] for group in block["groups"]]
    count = sum(len(group["candidate_records"]) for _, group in groups)
    if proposals is not None and (not isinstance(proposals, list) or len(proposals) != count):
        result["errors"].append("proposals must match the number and order of parsed source rows")
        proposals = None
    cursor, seen = 0, set()
    for block, group in groups:
        scope = preview.UploadContext(**group["context"])
        rows = [entry["record"] for entry in group["candidate_records"]]
        buffer = io.StringIO(newline="")
        columns = list(dict.fromkeys(field for row in rows for field in row))
        writer = csv.DictWriter(buffer, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
        proposed = None if proposals is None else proposals[cursor:cursor + len(rows)]
        cursor += len(rows)
        checked = preview.preview_csv(root, buffer.getvalue().encode(), scope, proposed,
            mapping_version="canonical_csv_v2" if version == "manual_text_v3" else "canonical_csv_v1")
        group["schema_sha256"] = checked.get("schema_sha256")
        if checked["status"] != "validated":
            group["issues"].extend(checked["errors"])
            for row in checked["quarantined_records"]:
                group["issues"].extend(row["errors"])
        else:
            for entry, validated in zip(group["candidate_records"], checked["validated_records"]):
                entry["record"] = validated["record"]
        for row in rows:
            rank = row.get("sku_rank", row.get("search_term_rank"))
            key = (scope.dataset_id, scope.store_id, scope.period_start, scope.period_end, rank)
            if key in seen:
                group["issues"].append("repeated source key across blocks; no version selected")
            seen.add(key)
    failed = bool(result["errors"]) or any(block["issues"] or group["issues"] for block, group in groups)
    failed = failed or any(block["issues"] for block in result["blocks"])
    if not failed:
        result["status"] = "validated"
        result["validated_records"] = [{"context": group["context"], **entry} for _, group in groups for entry in group["candidate_records"]]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--expected-store-id", action="append")
    parser.add_argument("--proposals", type=Path)
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    try:
        proposals = json.loads(args.proposals.read_text(encoding="utf-8"), object_pairs_hook=preview._json_object,
                               parse_float=preview.Decimal) if args.proposals else None
        result = preview_text(Path(__file__).resolve().parents[2], args.input.read_bytes(), args.profile,
                              args.expected_store_id, proposals)
        result["source_file"] = str(args.input)
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Cannot preview source text: {exc}\n")
    output = result
    if args.summary:
        output = {key: result[key] for key in ("mode", "status", "file_sha256", "mapping_version", "errors")}
        output["store_period_blocks"] = len(result["blocks"])
        output["candidate_records"] = sum(len(group["candidate_records"]) for block in result["blocks"] for group in block["groups"])
        output["validated_records"] = len(result["validated_records"])
        output["issues"] = dict(Counter(
            [item["reason"] for block in result["blocks"] for item in block["issues"]]
            + [reason for block in result["blocks"] for group in block["groups"] for reason in group["issues"]]
        ))
    print(preview.preview_json(output))
    return 0 if result["status"] == "validated" else 2


if __name__ == "__main__":
    raise SystemExit(main())
