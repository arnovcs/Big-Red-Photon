"""Simple OSM opening_hours parsing (§9.3): common forms only, unknown otherwise."""

from datetime import datetime

import pytest

from app.providers.opening_hours import status_at

# 2026-10-03 is a Saturday; 2026-10-05 is a Monday.
SAT = "2026-10-03"
MON = "2026-10-05"


def at(day: str, hhmm: str) -> datetime:
    return datetime.fromisoformat(f"{day}T{hhmm}:00-04:00")


@pytest.mark.parametrize(
    ("hours", "when", "expected"),
    [
        ("24/7", at(SAT, "03:00"), "open"),
        ("Mo-Su 11:00-22:00", at(SAT, "18:00"), "open"),
        ("Mo-Su 11:00-22:00", at(SAT, "22:00"), "closed"),
        ("Mo-Su 11:00-22:00", at(SAT, "10:59"), "closed"),
        ("Mo-Fr 07:00-20:00; Sa-Su 09:00-20:00", at(SAT, "08:00"), "closed"),
        ("Mo-Fr 07:00-20:00; Sa-Su 09:00-20:00", at(MON, "08:00"), "open"),
        ("Mo,We,Fr 10:00-14:00,17:00-22:00", at(MON, "15:00"), "closed"),
        ("Mo,We,Fr 10:00-14:00,17:00-22:00", at(MON, "18:00"), "open"),
        ("Mo-Su 11:00-22:00; Sa off", at(SAT, "18:00"), "closed"),
        ("Fr-Sa 17:00-02:00", at("2026-10-04", "01:30"), "open"),  # Sat night past midnight
        ("Tu-We, Sa-Su 17:00-01:00", at("2026-10-04", "09:30"), "closed"),  # Chanticleer
        ("Tu-We, Sa-Su 17:00-01:00", at("2026-10-04", "18:00"), "open"),
        ("Mo, We, Fr 17:00-00:00; Tu 16:00-00:00", at(MON, "17:30"), "open"),
        ("Mo-Fr 10:00-14:00, 17:00-22:00", at(MON, "18:00"), "open"),
        ("07:00-19:00", at("2026-10-04", "09:30"), "open"),  # no days = every day
        ("07:00-19:00", at(SAT, "20:00"), "closed"),
    ],
)
def test_common_forms(hours: str, when: datetime, expected: str) -> None:
    assert status_at(hours, when)[0] == expected


def test_closing_time_is_returned_when_open() -> None:
    status, closes = status_at("Mo-Su 11:00-22:00", at(SAT, "18:00"))
    assert status == "open"
    assert closes == at(SAT, "22:00")


def test_past_midnight_closing_time_is_next_day() -> None:
    _, closes = status_at("Fr-Sa 17:00-02:00", at(SAT, "23:00"))
    assert closes == at("2026-10-04", "02:00")


@pytest.mark.parametrize(
    "hours",
    [
        None,
        "",
        "Mo-Fr 11:00-14:00; PH off",
        "sunrise-sunset",
        'Mo-Fr 11:00-14:00 || "call us"',
        "Jan-Mar Mo 10:00-12:00",
        "Mo-Fr 25:00-26:00",
        "Mo-Fr 08:00-15:00, Sa-Su 09:00-16:00",  # comma used as a rule separator
        "garbage",
    ],
)
def test_anything_else_is_unknown_without_crashing(hours) -> None:
    assert status_at(hours, at(SAT, "18:00")) == ("unknown", None)
