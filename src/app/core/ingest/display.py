"""Times, spans and sizes of the ingest pages, in words.

The ingester reports every time as an ISO-8601 string in UTC with microseconds, which is what
its own interface used to print. A person reading a run wants "today 14:03, 3 minutes ago" in
the zone the ingester schedules in, so the pages call these. None of them raises: a value that
cannot be read is shown as it came, because a page must not fail over a timestamp.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _zone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return UTC


def local_time(value: object, tz_name: str = "UTC", *, now: datetime | None = None) -> str:
    """`Today 14:03`, `Yesterday 09:30`, `Mon 05 Oct 14:03`, `05 Oct 2025 14:03`."""
    moment = parse_iso(value)
    if moment is None:
        return str(value) if value else ""
    zone = _zone(tz_name)
    shown = moment.astimezone(zone)
    today = (now or datetime.now(UTC)).astimezone(zone).date()
    clock = shown.strftime("%H:%M")
    if shown.date() == today:
        return f"Today {clock}"
    if shown.date() == today - timedelta(days=1):
        return f"Yesterday {clock}"
    if shown.date() == today + timedelta(days=1):
        return f"Tomorrow {clock}"
    if shown.year != today.year:
        return shown.strftime("%d %b %Y ") + clock
    return shown.strftime("%a %d %b ") + clock


def _span(seconds: float) -> str:
    seconds = abs(seconds)
    if seconds < 45:
        return "a moment"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min"
    hours = round(seconds / 3600)
    if hours < 48:
        return f"{hours} h"
    return f"{round(seconds / 86400)} days"


def ago(value: object, *, now: datetime | None = None) -> str:
    """`3 min ago`, `in 2 h`, `a moment ago`."""
    moment = parse_iso(value)
    if moment is None:
        return ""
    delta = (now or datetime.now(UTC)).astimezone(UTC) - moment.astimezone(UTC)
    seconds = delta.total_seconds()
    return f"{_span(seconds)} ago" if seconds >= 0 else f"in {_span(seconds)}"


def duration(start: object, end: object = None, *, now: datetime | None = None) -> str:
    """`2 min 10 s`, `1 h 05 min`, `42 s`; for a run still working, up to now."""
    began = parse_iso(start)
    if began is None:
        return ""
    finished = parse_iso(end) or (now or datetime.now(UTC))
    total = max(0, int((finished - began).total_seconds()))
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours} h {minutes:02d} min"
    if minutes:
        return f"{minutes} min {seconds:02d} s"
    return f"{seconds} s"


def size_text(value: object) -> str:
    """`812 B`, `12.4 KB`, `3.1 MB`."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return ""
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


def short_id(value: object) -> str:
    return str(value)[:8] if value else ""
