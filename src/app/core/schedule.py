"""The backup schedule: its file, its validation and the arithmetic on it.

The schedule is one small document, `$PAPAIA_CONFIG_DIR/manager/schedule.yaml`.
It is the only source of truth: the scheduler keeps its jobs in memory and is
rebuilt from this file, so a schedule that was removed cannot come back from some
second store after a restart.

Nothing here starts a backup or talks to the scheduler. This module answers
questions -- is this expression acceptable, when does it fire next, how long may
the newest restore point be before the schedule counts as overdue, may a
retention be applied on this run -- and `app.core.scheduler` acts on the answers.

Cron expressions are *standard* cron, not APScheduler's dialect. APScheduler 3.x
numbers weekdays from Monday (so `0 3 * * 1` fires on Tuesday) and ANDs a
restricted day-of-month with a restricted weekday where cron ORs them. Both are
surprises for someone who types an expression they know from a crontab, so the
expression is normalised here: weekdays become names, and the one combination
whose meaning differs is refused.
"""
from __future__ import annotations

import logging
import math
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.core import backups

logger = logging.getLogger(__name__)

SCHEDULE_NAME = "schedule.yaml"
DEFAULT_CRON = "0 3 * * *"

# Backups pause containers while their volumes are archived. Runs closer together
# than this would keep the stack in a permanent state of being backed up.
MIN_INTERVAL = timedelta(hours=1)

# A schedule counts as overdue once the newest successful restore point is older
# than this many times the longest gap between two runs. One missed slot is
# normal (the manager was restarting); a second one is a problem worth a colour.
OVERDUE_FACTOR = 1.5

# How far ahead the gaps of an expression are sampled. Two years is enough to see
# two runs of a yearly expression; the sample cap keeps an every-hour expression
# from walking the whole horizon.
_CADENCE_HORIZON = timedelta(days=800)
_CADENCE_SAMPLES = 2000

WEEKDAYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAY_LABELS: dict[str, str] = {
    "mon": "Monday",
    "tue": "Tuesday",
    "wed": "Wednesday",
    "thu": "Thursday",
    "fri": "Friday",
    "sat": "Saturday",
    "sun": "Sunday",
}
# Offered by the hourly preset. Divisors of 24, so the runs are evenly spaced
# across midnight too.
HOURLY_CHOICES: tuple[int, ...] = (1, 2, 3, 4, 6, 8, 12)

# Offered as a datalist next to the timezone field; any IANA name is accepted.
COMMON_TIMEZONES: tuple[str, ...] = (
    "UTC",
    "Europe/Berlin",
    "Europe/London",
    "Europe/Paris",
    "Europe/Madrid",
    "Europe/Rome",
    "Europe/Warsaw",
    "Europe/Athens",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/Sao_Paulo",
    "Asia/Dubai",
    "Asia/Kolkata",
    "Asia/Singapore",
    "Asia/Shanghai",
    "Asia/Tokyo",
    "Australia/Sydney",
)

# Standard cron numbers weekdays from Sunday (0 or 7).
_WEEKDAY_NUMBER = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
_PLAIN_FIELD = re.compile(r"^[0-9*/,\-]+$")
_TZ_NAME = re.compile(r"^[A-Za-z0-9_+\-]+(/[A-Za-z0-9_+\-]+){0,2}$")
_TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class ScheduleError(ValueError):
    """An expression, timezone or combination of settings that cannot be scheduled."""


# ---------------------------------------------------------------------------
# Timezone
# ---------------------------------------------------------------------------


def zone(name: str) -> ZoneInfo:
    """The named IANA zone, or `ScheduleError` for anything else.

    The name is shaped before it reaches `ZoneInfo`, which resolves it against the
    filesystem: a value like `../etc/passwd` is refused here rather than trusted
    to the library's own checks.
    """
    if not _TZ_NAME.match(name):
        raise ScheduleError(
            f"Unknown timezone {name!r}. Use an IANA name such as Europe/Berlin or UTC."
        )
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ScheduleError(
            f"Unknown timezone {name!r}. Use an IANA name such as Europe/Berlin or UTC."
        ) from exc


def default_timezone() -> str:
    """The container's `TZ` when it names an IANA zone, otherwise UTC."""
    name = os.environ.get("TZ", "").strip()
    if name:
        try:
            zone(name)
        except ScheduleError:
            return "UTC"
        return name
    return "UTC"


