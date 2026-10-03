"""The backup schedule's rules: expressions, timezones, retention, and the file.

Pure logic, no manager and no scheduler. What is asserted here is what the page and
the scheduler rely on: that an expression means what its author thinks it means,
that a schedule which could harm a deployment is refused before it is written, and
that a damaged schedule.yaml is reported instead of silently meaning "no schedule".
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from app.core import schedule
from app.core.backups import RestorePoint
from app.core.schedule import ScheduleError

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)  # a Saturday


def _point(age: timedelta, result: str = "ok", *, name: str = "p") -> RestorePoint:
    taken = NOW - age
    return RestorePoint(
        id=f"{name}-{int(age.total_seconds())}",
        created_at=taken.strftime("%Y-%m-%dT%H:%M:%SZ"),
        result=result,
    )


def _daily(**fields: object) -> schedule.BackupSchedule:
    return schedule.build_schedule(cron="0 3 * * *", timezone="UTC", **fields)


# ---------------------------------------------------------------------------
# Expressions mean what a crontab means
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "stored"),
    [
        ("0 3 * * *", "0 3 * * *"),
        ("  0   3 *  * * ", "0 3 * * *"),
        ("0 3 * * 1", "0 3 * * mon"),
        ("0 3 * * 0", "0 3 * * sun"),
        ("0 3 * * 7", "0 3 * * sun"),
        ("0 3 * * 1,4", "0 3 * * mon,thu"),
        ("0 3 * * 1-5", "0 3 * * mon,tue,wed,thu,fri"),
        ("0 3 * * MON-FRI", "0 3 * * mon,tue,wed,thu,fri"),
        ("0 3 * * mon-sun", "0 3 * * *"),
        ("0 3 * * 0-6", "0 3 * * *"),
        # Standard cron counts the step from Sunday: 0, 2, 4, 6.
        ("0 3 * * */2", "0 3 * * tue,thu,sat,sun"),
        ("0 */6 * * *", "0 */6 * * *"),
        ("15 4 1 * *", "15 4 1 * *"),
    ],
)
def test_an_expression_is_normalised_to_names_for_the_weekday(typed: str, stored: str) -> None:
    assert schedule.parse_fields(typed).expression == stored


def test_a_numeric_weekday_means_the_day_cron_means_not_the_day_apscheduler_means() -> None:
    """APScheduler 3.x counts weekdays from Monday, so `1` is Tuesday there. The
    expression is normalised so that `1` is Monday, as in every crontab."""
    first = schedule.next_runs("0 3 * * 1", "UTC", count=2, now=NOW)
    assert [run.weekday() for run in first] == [0, 0]  # Monday
    assert first[0] == datetime(2026, 10, 5, 3, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "typed",
    [
        "",
        "0 3 * *",
        "0 3 * * * *",
        "a 3 * * *",
        "0 3 L * *",
        "0 3 1st * mon",
        "60 3 * * *",
        "0 24 * * *",
        "0 3 32 * *",
        "0 3 * 13 *",
        "0 3 * * 8",
        "0 3 * * foo",
        "0 3 * * 5-1",
        "0 3 * * 1/2",
        "0 3 * * */0",
    ],
)
def test_a_malformed_expression_is_refused(typed: str) -> None:
    with pytest.raises(ScheduleError):
        schedule.parse_fields(typed)


def test_a_day_of_month_together_with_a_weekday_is_refused() -> None:
    """Cron runs on either, APScheduler on both. Neither is what someone writing
    `1 * mon` means, so it is not accepted at all."""
    with pytest.raises(ScheduleError, match="either the day of the month or the day of the week"):
        schedule.parse_fields("0 3 1 * mon")


# ---------------------------------------------------------------------------
# Timezone
# ---------------------------------------------------------------------------


def test_a_known_zone_is_accepted() -> None:
    assert schedule.zone("Europe/Berlin").key == "Europe/Berlin"
    assert schedule.zone("UTC").key == "UTC"


@pytest.mark.parametrize(
    "name", ["", "Mars/Olympus", "../etc/passwd", "Europe/../UTC", "a b", "x" * 80]
)
def test_an_unknown_or_hostile_zone_is_refused(name: str) -> None:
    with pytest.raises(ScheduleError, match="Unknown timezone"):
        schedule.zone(name)


def test_the_default_zone_is_the_containers_tz_or_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TZ", raising=False)
    assert schedule.default_timezone() == "UTC"
    monkeypatch.setenv("TZ", "Europe/Berlin")
    assert schedule.default_timezone() == "Europe/Berlin"
    # A POSIX TZ string is not an IANA name and must not become one.
    monkeypatch.setenv("TZ", "CET-1CEST")
    assert schedule.default_timezone() == "UTC"


def test_a_time_that_does_not_exist_is_reported_where_it_happens() -> None:
    """On 2026-03-29 the clocks in Berlin skip from 02:00 to 03:00. A 02:30 run
    happens at 03:30 that day, and is shown that way."""
    runs = schedule.next_runs(
        "30 2 * * *", "Europe/Berlin", count=2, now=datetime(2026, 3, 28, 12, 0, tzinfo=UTC)
    )
    assert runs[0].strftime("%Y-%m-%d %H:%M") == "2026-03-29 03:30"
    assert runs[0].utcoffset() == timedelta(hours=2)
    assert runs[1].strftime("%Y-%m-%d %H:%M") == "2026-03-30 02:30"


def test_times_are_formatted_in_the_schedules_own_zone() -> None:
    moment = datetime(2026, 7, 1, 1, 0, tzinfo=UTC)
    assert schedule.format_moment(moment, "Europe/Berlin") == "Wed 2026-07-01 03:00 CEST"
    assert schedule.format_moment(moment, "UTC") == "Wed 2026-07-01 01:00 UTC"


# ---------------------------------------------------------------------------
# Cadence, and what it limits
# ---------------------------------------------------------------------------


def test_a_daily_schedule_is_24_hours_apart_by_the_wall_clock_across_a_clock_change() -> None:
    """`longest` is what the schedule is *meant* to go without a backup, so it is the
    wall-clock 24 hours however the clocks move. `shortest` is elapsed time, and on
    the day the clocks spring forward two runs at 03:00 are only 23 hours apart."""
    cadence = schedule.analyse_cadence(
        "0 3 * * *", "Europe/Berlin", now=datetime(2026, 3, 1, tzinfo=UTC)
    )
    assert cadence.longest == timedelta(hours=24)
    assert cadence.shortest == timedelta(hours=23)


@pytest.mark.parametrize("zone_name", ["Europe/Berlin", "America/New_York", "Australia/Sydney"])
def test_an_hourly_schedule_is_one_hour_apart_even_where_the_clock_repeats_an_hour(
    zone_name: str,
) -> None:
    """When the clocks go back the wall clock reads the same hour twice, and an hourly
    schedule runs at both. Read off the wall clock those two runs are 0 apart; in
    elapsed time they are the hour they always were."""
    cadence = schedule.analyse_cadence("0 * * * *", zone_name, now=datetime(2026, 3, 1, tzinfo=UTC))
    assert cadence.shortest == timedelta(hours=1)
    assert cadence.longest == timedelta(hours=1)


def test_the_next_runs_walk_forward_through_the_hour_the_clocks_repeat() -> None:
    """On 2026-10-25 the clocks in Berlin go back from 03:00 to 02:00. An hourly
    schedule runs at every real hour across that, 02:00 twice included, and the
    list of next runs must not go backwards or repeat itself."""
    runs = schedule.next_runs(
        "0 * * * *", "Europe/Berlin", count=6, now=datetime(2026, 10, 24, 22, 30, tzinfo=UTC)
    )
    assert [run.astimezone(UTC) for run in runs] == [
        datetime(2026, 10, 24, 23, 0, tzinfo=UTC),
        datetime(2026, 10, 25, 0, 0, tzinfo=UTC),
        datetime(2026, 10, 25, 1, 0, tzinfo=UTC),
        datetime(2026, 10, 25, 2, 0, tzinfo=UTC),
        datetime(2026, 10, 25, 3, 0, tzinfo=UTC),
        datetime(2026, 10, 25, 4, 0, tzinfo=UTC),
    ]


def test_a_fixed_time_inside_the_repeated_hour_runs_twice_on_that_day() -> None:
    """Known behaviour of APScheduler 3.x, documented in AGENTS.md and not worked
    around: 02:30 happens twice on the day the clocks go back, and a daily 02:30
    schedule runs at both. With the single-flight queue that is a second backup an
    hour later, once a year. If this starts failing, APScheduler has changed and the
    note can go."""
    runs = schedule.next_runs(
        "30 2 * * *", "Europe/Berlin", count=3, now=datetime(2026, 10, 24, 22, 0, tzinfo=UTC)
    )
    instants = [run.astimezone(UTC) for run in runs]
    assert instants[1] - instants[0] == timedelta(hours=1)
    assert instants[2] - instants[1] == timedelta(hours=24)


@pytest.mark.parametrize("zone_name", ["Europe/Berlin", "America/New_York", "Australia/Sydney"])
@pytest.mark.parametrize("cron", ["0 * * * *", "30 */2 * * *", "0 */3 * * *"])
def test_an_hourly_schedule_is_accepted_in_a_zone_with_daylight_saving(
    zone_name: str, cron: str
) -> None:
    assert schedule.build_schedule(cron=cron, timezone=zone_name).cron == cron


def test_runs_closer_than_an_hour_are_still_refused_in_a_zone_with_daylight_saving() -> None:
    with pytest.raises(ScheduleError, match="more often than once an hour"):
        schedule.build_schedule(cron="0,30 * * * *", timezone="Europe/Berlin")


def test_an_uneven_schedule_has_a_shortest_and_a_longest_gap() -> None:
    weekly = schedule.analyse_cadence("0 3 * * mon,thu", "UTC", now=NOW)
    assert (weekly.shortest, weekly.longest) == (timedelta(days=3), timedelta(days=4))
    hourly = schedule.analyse_cadence("0 */5 * * *", "UTC", now=NOW)
    assert (hourly.shortest, hourly.longest) == (timedelta(hours=4), timedelta(hours=5))


def test_a_schedule_that_runs_less_than_twice_in_two_years_has_no_cadence() -> None:
    with pytest.raises(ScheduleError, match="less than twice"):
        schedule.analyse_cadence("0 3 29 2 *", "UTC", now=NOW)


@pytest.mark.parametrize(
    ("cron", "floor"),
    [("0 3 * * *", 2), ("0 */6 * * *", 1), ("0 3 * * mon", 14), ("0 3 * * mon,thu", 8)],
)
def test_the_retention_floor_is_twice_the_longest_gap_in_whole_days(cron: str, floor: int) -> None:
    assert schedule.min_retention_days(schedule.analyse_cadence(cron, "UTC", now=NOW)) == floor


# ---------------------------------------------------------------------------
# The model refuses what could do harm
# ---------------------------------------------------------------------------


def test_a_valid_schedule_gets_its_defaults() -> None:
    current = _daily()
    assert current.enabled is True
    assert current.retention_days is None
    assert current.run_on_startup == "if_missed"
    assert current.misfire_grace_seconds == 3600


def test_runs_closer_together_than_an_hour_are_refused() -> None:
    with pytest.raises(ScheduleError, match="more often than once an hour"):
        schedule.build_schedule(cron="*/30 * * * *", timezone="UTC")
    with pytest.raises(ScheduleError, match="more often than once an hour"):
        schedule.build_schedule(cron="0,10 3 * * *", timezone="UTC")
    assert schedule.build_schedule(cron="0 * * * *", timezone="UTC").cron == "0 * * * *"


@pytest.mark.parametrize(("days", "accepted"), [(1, False), (2, True), (30, True)])
def test_a_daily_retention_must_keep_at_least_two_runs(days: int, accepted: bool) -> None:
    if accepted:
        assert _daily(retention_days=days).retention_days == days
    else:
        with pytest.raises(ScheduleError, match="at least 2 days"):
            _daily(retention_days=days)


def test_a_weekly_retention_floor_follows_the_gap() -> None:
    with pytest.raises(ScheduleError, match="at least 14 days"):
        schedule.build_schedule(cron="0 3 * * mon", timezone="UTC", retention_days=13)
    assert schedule.build_schedule(
        cron="0 3 * * mon", timezone="UTC", retention_days=14
    ).retention_days == 14


@pytest.mark.parametrize("days", [0, -1, 3651])
def test_a_retention_outside_its_range_is_refused(days: int) -> None:
    """0 is papaia-ctl's "delete everything older than today" -- fine for a person
    clicking a button, not for a timer."""
    with pytest.raises(ScheduleError):
        _daily(retention_days=days)


def test_an_unknown_key_is_an_error_and_not_ignored() -> None:
    with pytest.raises(ScheduleError, match="foo"):
        _daily(foo=1)


def test_an_unknown_zone_in_the_model_is_refused() -> None:
    with pytest.raises(ScheduleError, match="Unknown timezone"):
        schedule.build_schedule(cron="0 3 * * *", timezone="Nowhere/Land")


def test_the_cron_is_stored_normalised() -> None:
    assert schedule.build_schedule(cron="0 3 * * 1", timezone="UTC").cron == "0 3 * * mon"


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "cron"),
    [
        ({"mode": "daily", "time": "03:00"}, "0 3 * * *"),
        ({"mode": "daily", "time": "23:45"}, "45 23 * * *"),
        ({"mode": "weekly", "time": "04:15", "weekdays": ["thu", "mon"]}, "15 4 * * mon,thu"),
        ({"mode": "weekly", "time": "02:00", "weekdays": list(schedule.WEEKDAYS)}, "0 2 * * *"),
        ({"mode": "hourly", "time": "00:30", "every_hours": 6}, "30 */6 * * *"),
        ({"mode": "hourly", "time": "00:00", "every_hours": 1}, "0 * * * *"),
        ({"mode": "custom", "cron": "  5  4 * *  tue "}, "5 4 * * tue"),
    ],
)
def test_a_preset_compiles_to_cron(kwargs: dict[str, object], cron: str) -> None:
    assert schedule.compile_preset(**kwargs) == cron  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"mode": "daily", "time": "25:00"},
        {"mode": "daily", "time": "3"},
        {"mode": "weekly", "weekdays": []},
        {"mode": "weekly", "weekdays": ["funday"]},
        {"mode": "hourly", "every_hours": 5},
        {"mode": "nonsense"},
    ],
)
def test_an_unusable_preset_is_refused(kwargs: dict[str, object]) -> None:
    with pytest.raises(ScheduleError):
        schedule.compile_preset(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "expression",
    ["0 3 * * *", "45 23 * * *", "15 4 * * mon,thu", "30 */6 * * *", "0 * * * *"],
)
def test_a_preset_survives_the_round_trip_through_cron(expression: str) -> None:
    preset = schedule.read_preset(expression)
    assert preset.mode != "custom"
    again = schedule.compile_preset(
        preset.mode,
        time=preset.time,
        weekdays=preset.weekdays,
        every_hours=preset.every_hours,
    )
    assert again == expression


@pytest.mark.parametrize("expression", ["0 3 1 * *", "0 3,15 * * *", "*/20 * * * *", "0 */5 * * *"])
def test_anything_else_reads_back_as_custom(expression: str) -> None:
    assert schedule.read_preset(expression).mode == "custom"


@pytest.mark.parametrize(
    ("expression", "sentence"),
    [
        ("0 3 * * *", "Every day at 03:00"),
        ("15 4 * * mon,thu", "Every Monday, Thursday at 04:15"),
        ("0 */6 * * *", "Every 6 hours at :00"),
        ("30 * * * *", "Every hour at :30"),
        ("0 3 1 * *", "Custom schedule (0 3 1 * *)"),
    ],
)
def test_an_expression_is_described_in_a_sentence(expression: str, sentence: str) -> None:
    assert schedule.describe(expression) == sentence


# ---------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------


def test_a_schedule_survives_a_round_trip_through_the_file(tmp_path: Path) -> None:
    saved = schedule.build_schedule(
        cron="15 4 * * 1,4",
        timezone="Europe/Berlin",
        retention_days=14,
        run_on_startup="never",
        enabled=False,
    )
    schedule.save_schedule(str(tmp_path), saved)

    loaded = schedule.load_schedule(str(tmp_path))
    assert loaded.error is None
    assert loaded.schedule == saved
    path = schedule.schedule_path(str(tmp_path))
    assert path == tmp_path / "manager" / "schedule.yaml"
    assert path.read_text(encoding="utf-8").startswith("# papaia-manager backup schedule")
    assert not list(path.parent.glob("*.tmp"))


def test_no_file_is_no_schedule_and_not_an_error(tmp_path: Path) -> None:
    assert schedule.load_schedule(str(tmp_path)) == schedule.ScheduleLoad(None, None)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("cron: [unterminated", "cannot be read"),
        ("- a\n- list\n", "does not contain a mapping"),
        ("cron: '0 3 * * *'\nbogus: true\n", "invalid"),
        ("cron: '*/5 * * * *'\n", "more often than once an hour"),
        ("cron: '0 3 * * *'\ntimezone: Nowhere/Land\n", "Unknown timezone"),
    ],
)
def test_a_damaged_file_is_reported_and_is_not_a_schedule(
    tmp_path: Path, content: str, message: str
) -> None:
    path = schedule.schedule_path(str(tmp_path))
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")

    loaded = schedule.load_schedule(str(tmp_path))
    assert loaded.schedule is None
    assert loaded.error is not None
    assert message in loaded.error
    assert "schedule.yaml" in loaded.error


def test_an_empty_file_is_no_schedule(tmp_path: Path) -> None:
    path = schedule.schedule_path(str(tmp_path))
    path.parent.mkdir(parents=True)
    path.write_text("", encoding="utf-8")
    assert schedule.load_schedule(str(tmp_path)) == schedule.ScheduleLoad(None, None)


def test_deleting_says_whether_there_was_a_file(tmp_path: Path) -> None:
    assert schedule.delete_schedule(str(tmp_path)) is False
    schedule.save_schedule(str(tmp_path), _daily())
    assert schedule.delete_schedule(str(tmp_path)) is True
    assert schedule.load_schedule(str(tmp_path)).schedule is None


# ---------------------------------------------------------------------------
# Age, overdue and catching up
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("span", "text"),
    [
        (timedelta(seconds=20), "under a minute"),
        (timedelta(minutes=1), "1 minute"),
        (timedelta(minutes=125), "2 hours 5 minutes"),
        (timedelta(hours=5), "5 hours"),
        (timedelta(hours=24), "1 day"),
        (timedelta(hours=36), "1 day 12 hours"),
        (timedelta(days=3, hours=23), "3 days 23 hours"),
        (timedelta(days=8, hours=3), "8 days"),
        (timedelta(seconds=-5), "under a minute"),
    ],
)
def test_a_span_is_spelled_in_up_to_two_units(span: timedelta, text: str) -> None:
    assert schedule.humanize(span) == text


def test_overdue_is_one_and_a_half_times_the_longest_gap() -> None:
    assert schedule.overdue_after(_daily()) == timedelta(hours=36)
    weekly = schedule.build_schedule(cron="0 3 * * mon", timezone="UTC")
    assert schedule.overdue_after(weekly) == timedelta(days=10, hours=12)


def test_a_missed_run_is_made_up_only_when_the_newest_success_is_overdue() -> None:
    current = _daily()
    assert schedule.catchup_due(current, [_point(timedelta(hours=10))], now=NOW) is False
    assert schedule.catchup_due(current, [_point(timedelta(hours=35))], now=NOW) is False
    assert schedule.catchup_due(current, [_point(timedelta(hours=37))], now=NOW) is True


def test_nothing_ever_taken_is_a_missed_run() -> None:
    assert schedule.catchup_due(_daily(), [], now=NOW) is True


def test_only_a_run_that_finished_ok_counts_as_a_backup() -> None:
    """A partial restore point is usable but not a success, and a failed one is
    neither -- neither may tell the schedule that all is well."""
    current = _daily()
    points = [
        _point(timedelta(hours=1), "partial", name="a"),
        _point(timedelta(hours=2), "failed", name="b"),
        _point(timedelta(hours=60), "ok", name="c"),
    ]
    assert schedule.catchup_due(current, points, now=NOW) is True


def test_the_status_is_overdue_only_for_an_active_schedule() -> None:
    old = [_point(timedelta(days=5))]
    kwargs = {"now": NOW, "next_run": None}
    assert schedule.evaluate(_daily(), old, **kwargs).overdue is True  # type: ignore[arg-type]
    assert schedule.evaluate(None, old, **kwargs).overdue is False  # type: ignore[arg-type]
    paused = _daily(enabled=False)
    assert schedule.evaluate(paused, old, **kwargs).overdue is False  # type: ignore[arg-type]
    fresh = [_point(timedelta(hours=20))]
    assert schedule.evaluate(_daily(), fresh, **kwargs).overdue is False  # type: ignore[arg-type]


def test_an_empty_catalogue_is_not_overdue_but_restore_points_that_never_succeeded_are() -> None:
    assert schedule.evaluate(_daily(), [], now=NOW, next_run=None).overdue is False
    failed_only = [_point(timedelta(hours=1), "failed")]
    assert schedule.evaluate(_daily(), failed_only, now=NOW, next_run=None).overdue is True


def test_the_status_names_the_latest_result_of_any_outcome() -> None:
    points = [
        _point(timedelta(hours=30), "ok", name="a"),
        _point(timedelta(hours=2), "partial", name="b"),
    ]
    status = schedule.evaluate(_daily(), points, now=NOW, next_run=None)
    assert status.latest_result == "partial"
    assert status.age == timedelta(hours=30)
    assert status.overdue is False


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def test_no_retention_means_no_flag() -> None:
    plan = schedule.plan_retention(_daily(), [_point(timedelta(hours=5))], now=NOW)
    assert plan == schedule.RetentionPlan(None, None)


def test_the_retention_is_applied_while_there_is_a_recent_good_restore_point() -> None:
    plan = schedule.plan_retention(
        _daily(retention_days=14), [_point(timedelta(hours=24))], now=NOW
    )
    assert plan == schedule.RetentionPlan(14, None)


def test_the_retention_is_held_back_once_the_newest_success_is_overdue() -> None:
    """papaia-ctl prunes after a failed run too. With the last good restore point
    past the overdue limit, another prune could only remove good ones."""
    plan = schedule.plan_retention(
        _daily(retention_days=14),
        [
            _point(timedelta(hours=1), "failed", name="a"),
            _point(timedelta(hours=40), "ok", name="b"),
        ],
        now=NOW,
    )
    assert plan.days is None
    assert plan.skipped is not None
    assert "1 day 16 hours old (more than 1 day 12 hours)" in plan.skipped


def test_the_retention_waits_for_a_first_good_restore_point() -> None:
    plan = schedule.plan_retention(_daily(retention_days=14), [], now=NOW)
    assert plan.days is None
    assert plan.skipped == "there is no successful restore point yet"
    only_failed = schedule.plan_retention(
        _daily(retention_days=14), [_point(timedelta(hours=3), "failed")], now=NOW
    )
    assert only_failed.days is None


# ---------------------------------------------------------------------------
# The state the page and the API read
# ---------------------------------------------------------------------------


def _catalogue(tmp_path: Path, ages: list[tuple[timedelta, str]]) -> str:
    config_dir = tmp_path / "config"
    backup_dir = tmp_path / "backups"
    config_dir.mkdir()
    backup_dir.mkdir()
    (config_dir / ".env").write_text(f"PAPAIA_BACKUP_DIR={backup_dir}\n", encoding="utf-8")
    entries = []
    for i, (age, result) in enumerate(ages):
        taken = datetime.now(tz=UTC) - age
        entries.append(
            {
                "id": taken.strftime("%Y-%m-%d_%H-%M-%S") + f"-{i}",
                "created_at": taken.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "result": result,
            }
        )
    (backup_dir / "backup.yaml").write_text(
        yaml.safe_dump({"version": 1, "backups": entries}), encoding="utf-8"
    )
    return str(config_dir)


def test_the_state_without_a_schedule_still_reports_the_last_backup(tmp_path: Path) -> None:
    config_dir = _catalogue(tmp_path, [(timedelta(days=3, hours=2), "ok")])
    state = schedule.read_state(config_dir, next_run=None)
    assert state.schedule is None
    assert state.age_text == "3 days 2 hours"
    assert state.last_ok_text is not None
    assert state.status.overdue is False
    assert state.description == ""
    assert state.next_runs_text == []
    assert state.backup_dir_reachable is True


def test_the_state_with_a_schedule_says_when_and_how_overdue(tmp_path: Path) -> None:
    config_dir = _catalogue(tmp_path, [(timedelta(days=5), "ok")])
    schedule.save_schedule(config_dir, schedule.build_schedule(
        cron="0 3 * * *", timezone="Europe/Berlin", retention_days=7
    ))
    state = schedule.read_state(config_dir, next_run=datetime.now(tz=UTC) + timedelta(hours=2))

    assert state.status.overdue is True
    assert state.overdue_after_text == "1 day 12 hours"
    assert state.description == "Every day at 03:00"
    assert len(state.next_runs_text) == 3
    assert state.next_run_text is not None
    assert state.retention_note is not None
    assert "5 days old" in state.retention_note
    assert state.would_delete == 0  # nothing is older than 7 days


def test_the_retention_preview_counts_what_the_next_run_would_delete(tmp_path: Path) -> None:
    config_dir = _catalogue(
        tmp_path,
        [(timedelta(days=30), "ok"), (timedelta(days=20), "failed"), (timedelta(hours=20), "ok")],
    )
    schedule.save_schedule(config_dir, schedule.build_schedule(
        cron="0 3 * * *", timezone="UTC", retention_days=14
    ))
    state = schedule.read_state(config_dir, next_run=None)
    assert state.would_delete == 2
    assert state.retention_note is None  # the newest success is fresh


def test_the_state_carries_the_load_error_of_a_damaged_file(tmp_path: Path) -> None:
    config_dir = _catalogue(tmp_path, [])
    path = schedule.schedule_path(config_dir)
    path.parent.mkdir(parents=True)
    path.write_text("cron: nope\n", encoding="utf-8")
    state = schedule.read_state(config_dir, next_run=None)
    assert state.schedule is None
    assert state.load.error is not None
    assert schedule.state_to_dict(state)["error"] == state.load.error


def test_the_state_serialises_for_the_api(tmp_path: Path) -> None:
    config_dir = _catalogue(tmp_path, [(timedelta(hours=5), "ok")])
    schedule.save_schedule(config_dir, schedule.build_schedule(
        cron="30 4 * * mon,thu", timezone="UTC"
    ))
    data = schedule.state_to_dict(schedule.read_state(config_dir, next_run=None))

    assert data["configured"] is True
    assert data["schedule"]["cron"] == "30 4 * * mon,thu"
    assert data["preset"] == {
        "mode": "weekly",
        "time": "04:30",
        "weekdays": ["mon", "thu"],
        "every_hours": 6,
        "cron": "",
    }
    assert data["overdue"] is False
    assert data["next_run"] is None
    assert data["description"] == "Every Monday, Thursday at 04:30"
