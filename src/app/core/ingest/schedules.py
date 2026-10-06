"""The schedule of an ingest job: what the builder offers, what `jobs.yaml` holds, what it means.

The ingester runs a job on `schedule.cron` (APScheduler's `CronTrigger.from_crontab`) or on
`schedule.every` (`30s`, `15m`, `4h`, `1d`), or only on request when neither is set. This
module turns the builder's choices into one of those and back, and says in words what a
stored schedule does.

Three things here are decided by the ingester and not by the manager, and each is easy to
get wrong:

* **The weekday numbers.** `from_crontab` counts from Monday (`0` is Monday, `6` is Sunday,
  `7` is refused), where standard cron counts from Sunday. An expression written for one
  fires a day off under the other. So this module *writes* weekday names, which both read the
  same way, and when it *reads* a number from a file it reads it the way the ingester does.
  `core/schedule.py` (the backup schedule) deliberately counts the standard way and is not
  used for this.
* **The trigger is the ingester's.** Validation and the next runs come from the same
  APScheduler calls the ingester makes (`from_crontab`, `IntervalTrigger`), not from a
  re-implementation, so what the preview shows is what will run.
* **The default zone is the ingester's**, not the manager container's: the job's
  `timezone`, else `defaults.schedule.timezone`, else the ingester's `QI_TIMEZONE`, else UTC.

An interval counts from the moment the ingester loaded the job. A preview of an interval can
therefore say how often, but not at what time.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from apscheduler.triggers.cron import CronTrigger

from app.core.schedule import COMMON_TIMEZONES, WEEKDAY_LABELS, ScheduleError, zone

# APScheduler's order: index = the number its from_crontab gives the day.
INGEST_WEEKDAYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

EVERY_UNITS: dict[str, tuple[str, int]] = {
    "s": ("second", 1),
    "m": ("minute", 60),
    "h": ("hour", 3600),
    "d": ("day", 86400),
}
STARTUP_POLICIES: tuple[str, ...] = ("never", "if_missed", "always")

_EVERY_RE = re.compile(r"^(\d+)(s|m|h|d)$")
_INT = re.compile(r"^\d+$")
_TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

Mode = Literal["manual", "interval", "hourly", "daily", "weekly", "monthly", "cron"]
MODES: tuple[str, ...] = ("manual", "interval", "hourly", "daily", "weekly", "monthly", "cron")

__all__ = [
    "COMMON_TIMEZONES",
    "INGEST_WEEKDAYS",
    "MODES",
    "Compiled",
    "Plan",
    "ScheduleError",
    "compile_plan",
    "cron_error",
    "describe",
    "every_error",
    "normalise_cron",
    "preview",
    "read_plan",
]


@dataclass(frozen=True)
class Plan:
    """What the builder holds. Every field is a control; `compile_plan` is the only judge."""

    mode: Mode = "manual"
    every_n: int = 15
    every_unit: str = "m"
    time: str = "03:00"
    minute: int = 0
    weekdays: tuple[str, ...] = ()
    day_of_month: int = 1
    cron: str = ""
    # Not part of the choice of when, kept with it because they live in the same block.
    timezone: str | None = None
    jitter_seconds: int = 30
    misfire_grace_seconds: int = 300
    run_on_startup: str = "if_missed"
    # Keys of the stored block this module does not model, carried over untouched.
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Compiled:
    """The two keys of `schedule:` that say when. At most one is set."""

    cron: str | None = None
    every: str | None = None


# ---------------------------------------------------------------------------
# Reading numbers the way the ingester does
# ---------------------------------------------------------------------------


def _day_name(token: str) -> str | None:
    """A weekday token as a name, or None if it is not a plain day.

    Names pass through; `0`..`6` are read as the ingester reads them (`0` is Monday).
    """
    lowered = token.lower()
    if lowered in INGEST_WEEKDAYS:
        return lowered
    if _INT.match(token) and 0 <= int(token) <= 6:
        return INGEST_WEEKDAYS[int(token)]
    return None


def normalise_cron(expression: str) -> str:
    """The expression with its weekday field spelled in names, when that can be done exactly.

    A plain list of days and a plain range (`1-5`, `mon-fri`, `0,2,4`) become names, which
    means the same under the ingester and under standard cron. Anything else (a step, a
    mix) is left as typed, because rewriting it could change its meaning.
    """
    parts = expression.split()
    if len(parts) != 5:
        return " ".join(parts)
    dow = parts[4]
    names: list[str] = []
    ok = True
    for chunk in dow.split(","):
        if "-" in chunk and "/" not in chunk:
            first, _, last = chunk.partition("-")
            low, high = _day_name(first), _day_name(last)
            if low is None or high is None:
                ok = False
                break
            span = INGEST_WEEKDAYS.index(low), INGEST_WEEKDAYS.index(high)
            if span[0] > span[1]:
                ok = False
                break
            names.extend(INGEST_WEEKDAYS[span[0] : span[1] + 1])
        else:
            name = _day_name(chunk)
            if name is None:
                ok = False
                break
            names.append(name)
    if ok and dow != "*":
        ordered = [day for day in INGEST_WEEKDAYS if day in names]
        parts[4] = ",".join(ordered)
    return " ".join(parts)


def cron_error(expression: str, timezone: str = "UTC") -> str | None:
    """Why the ingester would refuse this cron expression, or None."""
    try:
        CronTrigger.from_crontab(expression, timezone=zone(timezone))
    except ScheduleError as exc:
        return str(exc)
    except (ValueError, TypeError) as exc:
        return f"invalid cron expression: {exc}"
    return None


def every_error(value: str) -> str | None:
    match = _EVERY_RE.match(value)
    if match is None:
        return "an interval looks like 30s, 15m, 4h or 1d"
    if int(match.group(1)) < 1:
        return "an interval has to be at least 1"
    return None


# ---------------------------------------------------------------------------
# Builder choices <-> the stored block
# ---------------------------------------------------------------------------

_MODELLED = {
    "cron",
    "every",
    "timezone",
    "jitter_seconds",
    "misfire_grace_seconds",
    "run_on_startup",
}


def read_plan(schedule: Mapping[str, Any] | None) -> Plan:
    """The builder's state for a stored `schedule:` block. Nothing is ever lost.

    A cron expression that none of the presets can express (several hours, a step in the
    day, a month) opens as `cron`, so it is shown and saved exactly as it is.
    """
    block = dict(schedule or {})
    common: dict[str, Any] = {
        "timezone": _text(block.get("timezone")),
        "jitter_seconds": _int(block.get("jitter_seconds"), 30),
        "misfire_grace_seconds": _int(block.get("misfire_grace_seconds"), 300),
        "run_on_startup": str(block.get("run_on_startup") or "if_missed"),
        "extra": {key: value for key, value in block.items() if key not in _MODELLED},
    }
    every = _text(block.get("every"))
    cron = _text(block.get("cron"))
    if every:
        match = _EVERY_RE.match(every)
        if match is None:
            return Plan(mode="cron", cron=every, **common)
        return Plan(
            mode="interval", every_n=int(match.group(1)), every_unit=match.group(2), **common
        )
    if not cron:
        return Plan(mode="manual", **common)

    fields = cron.split()
    if len(fields) != 5:
        return Plan(mode="cron", cron=cron, **common)
    minute, hour, dom, month, dow = fields
    if month != "*" or not _INT.match(minute) or not 0 <= int(minute) <= 59:
        return Plan(mode="cron", cron=cron, **common)
    if hour == "*" and dom == "*" and dow == "*":
        return Plan(mode="hourly", minute=int(minute), **common)
    if not _INT.match(hour) or not 0 <= int(hour) <= 23:
        return Plan(mode="cron", cron=cron, **common)
    clock = f"{int(hour):02d}:{int(minute):02d}"
    if dom == "*" and dow == "*":
        return Plan(mode="daily", time=clock, **common)
    if dom == "*":
        days = [_day_name(chunk) for chunk in dow.split(",")]
        if all(days):
            chosen = tuple(day for day in INGEST_WEEKDAYS if day in days)
            return Plan(mode="weekly", time=clock, weekdays=chosen, **common)
    if dow == "*" and _INT.match(dom) and 1 <= int(dom) <= 31:
        return Plan(mode="monthly", time=clock, day_of_month=int(dom), **common)
    return Plan(mode="cron", cron=cron, **common)


def compile_plan(plan: Plan) -> Compiled:
    """The `cron` or `every` a plan stands for; `ScheduleError` for a plan that cannot run."""
    if plan.mode == "manual":
        return Compiled()
    if plan.mode == "interval":
        if plan.every_unit not in EVERY_UNITS:
            raise ScheduleError("The interval unit is seconds, minutes, hours or days.")
        if plan.every_n < 1:
            raise ScheduleError("An interval has to be at least 1.")
        return Compiled(every=f"{plan.every_n}{plan.every_unit}")
    if plan.mode == "hourly":
        if not 0 <= plan.minute <= 59:
            raise ScheduleError("The minute is between 0 and 59.")
        return Compiled(cron=f"{plan.minute} * * * *")
    if plan.mode == "cron":
        expression = normalise_cron(plan.cron)
        if not expression:
            raise ScheduleError("Enter a cron expression.")
        return Compiled(cron=expression)
    hour, minute = _clock(plan.time)
    if plan.mode == "daily":
        return Compiled(cron=f"{minute} {hour} * * *")
    if plan.mode == "weekly":
        picked = {day.lower() for day in plan.weekdays}
        unknown = picked - set(INGEST_WEEKDAYS)
        if unknown:
            raise ScheduleError(f"Unknown weekday {sorted(unknown)[0]!r}.")
        if not picked:
            raise ScheduleError("Pick at least one day of the week.")
        names = "*" if len(picked) == 7 else ",".join(d for d in INGEST_WEEKDAYS if d in picked)
        return Compiled(cron=f"{minute} {hour} * * {names}")
    if plan.mode == "monthly":
        if not 1 <= plan.day_of_month <= 31:
            raise ScheduleError("The day of the month is between 1 and 31.")
        return Compiled(cron=f"{minute} {hour} {plan.day_of_month} * *")
    raise ScheduleError(f"Unknown schedule type {plan.mode!r}.")


def schedule_block(plan: Plan, *, full: bool = False) -> dict[str, Any]:
    """The `schedule:` mapping for a plan.

    By default only what differs from the ingester's own defaults. With `full` every detail
    is spelled out, for a caller that knows the catalog's `defaults.schedule` and drops what
    the job would inherit anyway: a job that wants the ingester's default while the catalog
    says otherwise has to say so. Keys this module does not model are carried over.
    """
    compiled = compile_plan(plan)
    block: dict[str, Any] = dict(plan.extra)
    if compiled.cron is not None:
        block["cron"] = compiled.cron
    if compiled.every is not None:
        block["every"] = compiled.every
    if plan.timezone:
        block["timezone"] = plan.timezone
    if full or plan.run_on_startup != "if_missed":
        block["run_on_startup"] = plan.run_on_startup
    if full or plan.jitter_seconds != 30:
        block["jitter_seconds"] = plan.jitter_seconds
    if full or plan.misfire_grace_seconds != 300:
        block["misfire_grace_seconds"] = plan.misfire_grace_seconds
    return block


def _clock(value: str) -> tuple[int, int]:
    match = _TIME.match(value.strip())
    if match is None:
        raise ScheduleError("The time must look like 03:00.")
    return int(match.group(1)), int(match.group(2))


def _text(value: Any) -> str | None:
    return str(value).strip() if isinstance(value, str) and value.strip() else None


def _int(value: Any, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


# ---------------------------------------------------------------------------
# Saying it, and showing the next runs
# ---------------------------------------------------------------------------


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def describe(plan: Plan) -> str:
    """A sentence for a plan: "Every day at 03:00"."""
    if plan.mode == "manual":
        return "Only when started by hand"
    if plan.mode == "interval":
        noun = EVERY_UNITS.get(plan.every_unit, (plan.every_unit, 1))[0]
        return "Every " + (noun if plan.every_n == 1 else _plural(plan.every_n, noun))
    if plan.mode == "hourly":
        return f"Every hour at :{plan.minute:02d}"
    if plan.mode == "daily":
        return f"Every day at {plan.time}"
    if plan.mode == "weekly":
        days = ", ".join(WEEKDAY_LABELS[day] for day in plan.weekdays if day in WEEKDAY_LABELS)
        return f"Every {days or 'week'} at {plan.time}"
    if plan.mode == "monthly":
        return f"On day {plan.day_of_month} of every month at {plan.time}"
    return f"Custom schedule ({plan.cron})"


def describe_block(schedule: Mapping[str, Any] | None) -> str:
    return describe(read_plan(schedule))


@dataclass(frozen=True)
class Preview:
    ok: bool
    error: str | None = None
    description: str = ""
    cron: str | None = None
    every: str | None = None
    # Wall-clock readings in the schedule's zone, the first run first.
    runs: tuple[str, ...] = ()
    timezone: str = "UTC"
    # Set for an interval: it counts from the load of the job, so no clock time is promised.
    counts_from_load: bool = False
    notes: tuple[str, ...] = ()


def effective_timezone(
    plan_timezone: str | None, defaults_timezone: str | None, ingester_timezone: str
) -> str:
    return plan_timezone or defaults_timezone or ingester_timezone or "UTC"


def preview(
    plan: Plan,
    *,
    default_timezone: str = "UTC",
    count: int = 3,
    now: datetime | None = None,
) -> Preview:
    """What the plan compiles to and when it would run next, or why it cannot."""
    zone_name = plan.timezone or default_timezone or "UTC"
    try:
        tz = zone(zone_name)
        compiled = compile_plan(plan)
        if compiled.cron is not None:
            problem = cron_error(compiled.cron, zone_name)
            if problem:
                raise ScheduleError(problem[0].upper() + problem[1:] + ".")
    except ScheduleError as exc:
        return Preview(ok=False, error=str(exc), timezone=zone_name)

    description = describe(plan)
    notes: list[str] = []
    if plan.mode == "monthly" and plan.day_of_month > 28:
        notes.append(
            f"Months without a day {plan.day_of_month} are skipped, so it does not run every month."
        )
    if compiled.cron is not None and any(ch.isdigit() for ch in compiled.cron.split()[4]):
        # What normalise_cron could not spell as names: a number inside a step.
        notes.append(
            "Weekday numbers count from Monday here (0 is Monday, 1 is Tuesday), unlike "
            "standard cron. Names (mon-sun) are unambiguous."
        )
    current = (now or datetime.now(UTC)).astimezone(UTC)
    runs: list[str] = []
    if compiled.cron is not None:
        trigger = CronTrigger.from_crontab(compiled.cron, timezone=tz)
        previous: datetime | None = None
        while len(runs) < count:
            upcoming = trigger.get_next_fire_time(previous, current)
            if upcoming is None:
                break
            runs.append(upcoming.astimezone(tz).strftime("%a %d %b %Y, %H:%M"))
            previous = upcoming
            current = upcoming.astimezone(UTC) + timedelta(seconds=1)
    return Preview(
        ok=True,
        description=description,
        cron=compiled.cron,
        every=compiled.every,
        runs=tuple(runs),
        timezone=zone_name,
        counts_from_load=compiled.every is not None,
        notes=tuple(notes),
    )