# ---------------------------------------------------------------------------
# Cron expressions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CronFields:
    """A validated expression, in the form APScheduler's `CronTrigger` takes."""

    minute: str
    hour: str
    day: str
    month: str
    day_of_week: str

    @property
    def expression(self) -> str:
        return f"{self.minute} {self.hour} {self.day} {self.month} {self.day_of_week}"


def _weekday_number(token: str) -> int:
    lowered = token.lower()
    if lowered in _WEEKDAY_NUMBER:
        return _WEEKDAY_NUMBER[lowered]
    if token.isdigit() and 0 <= int(token) <= 7:
        return int(token)
    raise ScheduleError(
        f"Unknown weekday {token!r}. Use mon-sun or the numbers 0-7 (0 and 7 are Sunday)."
    )


def _normalise_weekdays(field: str) -> str:
    """Standard-cron weekdays -> APScheduler weekday names, or `*`.

    Spelled out as names because the numbers mean different days in the two
    dialects, and a name cannot be misread by either.
    """
    if field == "*":
        return "*"
    chosen: set[int] = set()
    for part in field.split(","):
        body, slash, raw_step = part.partition("/")
        step = 1
        if slash:
            if not raw_step.isdigit() or int(raw_step) < 1:
                raise ScheduleError(f"The step in {part!r} must be a whole number of at least 1.")
            step = int(raw_step)
        if body == "*":
            low, high = 0, 6
        elif "-" in body:
            first, _, last = body.partition("-")
            low, high = _weekday_number(first), _weekday_number(last)
            if high == 0 and low > 0:
                high = 7  # mon-sun
            if low > high:
                raise ScheduleError(f"The weekday range {body!r} runs backwards.")
        else:
            if slash:
                raise ScheduleError(f"{part!r}: a step needs '*' or a range in front of it.")
            low = high = _weekday_number(body)
        chosen.update(day % 7 for day in range(low, high + 1, step))
    if len(chosen) == 7:
        return "*"
    return ",".join(name for name in WEEKDAYS if _WEEKDAY_NUMBER[name] in chosen)


def parse_fields(expression: str) -> CronFields:
    """Validate an expression and normalise it, or raise `ScheduleError`."""
    parts = expression.split()
    if len(parts) != 5:
        raise ScheduleError(
            "A cron expression has five fields: minute hour day-of-month month day-of-week."
        )
    labels = ("minute", "hour", "day of the month", "month")
    for part, label in zip(parts[:4], labels, strict=True):
        if not _PLAIN_FIELD.match(part):
            raise ScheduleError(
                f"The {label} field {part!r} may only contain digits, '*', ',', '-' and '/'."
            )
    fields = CronFields(parts[0], parts[1], parts[2], parts[3], _normalise_weekdays(parts[4]))
    if fields.day != "*" and fields.day_of_week != "*":
        raise ScheduleError(
            "Restrict either the day of the month or the day of the week, not both: "
            "cron runs on either, which is rarely what is meant."
        )
    _trigger(fields, UTC)  # the range checks are APScheduler's
    return fields


def _trigger(fields: CronFields, tz: tzinfo) -> CronTrigger:
    try:
        return CronTrigger(
            minute=fields.minute,
            hour=fields.hour,
            day=fields.day,
            month=fields.month,
            day_of_week=fields.day_of_week,
            timezone=tz,
        )
    except (ValueError, TypeError) as exc:
        raise ScheduleError(f"Invalid cron expression: {exc}") from exc


def build_trigger(expression: str, timezone: str) -> CronTrigger:
    """The trigger for an expression in a zone."""
    return _trigger(parse_fields(expression), zone(timezone))


def next_runs(
    expression: str, timezone: str, *, count: int = 3, now: datetime | None = None
) -> list[datetime]:
    """The next `count` runs, as real instants shown in the schedule's zone.

    A time that does not exist on the day the clocks spring forward is reported
    where it actually happens (02:30 becomes 03:30), not under the wall-clock
    reading APScheduler hands back for it.
    """
    tz = zone(timezone)
    trigger = _trigger(parse_fields(expression), tz)
    current = (now or datetime.now(UTC)).astimezone(tz)
    runs: list[datetime] = []
    previous: datetime | None = None
    while len(runs) < count:
        upcoming = trigger.get_next_fire_time(previous, current)
        if upcoming is None:
            break
        runs.append(upcoming.astimezone(UTC).astimezone(tz))
        previous = upcoming
        current = upcoming + timedelta(seconds=1)
    return runs


