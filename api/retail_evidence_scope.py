"""Match a declared retail request window to complete, unambiguous facts."""

from __future__ import annotations

import calendar
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import date

if __package__:
    from .retail_entity_scope import retail_fact_matches_entities
else:
    from retail_entity_scope import retail_fact_matches_entities


@dataclass(frozen=True)
class RetailWindow:
    period_start: str
    period_end: str

    def __post_init__(self):
        for value in (self.period_start, self.period_end):
            if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
                raise ValueError("Reporting dates must use YYYY-MM-DD.")
        start, end = date.fromisoformat(self.period_start), date.fromisoformat(self.period_end)
        if start > end or start.day != 1 or end.day != calendar.monthrange(end.year, end.month)[1]:
            raise ValueError("Specify a complete calendar-month window.")

    @property
    def period_granularity(self):
        return "month" if self.period_start[:7] == self.period_end[:7] else "month_range"

    @property
    def period_label(self):
        if self.period_granularity == "month":
            return self.period_start[:7]
        return self.period_start[:7] + "_to_" + self.period_end[:7]


DEMO1_WINDOW = RetailWindow("2026-02-01", "2026-04-30")
DEMO2_WINDOW = RetailWindow("2026-03-01", "2026-03-31")
MONTH_NAMES = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)
MONTH_NUMBERS = {name: index for index, name in enumerate(MONTH_NAMES, 1)}
MONTH_NUMBERS.update({name[:3]: index for index, name in enumerate(MONTH_NAMES, 1)})
MONTH_NUMBERS["sept"] = 9
CJK_MONTHS = dict(zip(("一", "二", "三", "四", "五", "六", "七", "八", "九", "十", "十一", "十二"), range(1, 13)))
MONTH_PATTERN = "|".join(sorted(MONTH_NUMBERS, key=len, reverse=True))
RANGE_SEPARATOR = r"\s*(?:to|through|[-–—~～至到]|_to_)\s*"


def _month_window(first, last):
    year, month = last
    return RetailWindow(date(*first, 1).isoformat(),
                        date(year, month, calendar.monthrange(year, month)[1]).isoformat())


def parse_retail_window(message: str, default: RetailWindow) -> RetailWindow:
    """Use the endpoint fixture only when the request omits time entirely.

    A month without a year uses the declared fixture year, never today's date.
    Unsupported or ambiguous time expressions do not fall back to that fixture.
    """
    original = unicodedata.normalize("NFKC", message or "")
    q = original.lower()
    if re.search(
        r"\b(?:daily|hourly|weekly|annual|annually|quarterly|q[1-4]|latest|yesterday|today|tomorrow)\b"
        r"|\b(?:per|by)\s+(?:day|hour|week)\b"
        r"|\b(?:day|hour|week)\s+\d+\b|\b(?:first|second)\s+half\s+of\b"
        r"|\b(?:last|this|next|previous|current)\s+(?:day|week|month|year|quarter)\b"
        r"|按日|按天|逐日|逐天|每日|每天|按小时|每小时|逐小时|按周|每周|逐周"
        r"|上月|上个月|本月|这个月|下月|下个月|去年|今年|明年|昨天|今天|明天|最近|最新|过去|季度|全年|年度"
        r"|上旬|中旬|下旬|月初|月底|前半月|后半月",
        q,
    ):
        raise ValueError("Specify an absolute reporting month or complete date window.")

    date_matches = list(re.finditer(r"(?<!\d)\d{4}[-/]\d{1,2}[-/]\d{1,2}(?!\d)", q))
    if date_matches:
        if len(date_matches) != 2:
            raise ValueError("Specify one complete reporting window.")
        left, right = date_matches
        if re.search(r"\b(?:after|before|since|until)\s*$", q[:left.start()]):
            raise ValueError("Specify a bounded reporting window.")
        if not re.fullmatch(RANGE_SEPARATOR, q[left.end():right.start()]):
            raise ValueError("Separate the reporting start and end dates with 'to' or '至'.")
        if re.match(r"(?:t\d|\s+\d{1,2}:)", q[right.end():]):
            raise ValueError("Sub-month timestamps are outside the current evidence scope.")
        start, end = (date(*map(int, re.split(r"[-/]", item.group()))) for item in date_matches)
        window = RetailWindow(start.isoformat(), end.isoformat())
        remainder = q[:left.start()] + " " + q[right.end():]
        if _month_mentions(remainder) or re.search(r"\d{4}|月|年", remainder):
            raise ValueError("The request contains additional or conflicting time references.")
        return window

    if re.search(
        r"(?<!\d)\d{1,2}[-/]\d{1,2}[-/]\d{4}(?!\d)"
        r"|(?:\d{1,2}|[一二三四五六七八九十]{1,3})月\s*"
        r"(?:\d{1,2}|[一二三四五六七八九十]{1,3})(?:日|号)"
        rf"|\b(?:{MONTH_PATTERN})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?\b"
        rf"|\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{MONTH_PATTERN})\b",
        q,
    ):
        raise ValueError("Sub-month dates are outside the current evidence scope.")

    mentions = _month_mentions(q, original)
    if any(re.search(r"\b(?:after|before|since|until)\s*$", q[:start]) for start, _, _, _ in mentions):
        raise ValueError("Specify a bounded reporting window.")
    remainder = q
    for start, end, _, _ in reversed(mentions):
        remainder = remainder[:start] + " " * (end - start) + remainder[end:]
    if re.search(r"(?<!\d)\d{4}(?!\d)|月|年|(?<!\d)\d{1,2}/\d{1,2}(?!\d)", remainder):
        raise ValueError("Specify the reporting month or an unambiguous complete window.")
    if any(re.search(r"\d+\s*[-–—~～至到]\s*$", q[:start]) for start, _, _, _ in mentions):
        raise ValueError("Write both month endpoints explicitly, such as 3月到4月.")
    if not mentions:
        return default
    explicit_years = {year for _, _, year, _ in mentions if year is not None}
    if any(year is None for _, _, year, _ in mentions) and len(explicit_years) > 1:
        raise ValueError("A month without a year is ambiguous across multiple years.")
    default_year = next(iter(explicit_years), int(default.period_start[:4]))
    months = [(year or default_year, month) for _, _, year, month in mentions]
    for year, month in months:
        date(year, month, 1)
    if len(set(months)) == 1:
        return _month_window(months[0], months[0])
    if len(months) != 2:
        raise ValueError("Specify one reporting month or one continuous window.")
    separator = q[mentions[0][1]:mentions[1][0]]
    if not re.fullmatch(RANGE_SEPARATOR, separator):
        adjacent = (months[1][0] * 12 + months[1][1]) - (months[0][0] * 12 + months[0][1]) == 1
        if not adjacent or not re.fullmatch(r"\s*(?:and|与|和|、|,)\s*", separator):
            raise ValueError("Specify the requested months as one continuous window.")
    return _month_window(months[0], months[1])


