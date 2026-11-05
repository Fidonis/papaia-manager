"""The numbers behind the Models page: windows, the sums of LiteLLM's day entries, colours
and the cache.

No page and no HTTP here except the fake LiteLLM behind `httpx.MockTransport`; the page's own
words are in `test_ui_models.py`.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

_CONFIG_DIR = tempfile.mkdtemp(prefix="papaia-usage-config-")
_WORKSPACE_DIR = tempfile.mkdtemp(prefix="papaia-usage-workspace-")

for _key, _value in {
    "OIDC_ISSUER_KC_AUTH": "https://kc.test/auth",
    "OIDC_ISSUER_KC_TOKEN": "https://kc.test/token",
    "OIDC_ISSUER_KC_CERTS": "https://kc.test/certs",
    "MANAGER_ADMIN_ROLE": "admin",
    "MANAGER_USER_ROLE": "user",
    "MANAGER_HOST": "http://localhost:8120",
    "MANAGER_OIDC_CLIENT_SECRET": "client-secret",
    "MANAGER_SESSION_SECRET": "test-session-secret-value",
    "PAPAIA_CONFIG_DIR": _CONFIG_DIR,
    "PAPAIA_WORKSPACE_DIR": _WORKSPACE_DIR,
}.items():
    os.environ.setdefault(_key, _value)

from app.config import Settings, get_settings  # noqa: E402
from app.core import model_usage  # noqa: E402
from app.core.litellm import NO_KEY, REJECTED, UNREADABLE  # noqa: E402
from app.core.model_usage import (  # noqa: E402
    SLOTS,
    InvalidWindow,
    Metrics,
    Window,
    assign_slots,
    build_report,
    load_reading,
    metrics_from,
    mode_label,
    parse_catalog,
    parse_usage,
    resolve_window,
)
from tests.fake_litellm import MASTER_KEY, FakeLitellm  # noqa: E402

TODAY = date(2026, 10, 24)


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    model_usage.reset_cache()
    yield
    model_usage.reset_cache()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    (tmp_path / "ai" / "litellm").mkdir(parents=True)
    (tmp_path / "ai" / "litellm" / ".env").write_text(
        f"LITELLM_MASTER_KEY={MASTER_KEY}\n", encoding="utf-8"
    )
    get_settings.cache_clear()
    return get_settings().model_copy(update={"papaia_config_dir": str(tmp_path)})


class _Clock:
    """The cache's clock, moved by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(model_usage, "_clock", fake)
    return fake


# ── the window ──────────────────────────────────────────────────────────────────────────────


def test_a_week_is_today_and_the_six_days_before() -> None:
    window = resolve_window("7d", today=TODAY)

    assert (window.start, window.end, window.length) == (date(2026, 10, 18), TODAY, 7)
    assert window.dates()[0] == window.start and window.dates()[-1] == TODAY


@pytest.mark.parametrize(("key", "length"), [("30d", 30), ("90d", 90)])
def test_the_longer_presets_count_today_in(key: str, length: int) -> None:
    window = resolve_window(key, today=TODAY)

    assert window.length == length and window.end == TODAY


def test_this_month_starts_on_the_first_and_is_one_day_long_on_the_first() -> None:
    assert resolve_window("month", today=TODAY).start == date(2026, 10, 1)
    assert resolve_window("month", today=date(2026, 11, 1)).length == 1


def test_a_custom_window_takes_both_ends_as_given_and_ignores_stray_blanks() -> None:
    window = resolve_window("custom", " 2026-10-01 ", "2026-10-03", today=TODAY)

    assert (window.start, window.end, window.length) == (date(2026, 10, 1), date(2026, 10, 3), 3)


