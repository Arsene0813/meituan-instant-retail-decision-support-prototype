"""Validate fixed RAC CSV sources without changing their recorded values."""

from __future__ import annotations

import calendar
import csv
import re
from decimal import Decimal
from pathlib import Path

from retail_ops.ingestion import preview
from retail_ops.sql_runtime import read_source


MONTH_KEYS = ("store_id", "period_month", "period_start", "period_end")


def checked_path(root: Path, source_path: str) -> Path:
    base = root.resolve()
    path = (base / source_path).resolve()
    if base not in path.parents:
        raise ValueError("source path leaves repository root")
    return path


def read_csv(root: Path, source_path: str) -> tuple[list[str], list[dict[str, str]]]:
    with checked_path(root, source_path).open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        reader = csv.reader(handle, strict=True)
        headers = next(reader, [])
        if not headers or len(headers) != len(set(headers)) or any(not h for h in headers):
            raise ValueError("empty or duplicate CSV header")
        rows = []
        for cells in reader:
            if not cells:
                continue
            if len(cells) != len(headers):
                raise ValueError(f"wrong cell count at source line {reader.line_num}")
            rows.append(dict(zip(headers, cells)))
    return headers, rows


def require_fields(headers: list[str], fields) -> None:
    missing = sorted(set(fields) - set(headers))
    if missing:
        raise ValueError("Missing canonical fields: " + ", ".join(missing))


def validate_month_rows(rows: list[dict[str, str]]) -> None:
    seen = set()
    for index, row in enumerate(rows, 2):
        values = {field: preview._value(field, row.get(field)) for field in MONTH_KEYS}
        if any(value is None for value in values.values()):
            raise ValueError(f"row {index}: missing store or reporting-window metadata")
        # Store identity and window labels must be canonical in the source;
        # whitespace is not an independent store-alias registration.
        if any(row[field] != values[field] for field in MONTH_KEYS):
            raise ValueError(f"row {index}: noncanonical store or reporting-window metadata")
        start = preview._date(values["period_start"])
        end = preview._date(values["period_end"])
        if start.day != 1 or end != start.replace(day=calendar.monthrange(start.year, start.month)[1]):
            raise ValueError(f"row {index}: expected a complete calendar-month window")
        if values["period_month"] != values["period_start"][:7]:
            raise ValueError(f"row {index}: month label conflicts with reporting-window dates")
        key = (values["store_id"], values["period_start"], values["period_end"])
        if key in seen:
            raise ValueError(f"row {index}: duplicate logical key; select a source version")
        seen.add(key)


def exact_output_number(value: str | None, *, count: bool = False) -> Decimal | None:
    """SQL output may use exponent notation or integral decimal counts."""
    if value is None or not value.strip():
        return None
    text = value.strip()
    if not re.fullmatch(r"[+-]?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?", text):
        raise ValueError("invalid exact SQL numeric text")
    number = Decimal(text)
    if not number.is_finite():
        raise ValueError("non-finite SQL numeric value")
    if count and (number < 0 or number != number.to_integral_value()):
        raise ValueError("expected a non-negative integral SQL count")
    return number


def canonical_rows(root: Path, dataset_id: str) -> tuple[list[str], list[dict[str, str]]]:
    """Use the registered source contract while preserving raw evidence text."""
    checked_path(root, f"retail_ops/data/{dataset_id}.csv")
    headers, rows = read_source(root, dataset_id)
    schema = preview.SCHEMAS[dataset_id]
    if set(headers) - schema - preview.IGNORED:
        raise ValueError(f"{dataset_id}: unregistered source columns")
    require_fields(headers, MONTH_KEYS)
    validate_month_rows(rows)
    for index, row in enumerate(rows, 2):
        for field in set(headers) & schema:
            try:
                preview._value(field, row[field])
            except (ValueError, ArithmeticError) as exc:
                raise ValueError(f"row {index}: {field}: {exc}") from exc
    return headers, rows
