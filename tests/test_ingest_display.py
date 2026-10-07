"""Times, spans and sizes as the ingest pages say them."""
from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.core.ingest import display

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("value", "tz", "expected"),
    [
        ("2026-10-06T10:03:00+00:00", "Europe/Berlin", "Today 12:03"),
        ("2026-10-05T07:30:00.123456+00:00", "UTC", "Yesterday 07:30"),
        ("2026-10-07T01:00:00+00:00", "UTC", "Tomorrow 01:00"),
        ("2026-10-01T22:15:00+00:00", "UTC", "Thu 01 Oct 22:15"),
        ("2025-03-02T22:15:00+00:00", "UTC", "02 Mar 2025 22:15"),
        ("2026-10-06T22:30:00+00:00", "Europe/Berlin", "Tomorrow 00:30"),
        ("2026-10-06T10:03:00", "UTC", "Today 10:03"),
        ("garbage", "UTC", "garbage"),
        ("", "UTC", ""),
        (None, "UTC", ""),
    ],
)
def test_a_time_is_shown_in_the_schedules_zone(value: object, tz: str, expected: str) -> None:
    assert display.local_time(value, tz, now=NOW) == expected


def test_an_unknown_zone_falls_back_to_utc() -> None:
    assert display.local_time("2026-10-06T10:03:00+00:00", "Nowhere/Land", now=NOW) == "Today 10:03"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-10-06T11:59:50+00:00", "a moment ago"),
        ("2026-10-06T11:57:00+00:00", "3 min ago"),
        ("2026-10-06T09:00:00+00:00", "3 h ago"),
        ("2026-10-01T12:00:00+00:00", "5 days ago"),
        ("2026-10-06T14:00:00+00:00", "in 2 h"),
        ("nonsense", ""),
        (None, ""),
    ],
)
def test_a_time_is_also_said_relative_to_now(value: object, expected: str) -> None:
    assert display.ago(value, now=NOW) == expected


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        ("2026-10-06T10:00:00+00:00", "2026-10-06T10:00:42+00:00", "42 s"),
        ("2026-10-06T10:00:00+00:00", "2026-10-06T10:02:10+00:00", "2 min 10 s"),
        ("2026-10-06T10:00:00+00:00", "2026-10-06T11:05:00+00:00", "1 h 05 min"),
        ("2026-10-06T11:58:00+00:00", None, "2 min 00 s"),
        ("nonsense", None, ""),
    ],
)
def test_how_long_a_run_took_or_has_been_working(
    start: str, end: str | None, expected: str
) -> None:
    assert display.duration(start, end, now=NOW) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(812, "812 B"), (1536, "1.5 KB"), (3 * 1024 * 1024, "3.0 MB"), (None, ""), (True, "")],
)
def test_sizes(value: object, expected: str) -> None:
    assert display.size_text(value) == expected


def test_a_short_id() -> None:
    assert display.short_id("0123456789abcdef") == "01234567"
    assert display.short_id(None) == ""


@pytest.mark.parametrize(
    ("first", "last", "tz", "expected"),
    [
        # A whole day in Berlin starts two hours before UTC midnight in summer time ...
        (date(2026, 10, 3), date(2026, 10, 3), "Europe/Berlin",
         ("2026-10-02T22:00:00+00:00", "2026-10-03T22:00:00+00:00")),
        # ... and one hour before in winter time.
        (date(2026, 12, 1), date(2026, 12, 1), "Europe/Berlin",
         ("2026-11-30T23:00:00+00:00", "2026-12-01T23:00:00+00:00")),
        # The day the clocks go forward is 23 hours long, and the one they go back 25.
        (date(2026, 3, 29), date(2026, 3, 29), "Europe/Berlin",
         ("2026-03-28T23:00:00+00:00", "2026-03-29T22:00:00+00:00")),
        (date(2026, 10, 25), date(2026, 10, 25), "Europe/Berlin",
         ("2026-10-24T22:00:00+00:00", "2026-10-25T23:00:00+00:00")),
        # Both days are included, so the end is the start of the day after the last one.
        (date(2026, 10, 1), date(2026, 10, 31), "UTC",
         ("2026-10-01T00:00:00+00:00", "2026-11-01T00:00:00+00:00")),
        # A missing day leaves that side open.
        (None, date(2026, 10, 3), "UTC", (None, "2026-10-04T00:00:00+00:00")),
        (date(2026, 10, 3), None, "UTC", ("2026-10-03T00:00:00+00:00", None)),
        (None, None, "UTC", (None, None)),
        # A zone nobody knows is read as UTC rather than failing the page.
        (date(2026, 10, 3), date(2026, 10, 3), "Mars/Olympus",
         ("2026-10-03T00:00:00+00:00", "2026-10-04T00:00:00+00:00")),
    ],
)
def test_whole_days_become_the_instants_the_ingester_compares(
    first: date | None, last: date | None, tz: str, expected: tuple[str | None, str | None]
) -> None:
    assert display.day_bounds(first, last, tz) == expected
