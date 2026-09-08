"""Validate the inclusive dates covered by an uploaded source report."""
from __future__ import annotations

import calendar
import re
from datetime import date


MAPPING_VERSIONS = frozenset({"canonical_csv_v1", "canonical_csv_v2"})
# These datasets already use period keys. This explicit list registers which
# sources the v2 adapter accepts outside a full natural month.
ACTUAL_WINDOW_DATASETS = frozenset({
    "demo2_store_period_metrics",
    "store_period_panel_metrics",
    "demo2_top_skus_by_transaction_amount",
    "demo2_top_skus_by_sales_volume",
    "demo2_top_search_terms",
})
MONTHLY_DATASETS = frozenset({"store_a_monthly_metrics", "store_a_top_skus"})


def validate_source_window(start, end, mapping_version):
    """Return a month label only when the source covers one complete month.

    This return value validates source metadata; callers must not insert it
    into a record whose original period_month is absent.
    """
    if not isinstance(mapping_version, str) or mapping_version not in MAPPING_VERSIONS:
        raise ValueError("source format is not registered for persistent intake")
    for value in (start, end):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError("upload window requires YYYY-MM-DD")
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if last < first:
        raise ValueError("source period_end is earlier than period_start")
    whole_month = first.day == 1 and last == date(
        first.year, first.month, calendar.monthrange(first.year, first.month)[1])
    if mapping_version == "canonical_csv_v1" and not whole_month:
        raise ValueError("canonical_csv_v1 requires a complete calendar-month window")
    return start[:7] if whole_month else None


def validate_source_dataset(dataset_id, mapping_version, start, end):
    """Apply the explicitly registered source-window policy for this dataset."""
    if not isinstance(dataset_id, str) or dataset_id not in ACTUAL_WINDOW_DATASETS | MONTHLY_DATASETS:
        raise ValueError("dataset has no registered source-window policy")
    month = validate_source_window(start, end, mapping_version)
    if month is None and dataset_id not in ACTUAL_WINDOW_DATASETS:
        raise ValueError("this dataset requires a complete calendar-month source window")
    return month
