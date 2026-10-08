"""The schedule of an ingest job: the builder, the stored block and the ingester's dialect."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from apscheduler.triggers.cron import CronTrigger

from app.core.ingest import schedules
from app.core.ingest.schedules import Plan, ScheduleError

# A Monday, so a weekday that is off by one shows.
_NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def _fires(expression: str, count: int = 6) -> list[datetime]:
    """The ingester's own reading of an expression: `from_crontab`, weekday 0 = Monday."""
    trigger = CronTrigger.from_crontab(expression, timezone=UTC)
    out: list[datetime] = []
    previous: datetime | None = None
    current = _NOW
    while len(out) < count:
        upcoming = trigger.get_next_fire_time(previous, current)
        assert upcoming is not None
        out.append(upcoming)
        previous = upcoming
        current = upcoming.replace(second=1)
    return out


# ---------------------------------------------------------------------------
# Reading what is stored
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("block", "mode"),
    [
        (None, "manual"),
        ({}, "manual"),
        ({"every": "15m"}, "interval"),
        ({"cron": "30 * * * *"}, "hourly"),
        ({"cron": "0 3 * * *"}, "daily"),
        ({"cron": "0 3 * * mon,wed"}, "weekly"),
        ({"cron": "0 3 15 * *"}, "monthly"),
        ({"cron": "0 */4 * * *"}, "cron"),
        ({"cron": "0 3 * 6 *"}, "cron"),
        ({"cron": "0 3 1 * 1"}, "cron"),
        ({"every": "weekly"}, "cron"),
        ({"cron": "not a cron"}, "cron"),
    ],
)
def test_a_stored_schedule_opens_on_the_control_that_matches_it(
    block: dict[str, Any] | None, mode: str
) -> None:
    assert schedules.read_plan(block).mode == mode


def test_numbers_are_read_the_way_the_ingester_reads_them() -> None:
    # In the ingester 0 is Monday, so this file means Monday and Wednesday, not Sunday.
    plan = schedules.read_plan({"cron": "0 3 * * 0,2"})

    assert plan.mode == "weekly"
    assert plan.weekdays == ("mon", "wed")
    assert plan.time == "03:00"


def test_the_details_of_a_schedule_are_read_and_unknown_keys_survive() -> None:
    plan = schedules.read_plan(
        {
            "every": "6h",
            "timezone": "Europe/Berlin",
            "jitter_seconds": 5,
            "misfire_grace_seconds": 60,
            "run_on_startup": "never",
            "something_new": 1,
        }
    )

    assert (plan.every_n, plan.every_unit) == (6, "h")
    assert (plan.timezone, plan.jitter_seconds, plan.misfire_grace_seconds) == (
        "Europe/Berlin",
        5,
        60,
    )
    assert plan.run_on_startup == "never"
    block = schedules.schedule_block(plan)
    assert block == {
        "every": "6h",
        "timezone": "Europe/Berlin",
        "jitter_seconds": 5,
        "misfire_grace_seconds": 60,
        "run_on_startup": "never",
        "something_new": 1,
    }


def test_a_block_that_keeps_every_default_stays_short() -> None:
    plan = schedules.read_plan(
        {"cron": "0 3 * * *", "jitter_seconds": 30, "run_on_startup": "if_missed"}
    )

    assert schedules.schedule_block(plan) == {"cron": "0 3 * * *"}


# ---------------------------------------------------------------------------
# Writing it: names, never numbers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("plan", "expected"),
    [
        (Plan(mode="manual"), schedules.Compiled()),
        (Plan(mode="interval", every_n=15, every_unit="m"), schedules.Compiled(every="15m")),
        (Plan(mode="interval", every_n=1, every_unit="d"), schedules.Compiled(every="1d")),
        (Plan(mode="hourly", minute=5), schedules.Compiled(cron="5 * * * *")),
        (Plan(mode="daily", time="03:30"), schedules.Compiled(cron="30 3 * * *")),
        (
            Plan(mode="weekly", time="22:00", weekdays=("fri", "mon")),
            schedules.Compiled(cron="0 22 * * mon,fri"),
        ),
        (
            Plan(mode="weekly", time="22:00", weekdays=schedules.INGEST_WEEKDAYS),
            schedules.Compiled(cron="0 22 * * *"),
        ),
        (
            Plan(mode="monthly", time="01:15", day_of_month=3),
            schedules.Compiled(cron="15 1 3 * *"),
        ),
        (
            Plan(mode="cron", cron="  0   4 * * 1-3 "),
            schedules.Compiled(cron="0 4 * * tue,wed,thu"),
        ),
    ],
)
def test_a_plan_compiles_to_what_the_ingester_reads(
    plan: Plan, expected: schedules.Compiled
) -> None:
    assert schedules.compile_plan(plan) == expected


@pytest.mark.parametrize(
    "plan",
    [
        Plan(mode="interval", every_n=0),
        Plan(mode="interval", every_unit="w"),
        Plan(mode="hourly", minute=60),
        Plan(mode="daily", time="25:00"),
        Plan(mode="weekly", weekdays=()),
        Plan(mode="weekly", weekdays=("funday",)),
        Plan(mode="monthly", day_of_month=32),
        Plan(mode="cron", cron=""),
    ],
)
def test_a_plan_that_cannot_run_is_refused(plan: Plan) -> None:
    with pytest.raises(ScheduleError):
        schedules.compile_plan(plan)