@dataclass(frozen=True)
class Cadence:
    """The shortest and the longest gap between two consecutive runs."""

    shortest: timedelta
    longest: timedelta


def analyse_cadence(expression: str, timezone: str, *, now: datetime | None = None) -> Cadence:
    """Sample the expression and measure the gaps between its runs.

    Gaps are wall-clock: two runs at 03:00 on either side of a clock change are
    24 hours apart, as the person who wrote "daily" means it, not 23 or 25. The
    subtraction of two datetimes that share a zone does exactly that.
    """
    tz = zone(timezone)
    trigger = _trigger(parse_fields(expression), tz)
    moment = (now or datetime.now(UTC)).astimezone(tz)
    horizon = moment + _CADENCE_HORIZON
    runs: list[datetime] = []
    previous: datetime | None = None
    current = moment
    while len(runs) < _CADENCE_SAMPLES:
        upcoming = trigger.get_next_fire_time(previous, current)
        if upcoming is None or upcoming > horizon:
            break
        runs.append(upcoming)
        previous = upcoming
        current = upcoming + timedelta(seconds=1)
    if len(runs) < 2:
        raise ScheduleError("This expression runs less than twice in two years.")
    gaps = [later - earlier for earlier, later in zip(runs, runs[1:], strict=False)]
    return Cadence(shortest=min(gaps), longest=max(gaps))


@lru_cache(maxsize=64)
def cadence_of(expression: str, timezone: str) -> Cadence:
    """`analyse_cadence` for the real clock, remembered for the process."""
    return analyse_cadence(expression, timezone)


def min_retention_days(cadence: Cadence) -> int:
    """The shortest retention that still keeps two runs: twice the longest gap."""
    return max(1, math.ceil(2 * cadence.longest / timedelta(days=1)))


# ---------------------------------------------------------------------------
# Presets: what the editor offers, compiled to and read back from cron
# ---------------------------------------------------------------------------

PresetMode = Literal["daily", "weekly", "hourly", "custom"]


@dataclass(frozen=True)
class Preset:
    mode: PresetMode
    time: str = "03:00"
    weekdays: tuple[str, ...] = ()
    every_hours: int = 6
    cron: str = ""


def _clock(value: str) -> tuple[int, int]:
    match = _TIME.match(value.strip())
    if match is None:
        raise ScheduleError("The time must look like 03:00.")
    return int(match.group(1)), int(match.group(2))


def compile_preset(
    mode: str,
    *,
    time: str = "03:00",
    weekdays: Iterable[str] = (),
    every_hours: int = 6,
    cron: str = "",
) -> str:
    """The cron expression a preset stands for."""
    if mode == "custom":
        return " ".join(cron.split())
    hour, minute = _clock(time)
    if mode == "daily":
        return f"{minute} {hour} * * *"
    if mode == "weekly":
        picked = {day.lower() for day in weekdays}
        unknown = picked - set(WEEKDAYS)
        if unknown:
            raise ScheduleError(f"Unknown weekday {sorted(unknown)[0]!r}.")
        if not picked:
            raise ScheduleError("Pick at least one day of the week.")
        names = ",".join(day for day in WEEKDAYS if day in picked)
        return f"{minute} {hour} * * {'*' if len(picked) == 7 else names}"
    if mode == "hourly":
        if every_hours not in HOURLY_CHOICES:
            raise ScheduleError(
                "Every N hours takes one of " + ", ".join(str(n) for n in HOURLY_CHOICES) + "."
            )
        return f"{minute} {'*' if every_hours == 1 else f'*/{every_hours}'} * * *"
    raise ScheduleError(f"Unknown schedule type {mode!r}.")


def read_preset(expression: str) -> Preset:
    """The preset an expression corresponds to, or a custom one."""
    try:
        fields = parse_fields(expression)
    except ScheduleError:
        return Preset(mode="custom", cron=expression)
    plain = fields.day == "*" and fields.month == "*"
    one_minute = fields.minute.isdigit()
    if plain and one_minute and fields.hour.isdigit():
        time = f"{int(fields.hour):02d}:{int(fields.minute):02d}"
        if fields.day_of_week == "*":
            return Preset(mode="daily", time=time)
        return Preset(mode="weekly", time=time, weekdays=tuple(fields.day_of_week.split(",")))
    if plain and one_minute and fields.day_of_week == "*":
        time = f"00:{int(fields.minute):02d}"
        if fields.hour == "*":
            return Preset(mode="hourly", time=time, every_hours=1)
        step = re.fullmatch(r"\*/(\d+)", fields.hour)
        if step and int(step.group(1)) in HOURLY_CHOICES:
            return Preset(mode="hourly", time=time, every_hours=int(step.group(1)))
    return Preset(mode="custom", cron=fields.expression)