def _month_mentions(q, original=None):
    patterns = (
        (r"(?<!\d)(?P<year>\d{4})[-/](?P<month>\d{1,2})(?![\d/-])", "numeric"),
        (r"(?:(?P<year>\d{4})\s*年\s*)?(?P<month>\d{1,2}|十一|十二|十|[一二三四五六七八九])\s*月", "chinese"),
        (rf"(?<![a-z0-9])(?:(?P<before>\d{{4}})\s+)?(?P<month>{MONTH_PATTERN})\b\.?"
         r"(?:\s+(?P<year>\d{4})(?!\d))?", "english"),
    )
    found = []
    for pattern, kind in patterns:
        for match in re.finditer(pattern, q):
            if any(match.start() < end and match.end() > start for start, end, _, _ in found):
                continue
            year_text = match.groupdict().get("year")
            before = match.groupdict().get("before")
            if before and year_text and before != year_text:
                raise ValueError("Conflicting years for the same month.")
            year = int(year_text or before) if year_text or before else None
            value = match.group("month")
            if kind == "english":
                # Do not mistake modal 'may' for a date in ordinary questions.
                if value == "may" and year is None and not re.search(r"\b(?:in|during|for|from|to|through|and)\s*$", q[:match.start()]):
                    if original is None or original[match.start():match.start()+3] != "May":
                        continue
                month = MONTH_NUMBERS[value]
            else:
                month = CJK_MONTHS[value] if value in CJK_MONTHS else int(value)
            found.append((match.start(), match.end(), year, month))
    return sorted(found)


def fact_window(fact: dict) -> RetailWindow:
    window = RetailWindow(fact.get("period_start"), fact.get("period_end"))
    if fact.get("period_label") != window.period_label or fact.get("period_granularity") != window.period_granularity:
        raise ValueError("Fact dates, period_label and period_granularity disagree.")
    if "period_month" in fact and (window.period_granularity != "month" or fact["period_month"] != window.period_label):
        raise ValueError("Fact period_month conflicts with its dates.")
    # Comparative facts may include baseline periods in observed_values,
    # as documented in DATA_DICTIONARY.md. Their target window stays explicit.
    return window


def select_retail_points(points, *, entity_ids, required_slots, window, limit, min_score=None):
    expected = {(entity, slot) for entity in entity_ids for slot in required_slots}
    if not expected:
        return [], "No retail evidence scope was selected."
    if len(expected) > limit:
        return [], f"The requested evidence requires {len(expected)} facts; increase top_k to at least {len(expected)}."
    if not isinstance(points, list):
        return [], "The retrieved retail evidence is not a fact list."
    selected, seen = {}, set()
    for point in points:
        if not isinstance(point, dict) or not retail_fact_matches_entities(point.get("payload"), entity_ids):
            return [], "The retrieved facts do not match the requested retail store scope."
        fact = point["payload"]
        if not isinstance(fact.get("slot"), str):
            return [], "The retrieved fact has no valid slot."
        if fact["slot"] not in required_slots:
            continue
        try:
            actual_window = fact_window(fact)
        except (ValueError, TypeError) as exc:
            return [], f"Invalid retail fact period metadata: {exc}"
        if actual_window != window:
            continue
        if not all(isinstance(fact.get(field), str) and fact[field].strip()
                   for field in ("value", "calculation", "source_path", "lineage_path")):
            return [], "The requested fact is missing its statement or source trace."
        if not isinstance(fact.get("observed_values"), dict) or not fact["observed_values"]:
            return [], "The requested fact is missing observed_values."
        for field in ("source_fields", "limitations"):
            if (not isinstance(fact.get(field), list) or not fact[field]
                    or not all(isinstance(item, str) and item.strip() for item in fact[field])):
                return [], f"The requested fact is missing {field}."
        if not isinstance(fact.get("confidence"), str) or fact["confidence"] not in {"high", "medium"}:
            return [], "The requested fact has no registered active trace-confidence level."
        key = (fact["entity_id"].lower(), fact["slot"])
        if key in seen:
            return [], "Multiple active facts have the same store, slot and window; select a source version before answering."
        seen.add(key)
        if min_score is not None:
            score = point.get("score")
            if type(score) not in (int, float) or not math.isfinite(score) or score < min_score:
                continue
        selected[key] = point
    missing = expected - set(selected)
    if missing:
        labels = ", ".join(entity + "/" + slot for entity, slot in sorted(missing))
        return [], f"No complete evidence for {window.period_label}; missing: {labels}."
    return list(selected.values()), None