def test_a_custom_window_may_end_today_and_be_a_year_long_but_no_longer() -> None:
    assert resolve_window("custom", "2026-10-24", "2026-10-24", today=TODAY).length == 1
    year = resolve_window("custom", "2025-10-25", "2026-10-24", today=TODAY)
    assert year.length == 365

    with pytest.raises(InvalidWindow, match="365"):
        resolve_window("custom", "2025-10-24", "2026-10-24", today=TODAY)


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("custom", "", ""), "Enter both dates."),
        (("custom", "2026-10-01", ""), "Enter both dates."),
        (("custom", "01.10.2026", "2026-10-03"), "Dates must look like 2026-10-24."),
        (("custom", "2026-02-30", "2026-03-03"), "Dates must look like 2026-10-24."),
        (("custom", "2026-10-03", "2026-10-01"), "The start date is after the end date."),
        (("custom", "2026-10-01", "2026-10-25"), "The end date can't be in the future."),
        (("tomorrow",), "Unknown time window."),
        (("",), "Unknown time window."),
    ],
)
def test_a_window_that_makes_no_sense_is_refused_in_words(
    args: tuple[str, ...], message: str
) -> None:
    with pytest.raises(InvalidWindow) as raised:
        resolve_window(*args, today=TODAY)

    assert str(raised.value) == message


# ── LiteLLM's numbers ───────────────────────────────────────────────────────────────────────


def test_a_metrics_object_with_gaps_or_junk_counts_what_it_can() -> None:
    assert metrics_from(None) == Metrics()
    assert metrics_from("nope") == Metrics()
    junk = {"prompt_tokens": None, "completion_tokens": "x", "spend": "n/a"}
    assert metrics_from(junk) == Metrics()
    assert metrics_from(
        {
            "prompt_tokens": 3,
            "completion_tokens": "4",
            "spend": "0.5",
            "api_requests": 2,
            "failed_requests": 1,
        }
    ) == Metrics(3, 4, 0.5, 2, 1)
    assert Metrics(3, 4).tokens == 7


def _entry(day: str, **groups: dict[str, Any]) -> dict[str, Any]:
    return {
        "date": day,
        "metrics": {},
        "breakdown": {"model_groups": {name: {"metrics": m} for name, m in groups.items()}},
    }


def test_the_same_model_and_day_from_several_entries_are_added_up() -> None:
    window = Window("7d", date(2026, 10, 18), TODAY)
    results = [
        _entry("2026-10-24", a={"prompt_tokens": 1, "api_requests": 1}),
        _entry("2026-10-24", a={"prompt_tokens": 2, "api_requests": 1}, b={"prompt_tokens": 5}),
        _entry("2026-10-23", a={"completion_tokens": 7}),
    ]

    usage = parse_usage(results, window)

    assert usage["a"][date(2026, 10, 24)] == Metrics(prompt_tokens=3, requests=2)
    assert usage["a"][date(2026, 10, 23)] == Metrics(completion_tokens=7)
    assert usage["b"][date(2026, 10, 24)] == Metrics(prompt_tokens=5)


def test_entries_that_cannot_be_placed_are_dropped_and_do_not_stop_the_rest() -> None:
    window = Window("7d", date(2026, 10, 18), TODAY)
    results: list[dict[str, Any]] = [
        _entry("2026-10-01", a={"prompt_tokens": 9}),  # before the window
        _entry("2026-10-25", a={"prompt_tokens": 9}),  # after it
        _entry("garbage", a={"prompt_tokens": 9}),
        {"date": "2026-10-20"},  # no breakdown
        {"date": "2026-10-20", "breakdown": {"model_groups": []}},
        {"date": "2026-10-20", "breakdown": {"model_groups": {"": {"metrics": {}}, "x": 3}}},
        _entry("2026-10-20", ok={"prompt_tokens": 1}),
    ]

    usage = parse_usage(results, window)

    assert list(usage) == ["ok"]