def describe(expression: str) -> str:
    """A sentence for an expression: "Every day at 03:00"."""
    preset = read_preset(expression)
    if preset.mode == "daily":
        return f"Every day at {preset.time}"
    if preset.mode == "weekly":
        days = ", ".join(WEEKDAY_LABELS[day] for day in preset.weekdays)
        return f"Every {days} at {preset.time}"
    if preset.mode == "hourly":
        minute = preset.time[3:]
        if preset.every_hours == 1:
            return f"Every hour at :{minute}"
        return f"Every {preset.every_hours} hours at :{minute}"
    return f"Custom schedule ({preset.cron})"


# ---------------------------------------------------------------------------
# The schedule itself
# ---------------------------------------------------------------------------


class BackupSchedule(BaseModel):
    """What `schedule.yaml` holds. Strict: an unknown key is an error, not ignored."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    cron: str = DEFAULT_CRON
    timezone: str = Field(default_factory=default_timezone)
    # None keeps every restore point. Otherwise handed to `papaia-ctl backup` as
    # --retention-period-days, subject to the guard in `plan_retention`.
    retention_days: int | None = Field(default=None, ge=1, le=3650)
    # How late a run may start and still count. A manager that was busy or paused
    # for a few minutes should still take the backup.
    misfire_grace_seconds: int = Field(default=3600, ge=60, le=86_400)
    run_on_startup: Literal["never", "if_missed"] = "if_missed"

    @field_validator("cron")
    @classmethod
    def _normalise_cron(cls, value: str) -> str:
        return parse_fields(value).expression

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        zone(value)
        return value

    @model_validator(mode="after")
    def _check_cadence(self) -> BackupSchedule:
        cadence = cadence_of(self.cron, self.timezone)
        if cadence.shortest < MIN_INTERVAL:
            raise ScheduleError(
                "Backups may not run more often than once an hour; this schedule has "
                f"runs {humanize(cadence.shortest)} apart."
            )
        if self.retention_days is not None:
            floor = min_retention_days(cadence)
            if self.retention_days < floor:
                raise ScheduleError(
                    f"The retention must be at least {floor} days for this schedule: "
                    "twice the longest gap between two runs, so that at least two "
                    "restore points are kept."
                )
        return self


def build_schedule(**fields: Any) -> BackupSchedule:
    """Validate submitted fields, with the first problem as a plain sentence."""
    try:
        return BackupSchedule(**fields)
    except ValidationError as exc:
        raise ScheduleError(_first_error(exc)) from exc


def _first_error(exc: ValidationError) -> str:
    error = exc.errors()[0]
    message = str(error["msg"]).removeprefix("Value error, ")
    location = ".".join(str(part) for part in error["loc"])
    return f"{location}: {message}" if location else message


def schedule_path(config_dir: str) -> Path:
    return Path(config_dir) / "manager" / SCHEDULE_NAME


@dataclass(frozen=True)
class ScheduleLoad:
    """The file's content, or why there is none to use.

    `schedule` is None both for "no schedule" (`error` is None too) and for a file
    that is there but unusable. The two are told apart because the second one means
    nothing is scheduled while the operator believes otherwise, and the page says so.
    """

    schedule: BackupSchedule | None
    error: str | None = None


def load_schedule(config_dir: str) -> ScheduleLoad:
    """Read the schedule. Never raises."""
    path = schedule_path(config_dir)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ScheduleLoad(None)
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("%s cannot be read: %s", path, exc)
        return ScheduleLoad(None, f"{SCHEDULE_NAME} cannot be read: {exc}")
    if raw is None:
        return ScheduleLoad(None)
    if not isinstance(raw, dict):
        logger.warning("%s does not contain a mapping", path)
        return ScheduleLoad(None, f"{SCHEDULE_NAME} does not contain a mapping.")
    try:
        return ScheduleLoad(build_schedule(**raw))
    except ScheduleError as exc:
        logger.warning("%s is invalid: %s", path, exc)
        return ScheduleLoad(None, f"{SCHEDULE_NAME} is invalid: {exc}")


def save_schedule(config_dir: str, schedule: BackupSchedule) -> None:
    """Atomically write schedule.yaml."""
    path = schedule_path(config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(
        "# papaia-manager backup schedule. Edited through the Backup page.\n"
        + yaml.dump(
            schedule.model_dump(mode="json"),
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    tmp.replace(path)


def delete_schedule(config_dir: str) -> bool:
    """Remove schedule.yaml. True if there was a file to remove."""
    path = schedule_path(config_dir)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


# ---------------------------------------------------------------------------
# What the page and the scheduler ask about the schedule and the catalogue
# ---------------------------------------------------------------------------


def humanize(span: timedelta) -> str:
    """`1 day 16 hours`, `5 hours 10 minutes`, `12 minutes`: two units, rounded down.

    A single unit is not enough where two spans are compared in one sentence:
    "40 hours old (more than 36 hours)" must not read "1 day (more than 1 day)".
    From a week on the day count is precise enough and the rest is left out.
    """
    seconds = max(0, int(span.total_seconds()))
    days, rest = divmod(seconds, 86_400)
    hours, rest = divmod(rest, 3_600)
    minutes = rest // 60

    def unit(count: int, name: str) -> str:
        return f"{count} {name}{'' if count == 1 else 's'}"

    if days:
        if days >= 7 or not hours:
            return unit(days, "day")
        return f"{unit(days, 'day')} {unit(hours, 'hour')}"
    if hours:
        if not minutes:
            return unit(hours, "hour")
        return f"{unit(hours, 'hour')} {unit(minutes, 'minute')}"
    if minutes:
        return unit(minutes, "minute")
    return "under a minute"


def overdue_after(schedule: BackupSchedule) -> timedelta:
    """How old the newest successful restore point may get before it is a problem."""
    return cadence_of(schedule.cron, schedule.timezone).longest * OVERDUE_FACTOR


def catchup_due(
    schedule: BackupSchedule, points: Iterable[backups.RestorePoint], *, now: datetime
) -> bool:
    """True if a run was missed: no successful backup, or one past the overdue limit."""
    newest = backups.newest_successful(points)
    taken = backups.restore_point_time(newest) if newest else None
    return taken is None or now - taken > overdue_after(schedule)


@dataclass(frozen=True)
class RetentionPlan:
    """What a run does about the retention.

    `days` is what to hand `papaia-ctl backup`, None meaning no flag. `skipped` is
    set when a retention is configured and not applied this time, and says why.
    """

    days: int | None
    skipped: str | None = None


def plan_retention(
    schedule: BackupSchedule, points: Iterable[backups.RestorePoint], *, now: datetime
) -> RetentionPlan:
    """Apply the retention only while the catalogue still has a recent good restore point.

    `papaia-ctl backup` prunes after every run, a failed one included, and does not
    keep a last usable restore point. A long series of failed backups would
    therefore age out every good one. Holding the retention back while the newest
    successful restore point is overdue means such a series ends the pruning
    instead of finishing the catalogue off; it resumes after the next good run.
    """
    if schedule.retention_days is None:
        return RetentionPlan(None)
    newest = backups.newest_successful(points)
    taken = backups.restore_point_time(newest) if newest else None
    if taken is None:
        return RetentionPlan(None, "there is no successful restore point yet")
    limit = overdue_after(schedule)
    age = now - taken
    if age > limit:
        return RetentionPlan(
            None,
            f"the newest successful restore point is {humanize(age)} old "
            f"(more than {humanize(limit)})",
        )
    return RetentionPlan(schedule.retention_days)


def format_moment(moment: datetime, timezone: str) -> str:
    """`Sat 2026-10-03 03:00 CEST`: an instant, read in the schedule's own zone."""
    return moment.astimezone(zone(timezone)).strftime("%a %Y-%m-%d %H:%M %Z")


