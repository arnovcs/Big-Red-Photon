"""Tiny parser for the common OSM `opening_hours` forms (§9.3). No dependencies.

Supported:  "24/7"
            "Mo-Su 11:00-22:00"
            "Mo-Fr 07:00-20:00; Sa-Su 09:00-20:00"
            "Mo,We,Fr 10:00-14:00,17:00-22:00"  (spaces after commas are fine)
            "07:00-19:00"                  (no days = every day)
            "Tu-Su 17:00-02:00"            (past midnight)
            "Mo off" / "Mo closed"         (in a rule list)
Anything else → "unknown". Never raises.

Later rules override earlier ones for the days they name (OSM semantics).
"""

import re
from datetime import datetime, timedelta
from typing import Literal

Status = Literal["open", "closed", "unknown"]

DAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
_DAY_SPEC = re.compile(r"^(Mo|Tu|We|Th|Fr|Sa|Su)(-(Mo|Tu|We|Th|Fr|Sa|Su))?$")
_RANGE = re.compile(r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$")

# One day's schedule: list of (open, close) minute offsets from that day's midnight.
# close may exceed 24*60 for ranges that run past midnight.
Schedule = dict[int, list[tuple[int, int]]]


def _days(spec: str) -> list[int] | None:
    days: list[int] = []
    for part in spec.split(","):
        match = _DAY_SPEC.match(part)
        if match is None:
            return None
        start = DAYS.index(match.group(1))
        end = DAYS.index(match.group(3)) if match.group(3) else start
        span = (end - start) % 7
        days += [(start + i) % 7 for i in range(span + 1)]
    return days


def _ranges(spec: str) -> list[tuple[int, int]] | None:
    if spec in {"off", "closed"}:
        return []
    ranges = []
    for part in spec.split(","):
        match = _RANGE.match(part.strip())
        if match is None:
            return None
        h1, m1, h2, m2 = (int(g) for g in match.groups())
        if h1 > 24 or h2 > 24 or m1 > 59 or m2 > 59:
            return None
        start, end = h1 * 60 + m1, h2 * 60 + m2
        if end <= start:
            end += 24 * 60  # runs past midnight
        ranges.append((start, end))
    return ranges


def parse(hours: str | None) -> Schedule | None:
    """Weekday (0=Mo) → open ranges, or None if the string isn't a supported form."""
    if not hours or not hours.strip():
        return None
    text = hours.strip()
    if text == "24/7":
        return {day: [(0, 24 * 60)] for day in range(7)}
    # "Tu-We, Sa-Su 17:00-01:00" → "Tu-We,Sa-Su 17:00-01:00"
    text = re.sub(r"\s*,\s*", ",", text)
    schedule: Schedule = {}
    for rule in text.split(";"):
        rule = rule.strip()
        if not rule:
            continue
        pieces = rule.split(None, 1)
        if len(pieces) == 1:
            # No day list ("07:00-19:00") means every day.
            days, ranges = list(range(7)), _ranges(pieces[0])
        else:
            days, ranges = _days(pieces[0]), _ranges(pieces[1].strip())
        if days is None or ranges is None:
            return None
        for day in days:
            schedule[day] = ranges
    return schedule or None


def status_at(hours: str | None, at: datetime) -> tuple[Status, datetime | None]:
    """(open/closed/unknown, closing time if open) for a local, timezone-aware `at`."""
    schedule = parse(hours)
    if schedule is None:
        return "unknown", None
    midnight = at.replace(hour=0, minute=0, second=0, microsecond=0)
    minute = at.hour * 60 + at.minute
    today, yesterday = at.weekday(), (at.weekday() - 1) % 7
    # Today's ranges, plus yesterday's ranges that run past midnight into today.
    candidates = [(midnight, r) for r in schedule.get(today, [])]
    candidates += [(midnight - timedelta(days=1), r) for r in schedule.get(yesterday, [])]
    for day_start, (start, end) in candidates:
        offset = minute + (24 * 60 if day_start < midnight else 0)
        if start <= offset < end:
            return "open", day_start + timedelta(minutes=end)
    return "closed", None


def is_supported(hours: str | None) -> bool:
    return parse(hours) is not None