def test_deployments_behind_one_name_are_one_model_and_prices_are_per_million() -> None:
    catalog = parse_catalog(
        [
            {"model_name": "Zeta", "model_info": {}},
            {
                "model_name": "gpt",
                "litellm_params": {"model": "openai/gpt", "api_base": "https://api.test/v1"},
                "model_info": {"mode": "chat", "input_cost_per_token": 1.5e-7},
            },
            {
                "model_name": "gpt",
                "litellm_params": {"model": "azure/gpt"},
                "model_info": {"output_cost_per_token": 6e-7, "mode": "other"},
            },
            {"model_name": "", "model_info": {}},
            {"nope": 1},
        ]
    )

    assert [entry.name for entry in catalog] == ["gpt", "Zeta"]
    gpt = catalog[0]
    assert (gpt.provider, gpt.host, gpt.mode, gpt.deployments) == ("openai", "api.test", "chat", 2)
    assert (gpt.input_per_million, gpt.output_per_million) == (0.15, 0.6)
    assert gpt.priced and not catalog[1].priced


def test_a_model_with_a_price_of_zero_or_none_is_not_priced() -> None:
    free, none, partial = parse_catalog(
        [
            {
                "model_name": "a",
                "model_info": {"input_cost_per_token": 0, "output_cost_per_token": 0},
            },
            {"model_name": "b", "model_info": {}},
            {"model_name": "c", "model_info": {"output_cost_per_token": 1e-6}},
        ]
    )

    assert not free.priced and not none.priced and partial.priced


# ── the report ──────────────────────────────────────────────────────────────────────────────


def test_up_to_seven_models_have_a_colour_each_by_name_and_it_does_not_depend_on_use() -> None:
    names = ["delta", "Alpha", "charlie", "bravo"]

    assert assign_slots(names, {}) == {"Alpha": 0, "bravo": 1, "charlie": 2, "delta": 3}
    # The same names with traffic on other days keep their colours.
    assert assign_slots(names, {"delta": (9, 9)}) == assign_slots(names, {})


def test_with_more_models_the_busiest_keep_a_colour_and_the_rest_are_other() -> None:
    names = [f"m{i}" for i in range(SLOTS + 3)]
    tokens = {name: (index, 0) for index, name in enumerate(names)}  # m9 is the busiest

    slots = assign_slots(names, tokens)

    assert len(slots) == SLOTS
    assert "m0" not in slots and "m9" in slots
    assert sorted(slots.values()) == list(range(SLOTS))
    assert list(slots) == sorted(slots)  # in name order


def test_the_report_has_a_row_for_every_served_model_and_for_every_one_that_was_used() -> None:
    window = Window("7d", date(2026, 10, 18), TODAY)
    catalog = parse_catalog(
        [{"model_name": "served", "model_info": {}}, {"model_name": "idle", "model_info": {}}]
    )
    usage = {
        "served": {TODAY: Metrics(1, 2, 0.5, 3, 1)},
        "gone": {TODAY - timedelta(days=2): Metrics(10, 20, 1.0, 1, 0)},
    }

    report = build_report(window, catalog, usage)

    assert [model.name for model in report.models] == ["gone", "idle", "served"]
    gone, idle, served = report.models
    assert not gone.configured and gone.used
    assert idle.configured and not idle.used and idle.totals == Metrics()
    assert served.configured and served.totals == Metrics(1, 2, 0.5, 3, 1)
    assert report.totals == Metrics(11, 22, 1.5, 4, 1)
    assert len(report.days) == 7 and len(report.daily_totals) == 7
    assert report.daily_totals[-1] == served.totals and report.daily_totals[4] == gone.totals
    assert report.model("idle") is idle and report.model("missing") is None
    assert all(len(model.daily) == 7 for model in report.models)


def test_the_type_of_a_model_is_worded_and_an_unknown_one_is_shown_as_it_is() -> None:
    assert mode_label("chat") == "Chat"
    assert mode_label("audio_speech") == "Speech"
    assert mode_label("video_generation") == "Video generation"


# ── reading, and keeping what was read ──────────────────────────────────────────────────────


def _window(days: int = 7) -> Window:
    """The last `days` days up to today."""
    return Window("custom", TODAY - timedelta(days=days - 1), TODAY)


async def test_a_reading_is_the_report_of_what_litellm_said(settings: Settings) -> None:
    fake = FakeLitellm()
    fake.use(TODAY, "gpt-4o-mini", prompt=10, completion=20, spend=0.5, ok=2)

    reading = await load_reading(settings, _window(), transport=fake.transport)

    assert reading.available and reading.problem is None and reading.database == "connected"
    assert reading.report is not None
    assert reading.report.totals == Metrics(10, 20, 0.5, 2, 0)
    assert fake.calls("/health/readiness") == 1