@dataclass(frozen=True)
class ScheduleStatus:
    """The numbers behind the status strip."""

    last_ok_at: datetime | None
    age: timedelta | None
    latest_result: str
    overdue_after: timedelta | None
    overdue: bool
    next_run: datetime | None


def evaluate(
    schedule: BackupSchedule | None,
    points: list[backups.RestorePoint],
    *,
    now: datetime,
    next_run: datetime | None,
) -> ScheduleStatus:
    """Age of the newest successful restore point, and whether that is a problem.

    Only a schedule can make it a problem: without one, the age is shown and never
    coloured, because nobody promised a backup.
    """
    newest_ok = backups.newest_successful(points)
    last_ok_at = backups.restore_point_time(newest_ok) if newest_ok else None
    age = now - last_ok_at if last_ok_at is not None else None
    latest = max(
        points,
        key=lambda p: backups.restore_point_time(p) or datetime.min.replace(tzinfo=UTC),
        default=None,
    )
    limit = overdue_after(schedule) if schedule is not None and schedule.enabled else None
    overdue = False
    if limit is not None:
        # An empty catalogue is a fresh installation waiting for its first run, not
        # an overdue one. Restore points that never succeeded are a problem.
        overdue = bool(points) if age is None else age > limit
    return ScheduleStatus(
        last_ok_at=last_ok_at,
        age=age,
        latest_result=latest.result if latest is not None else "",
        overdue_after=limit,
        overdue=overdue,
        next_run=next_run,
    )