@pytest.mark.parametrize(
    "expression",
    [
        "0 3 * * 0",
        "0 3 * * 1",
        "0 3 * * 6",
        "0 3 * * 0,2,4",
        "0 3 * * 1-3",
        "30 8 * * mon-fri",
        "0 3 * * *",
        "0 3 15 * *",
    ],
)
def test_writing_names_never_changes_what_the_ingester_runs(expression: str) -> None:
    """The point of writing names: read in the ingester's dialect, written in names, same runs."""
    rewritten = schedules.compile_plan(schedules.read_plan({"cron": expression})).cron

    assert rewritten is not None
    assert _fires(rewritten) == _fires(expression)


def test_a_monday_is_a_monday_whatever_the_dialect() -> None:
    weekly = Plan(mode="weekly", time="03:00", weekdays=("mon",))

    preview = schedules.preview(weekly, now=_NOW)

    assert preview.ok
    assert preview.cron == "0 3 * * mon"
    assert [run.split(",")[0].split()[0] for run in preview.runs] == ["Mon", "Mon", "Mon"]


def test_a_number_in_a_step_is_left_alone_and_flagged() -> None:
    assert schedules.normalise_cron("0 3 * * */2") == "0 3 * * */2"

    preview = schedules.preview(Plan(mode="cron", cron="0 3 * * */2"), now=_NOW)

    assert preview.ok
    assert any("count from Monday" in note for note in preview.notes)


# ---------------------------------------------------------------------------
# Previews and words
# ---------------------------------------------------------------------------


def test_the_preview_lists_the_next_runs_in_the_schedules_zone() -> None:
    plan = Plan(mode="daily", time="03:00", timezone="Europe/Berlin")

    preview = schedules.preview(plan, now=_NOW)

    assert preview.ok and preview.timezone == "Europe/Berlin"
    assert preview.runs[0] == "Tue 06 Oct 2026, 03:00"
    assert len(preview.runs) == 3


def test_the_default_zone_is_the_one_the_caller_supplies() -> None:
    preview = schedules.preview(Plan(mode="daily"), default_timezone="Europe/Berlin", now=_NOW)

    assert preview.timezone == "Europe/Berlin"


def test_an_interval_promises_no_clock_time() -> None:
    preview = schedules.preview(Plan(mode="interval", every_n=2, every_unit="h"), now=_NOW)

    assert preview.ok
    assert preview.every == "2h"
    assert preview.counts_from_load is True
    assert preview.runs == ()


def test_a_day_that_not_every_month_has_is_flagged() -> None:
    preview = schedules.preview(Plan(mode="monthly", day_of_month=31), now=_NOW)

    assert preview.ok
    assert any("skipped" in note for note in preview.notes)
    assert schedules.preview(Plan(mode="monthly", day_of_month=28), now=_NOW).notes == ()


@pytest.mark.parametrize(
    ("plan", "fragment"),
    [
        (Plan(mode="cron", cron="not a cron"), "cron"),
        (Plan(mode="cron", cron="61 3 * * *"), "cron"),
        (Plan(mode="daily", timezone="Nowhere/Land"), "timezone"),
        (Plan(mode="weekly"), "day of the week"),
    ],
)
def test_a_schedule_that_will_not_load_says_why(plan: Plan, fragment: str) -> None:
    preview = schedules.preview(plan, now=_NOW)

    assert not preview.ok
    assert preview.error is not None and fragment in preview.error.lower()


def test_cron_error_follows_the_ingesters_parser() -> None:
    assert schedules.cron_error("0 3 * * mon") is None
    assert schedules.cron_error("0 3 * * 7") is not None  # APScheduler refuses 7
    assert schedules.cron_error("nonsense") is not None
    assert schedules.every_error("15m") is None
    assert schedules.every_error("0m") is not None
    assert schedules.every_error("15 minutes") is not None


@pytest.mark.parametrize(
    ("plan", "sentence"),
    [
        (Plan(mode="manual"), "Only when started by hand"),
        (Plan(mode="interval", every_n=1, every_unit="h"), "Every hour"),
        (Plan(mode="interval", every_n=30, every_unit="s"), "Every 30 seconds"),
        (Plan(mode="hourly", minute=5), "Every hour at :05"),
        (Plan(mode="daily", time="03:00"), "Every day at 03:00"),
        (
            Plan(mode="weekly", time="08:30", weekdays=("mon", "fri")),
            "Every Monday, Friday at 08:30",
        ),
        (Plan(mode="monthly", time="01:00", day_of_month=15), "On day 15 of every month at 01:00"),
        (Plan(mode="cron", cron="0 */4 * * *"), "Custom schedule (0 */4 * * *)"),
    ],
)
def test_a_plan_is_described_in_words(plan: Plan, sentence: str) -> None:
    assert schedules.describe(plan) == sentence


def test_a_stored_block_is_described_in_words() -> None:
    assert schedules.describe_block({"cron": "0 3 * * 1"}) == "Every Tuesday at 03:00"
    assert schedules.describe_block(None) == "Only when started by hand"


def test_the_timezone_chain_prefers_the_job_then_the_defaults_then_the_ingester() -> None:
    chain = schedules.effective_timezone
    assert chain("Asia/Tokyo", "Europe/Berlin", "UTC") == "Asia/Tokyo"
    assert chain(None, "Europe/Berlin", "America/New_York") == "Europe/Berlin"
    assert chain(None, None, "America/New_York") == "America/New_York"
    assert chain(None, None, "") == "UTC"