async def test_a_young_reading_is_served_again_and_an_old_one_is_read_anew(
    settings: Settings, clock: _Clock
) -> None:
    fake = FakeLitellm()

    first = await load_reading(settings, _window(), transport=fake.transport)
    clock.now += 59
    assert await load_reading(settings, _window(), transport=fake.transport) is first
    clock.now += 2
    second = await load_reading(settings, _window(), transport=fake.transport)

    assert second is not first
    assert fake.calls("/user/daily/activity") == 2


async def test_each_window_has_its_own_reading(settings: Settings) -> None:
    fake = FakeLitellm()

    await load_reading(settings, _window(7), transport=fake.transport)
    await load_reading(settings, _window(30), transport=fake.transport)
    await load_reading(settings, _window(7), transport=fake.transport)

    assert fake.calls("/user/daily/activity") == 2


async def test_asking_for_a_fresh_one_twice_in_a_row_reads_once_and_later_reads_again(
    settings: Settings, clock: _Clock
) -> None:
    fake = FakeLitellm()
    await load_reading(settings, _window(), transport=fake.transport)

    clock.now += 2
    await load_reading(settings, _window(), fresh=True, transport=fake.transport)
    assert fake.calls("/user/daily/activity") == 1  # folded into the one just made

    clock.now += 6
    await load_reading(settings, _window(), fresh=True, transport=fake.transport)
    assert fake.calls("/user/daily/activity") == 2


async def test_readers_who_ask_while_one_is_under_way_share_it(settings: Settings) -> None:
    fake = FakeLitellm()

    readings = await asyncio.gather(
        *(load_reading(settings, _window(), transport=fake.transport) for _ in range(5))
    )

    assert all(reading is readings[0] for reading in readings)
    assert fake.calls("/user/daily/activity") == 1


async def test_a_failure_is_a_reading_that_says_why_and_is_tried_again_sooner(
    settings: Settings, clock: _Clock
) -> None:
    fake = FakeLitellm()
    fake.fail = True

    failed = await load_reading(settings, _window(), transport=fake.transport)
    assert not failed.available and failed.report is None
    assert failed.problem is not None and failed.problem.msgid == REJECTED
    assert failed.problem.params == {"detail": "database is gone"}

    clock.now += 14
    assert await load_reading(settings, _window(), transport=fake.transport) is failed
    fake.fail = False
    clock.now += 2  # past the 15 seconds a failure is kept, far inside the 60 of a success
    assert (await load_reading(settings, _window(), transport=fake.transport)).available


async def test_a_litellm_that_is_down_costs_a_reading_not_an_exception(settings: Settings) -> None:
    fake = FakeLitellm()
    fake.down = True

    reading = await load_reading(settings, _window(), transport=fake.transport)

    assert not reading.available and reading.problem is not None
    assert reading.problem.params["where"] == "litellm:4000"


async def test_an_answer_that_does_not_add_up_is_unreadable(settings: Settings) -> None:
    fake = FakeLitellm()
    fake.garbage = True

    reading = await load_reading(settings, _window(), transport=fake.transport)

    assert reading.problem is not None and reading.problem.msgid == UNREADABLE


async def test_without_a_key_nobody_is_asked_anything(settings: Settings, tmp_path: Path) -> None:
    (tmp_path / "ai" / "litellm" / ".env").unlink()
    fake = FakeLitellm()

    reading = await load_reading(settings, _window(), transport=fake.transport)

    assert reading.problem is not None and reading.problem.msgid == NO_KEY
    assert fake.requests == []


async def test_the_cache_stays_small(settings: Settings) -> None:
    fake = FakeLitellm()
    for days in range(1, model_usage._MAX_CACHED + 6):
        await load_reading(settings, _window(days), transport=fake.transport)

    assert len(model_usage._cache) == model_usage._MAX_CACHED