@dataclass(frozen=True)
class ScheduleState:
    """Everything the Backup page and the API say about the schedule, in one read.

    Times are already formatted in the schedule's zone (or the default one without
    a schedule), so a template and a JSON consumer cannot disagree about them.
    """

    load: ScheduleLoad
    status: ScheduleStatus
    timezone: str
    description: str
    preset: Preset | None
    next_run_text: str | None
    next_runs_text: list[str]
    last_ok_text: str | None
    age_text: str | None
    overdue_after_text: str | None
    # Restore points the next run's retention would delete, when one is configured.
    would_delete: int | None
    retention_note: str | None
    backup_dir_reachable: bool

    @property
    def schedule(self) -> BackupSchedule | None:
        return self.load.schedule


def read_state(
    config_dir: str, *, next_run: datetime | None, now: datetime | None = None
) -> ScheduleState:
    """Read the schedule and the catalogue and say where they stand.

    `next_run` comes from the live scheduler, not from the expression: the page
    says when the next run *will* happen, and that is None when the scheduler is
    not running even though a schedule is on disk.
    """
    moment = now or datetime.now(tz=UTC)
    load = load_schedule(config_dir)
    current = load.schedule
    backup_dir = backups.resolve_backup_dir(config_dir)
    points = backups.load_restore_points(backup_dir)
    tz = current.timezone if current is not None else default_timezone()
    status = evaluate(current, points, now=moment, next_run=next_run)

    would_delete: int | None = None
    retention_note: str | None = None
    if current is not None and current.retention_days is not None:
        would_delete = backups.count_older_than(points, current.retention_days, now=moment)
        plan = plan_retention(current, points, now=moment)
        if plan.skipped:
            retention_note = f"Not applied at the moment: {plan.skipped}."

    return ScheduleState(
        load=load,
        status=status,
        timezone=tz,
        description=describe(current.cron) if current is not None else "",
        preset=read_preset(current.cron) if current is not None else None,
        next_run_text=format_moment(next_run, tz) if next_run is not None else None,
        next_runs_text=(
            [format_moment(r, tz) for r in next_runs(current.cron, tz, now=moment)]
            if current is not None and current.enabled
            else []
        ),
        last_ok_text=(
            format_moment(status.last_ok_at, tz) if status.last_ok_at is not None else None
        ),
        age_text=humanize(status.age) if status.age is not None else None,
        overdue_after_text=(
            humanize(status.overdue_after) if status.overdue_after is not None else None
        ),
        would_delete=would_delete,
        retention_note=retention_note,
        backup_dir_reachable=backups.is_reachable(backup_dir),
    )


def state_to_dict(state: ScheduleState) -> dict[str, Any]:
    """The JSON shape of `read_state`."""
    current = state.schedule
    preset = state.preset
    return {
        "configured": current is not None,
        "error": state.load.error,
        "schedule": current.model_dump(mode="json") if current is not None else None,
        "description": state.description,
        "preset": (
            {
                "mode": preset.mode,
                "time": preset.time,
                "weekdays": list(preset.weekdays),
                "every_hours": preset.every_hours,
                "cron": preset.cron,
            }
            if preset is not None
            else None
        ),
        "timezone": state.timezone,
        "next_run": state.next_run_text,
        "next_runs": state.next_runs_text,
        "last_successful_backup": state.last_ok_text,
        "age": state.age_text,
        "overdue": state.status.overdue,
        "overdue_after": state.overdue_after_text,
        "latest_result": state.status.latest_result,
        "retention": {
            "would_delete": state.would_delete,
            "note": state.retention_note,
        },
        "backup_dir_reachable": state.backup_dir_reachable,
    }
