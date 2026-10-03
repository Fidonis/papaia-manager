"""Docker usage: what Docker's data takes, measured apart from the rest of the Host page.

Same method as the host health tests: the parser against documents shaped like the
core's real `doctor --json`, the runner against a stand-in for `run_py_cli`, and
the clock replaced where a test is about time. What is specific here is the cost
model. This is the one check that makes the daemon work, so it must be measured
rarely, never make a visitor wait more than once, and never be able to hold up or
break the host reading.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.core import docker_usage, host_health
from app.core.ctl import CtlError
from app.core.docker_usage import DockerUsage, format_decimal_bytes, parse_docker_usage

_CORE_CHECKS = (
    "docker_version",
    "disk_space",
    "docker_usage",
    "memory",
    "cpu",
    "gpu",
    "ports",
    "dns",
    "time_sync",
    "certs",
    "addon_compat",
    "container_health",
)


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    host_health.reset_cache()
    yield
    host_health.reset_cache()


def _registry(*names: str) -> str:
    """The part of the core's `lib/doctor.py` that lists its checks."""
    body = "".join(f'    ("{name}", check_{name}),\n' for name in names)
    return f"CHECKS: list[tuple[str, Callable[[Context], CheckResult]]] = [\n{body}]\n"


def _entry(count: int, active: int, size: int, reclaimable: int | None) -> dict[str, Any]:
    return {
        "count": count,
        "active": active,
        "size_bytes": size,
        "reclaimable_bytes": reclaimable,
    }


def _types() -> dict[str, Any]:
    return {
        "images": _entry(53, 31, 50_460_000_000, 14_080_000_000),
        "containers": _entry(32, 31, 235_900_000, 16_380),
        "volumes": _entry(75, 22, 26_080_000_000, 23_460_000_000),
        "build_cache": _entry(897, 0, 25_530_000_000, 20_730_000_000),
    }


def _doc(
    *,
    status: str = "pass",
    summary: str = "Docker uses 102.3GB",
    types: Any = None,
    schema_version: int = 1,
) -> str:
    details = {} if status != "pass" else {"types": _types() if types is None else types}
    return json.dumps(
        {
            "schema_version": schema_version,
            "generated_at": "2026-10-03T12:00:00Z",
            "platform_version": "1.4.0",
            "checks": [
                {"name": "docker_usage", "status": status, "summary": summary, "details": details}
            ],
            "summary": {"pass": 1, "warn": 0, "fail": 0, "skip": 0},
            "ok": True,
        }
    )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_a_reading_has_a_row_per_docker_type_in_dockers_order() -> None:
    usage = parse_docker_usage(_doc())

    assert usage.available
    assert [r.label for r in usage.rows] == ["Images", "Containers", "Volumes", "Build cache"]
    assert [r.key for r in usage.rows] == ["images", "containers", "volumes", "build_cache"]


def test_sizes_and_the_reclaimable_part_read_the_way_docker_prints_them() -> None:
    usage = parse_docker_usage(_doc())
    images, containers, volumes, cache = usage.rows

    assert (images.size_text, images.reclaimable_text) == ("50.46 GB", "14.08 GB reclaimable")
    assert (containers.size_text, containers.reclaimable_text) == (
        "235.9 MB",
        "16.38 kB reclaimable",
    )
    assert (volumes.size_text, cache.size_text) == ("26.08 GB", "25.53 GB")
    assert (usage.total_text, usage.reclaimable_text) == ("102.3 GB", "58.27 GB")
    assert usage.total_bytes == 102_305_900_000


def test_a_row_says_how_many_there_are_and_how_many_are_active() -> None:
    images, _, volumes, cache = (parse_docker_usage(_doc()).rows[i] for i in (0, 1, 2, 3))

    assert images.detail == "53 total · 31 active"
    assert volumes.detail == "75 total · 22 active"
    assert cache.detail == "897 total · 0 active"


def test_the_bar_is_the_rows_share_of_everything_docker_holds() -> None:
    usage = parse_docker_usage(_doc())

    assert [r.percent for r in usage.rows] == [49.3, 0.2, 25.5, 25.0]
    assert sum(r.percent for r in usage.rows) == pytest.approx(100.0, abs=0.5)


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (0, "0 B"),
        (999, "999 B"),
        (16_380, "16.38 kB"),
        (235_900_000, "235.9 MB"),
        (50_460_000_000, "50.46 GB"),
        (102_305_900_000, "102.3 GB"),
        (1_500_000_000_000, "1.5 TB"),
    ],
)
def test_sizes_are_decimal_with_four_significant_digits_like_docker(value: int, text: str) -> None:
    assert format_decimal_bytes(value) == text


def test_a_skip_from_the_core_is_a_reading_with_its_reason() -> None:
    usage = parse_docker_usage(_doc(status="skip", summary="docker system df timed out after 20s"))

    assert not usage.available
    assert usage.reason == "docker system df timed out after 20s"
    assert usage.rows == ()


def test_a_skip_without_a_reason_still_says_something() -> None:
    assert parse_docker_usage(_doc(status="skip", summary="")).reason


@pytest.mark.parametrize(
    ("stdout", "reason"),
    [
        ("", "did not return JSON"),
        ("Traceback (most recent call last):", "did not return JSON"),
        ("[1, 2]", "not return a JSON object"),
        (_doc(schema_version=2), "schema 2"),
        (json.dumps({"schema_version": 1, "checks": []}), "did not report docker_usage"),
        (json.dumps({"schema_version": 1, "checks": "nope"}), "did not report docker_usage"),
        (_doc(types={}), "without figures"),
        (_doc(types="nope"), "without figures"),
        (_doc(types={"images": {"size_bytes": "big"}, "volumes": 3}), "without figures"),
    ],
)
def test_an_unusable_document_is_unavailable_with_a_reason(stdout: str, reason: str) -> None:
    usage = parse_docker_usage(stdout)

    assert not usage.available
    assert reason in usage.reason


def test_rows_that_do_not_fit_are_dropped_and_the_rest_is_kept() -> None:
    types = _types()
    types["plugins"] = _entry(1, 1, 5_000, 0)  # a row Docker adds later
    types["volumes"] = {"size_bytes": True, "count": 75}  # a JSON `true` is not a size
    types["containers"] = "nope"
    types["build_cache"] = {"size_bytes": 1_000_000_000, "count": "x", "reclaimable_bytes": None}

    usage = parse_docker_usage(_doc(types=types))

    assert [r.key for r in usage.rows] == ["images", "build_cache"]
    cache = usage.rows[1]
    # A count that is not a number is unknown, not zero, and says so by saying nothing.
    assert (cache.count, cache.active, cache.reclaimable_bytes) == (None, None, None)
    assert (cache.detail, cache.reclaimable_text) == ("", "")
    assert usage.total_bytes == 51_460_000_000


# ---------------------------------------------------------------------------
# What the core knows
# ---------------------------------------------------------------------------


def _core(tmp_path: Path, text: str | None) -> str:
    """A workspace whose core has a `doctor` with this source, or none at all."""
    workspace = tmp_path / "workspace"
    lib = workspace / "papaia" / "tools" / "lib"
    lib.mkdir(parents=True)
    if text is not None:
        (lib / "doctor.py").write_text(text, encoding="utf-8")
    return str(workspace)


def test_the_cores_checks_are_read_from_its_registry_in_order(tmp_path: Path) -> None:
    workspace = _core(tmp_path, '"""Preflight."""\n\n' + _registry(*_CORE_CHECKS))

    assert docker_usage.core_checks(workspace) == _CORE_CHECKS
    assert docker_usage.core_has_usage(workspace)


def test_a_core_before_the_check_does_not_have_it(tmp_path: Path) -> None:
    older = tuple(n for n in _CORE_CHECKS if n != "docker_usage")
    workspace = _core(tmp_path, _registry(*older))

    assert docker_usage.core_checks(workspace) == older
    assert not docker_usage.core_has_usage(workspace)


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        # The name in prose or a comment is not the registry, and must not make a
        # core look as if it knew a check it would then refuse to skip.
        "# docker_usage is planned\nCHECKS = []\n",
        'def f():\n    return "docker_usage"\n',
    ],
)
def test_without_a_registry_entry_the_core_is_taken_not_to_have_it(
    tmp_path: Path, text: str | None
) -> None:
    workspace = _core(tmp_path, text)

    assert not docker_usage.core_has_usage(workspace)


def test_a_missing_workspace_has_no_checks(tmp_path: Path) -> None:
    assert docker_usage.core_checks(str(tmp_path / "nowhere")) == ()


# ---------------------------------------------------------------------------
# Pace
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("interval", "expected"),
    [
        (10, 300.0),  # never sooner than five minutes
        (30, 300.0),
        (60, 600.0),  # the default: ten times a minute
        (300, 3000.0),
        (360, 3600.0),
        (600, 3600.0),  # and never later than an hour...
        (3600, 3600.0),  # ...unless the interval itself is that slow
    ],
)
def test_docker_usage_is_measured_every_tenth_interval_between_five_minutes_and_an_hour(
    interval: int, expected: float
) -> None:
    assert docker_usage.usage_interval(interval) == expected


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


class _Doctor:
    """A stand-in for `run_py_cli` that records how it was called."""

    def __init__(
        self,
        stdout: str | None = None,
        *,
        code: int = 0,
        stderr: str = "",
        error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self.stdout = _doc() if stdout is None else stdout
        self.code, self.stderr, self.error, self.delay = code, stderr, error, delay
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> tuple[int, str, str]:
        self.calls.append(kwargs)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.code, self.stdout, self.stderr


@pytest.fixture
def workspace(tmp_path: Path) -> str:
    """A workspace whose core has the `docker_usage` check."""
    return _core(tmp_path, _registry(*_CORE_CHECKS))


@pytest.fixture
def doctor(monkeypatch: pytest.MonkeyPatch) -> _Doctor:
    stub = _Doctor()
    monkeypatch.setattr(docker_usage, "run_py_cli", stub)
    return stub


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The cache's notion of now, advanced by hand (the module's `time` only: patching
    `time.monotonic` itself would stop the event loop's own clock)."""
    now = [1000.0]
    monkeypatch.setattr(docker_usage, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


async def _load(
    workspace: str, *, force: bool = False, config_dir: str = "/cfg"
) -> DockerUsage | None:
    return await docker_usage.load_docker_usage(
        config_dir=config_dir, workspace_dir=workspace, force=force
    )


async def _settle() -> None:
    """Let a run started behind a reading finish."""
    await asyncio.sleep(0.05)


def _set_interval(tmp_path: Path, seconds: int) -> str:
    config = tmp_path / "config"
    (config / "manager").mkdir(parents=True, exist_ok=True)
    (config / "manager" / "settings.yaml").write_text(
        f"host:\n  refresh_seconds: {seconds}\n", encoding="utf-8"
    )
    return str(config)


async def test_the_run_asks_for_this_check_alone_bounded(workspace: str, doctor: _Doctor) -> None:
    usage = await _load(workspace)

    assert usage is not None and usage.available
    (call,) = doctor.calls
    assert call["command"] == "doctor"
    assert call["config_dir"] == "/cfg"
    assert call["limit"] == docker_usage.USAGE_RUN_TIMEOUT_SECONDS
    flags = call["extra_flags"]
    assert flags[0] == "--json"
    # Everything the core's registry holds except `docker_usage`: `doctor` has no
    # "only", so this is what makes the run do one thing.
    skipped = flags[1].removeprefix("--skip=").split(",")
    assert skipped == [n for n in _CORE_CHECKS if n != "docker_usage"]


async def test_a_core_without_the_check_is_left_alone_and_nothing_is_run(
    tmp_path: Path, doctor: _Doctor
) -> None:
    older = _core(tmp_path, _registry(*(n for n in _CORE_CHECKS if n != "docker_usage")))

    assert await _load(older) is None
    assert doctor.calls == []


async def test_a_core_without_doctor_is_left_alone_too(tmp_path: Path, doctor: _Doctor) -> None:
    assert await _load(_core(tmp_path, None)) is None
    assert doctor.calls == []


async def test_a_reading_is_reused_until_its_interval_runs_out(
    workspace: str, doctor: _Doctor, clock: list[float]
) -> None:
    first = await _load(workspace)
    clock[0] += docker_usage.usage_interval(60) - 1

    assert await _load(workspace) is first
    assert len(doctor.calls) == 1


async def test_a_stale_reading_is_shown_at_once_and_replaced_behind_the_visitor(
    workspace: str, doctor: _Doctor, clock: list[float]
) -> None:
    first = await _load(workspace)
    clock[0] += docker_usage.usage_interval(60) + 1
    doctor.stdout = _doc(types={"images": _entry(1, 1, 7_000_000_000, 0)})
    doctor.delay = 0.05

    shown = await _load(workspace)

    # The visitor got the old reading without waiting for the new run...
    assert shown is first
    await asyncio.sleep(0.01)
    assert len(doctor.calls) == 2
    # ...and the next one finds the new one.
    await asyncio.sleep(0.15)
    refreshed = await _load(workspace)
    assert refreshed is not first
    assert refreshed is not None and refreshed.total_bytes == 7_000_000_000
    assert len(doctor.calls) == 2


async def test_nothing_to_show_yet_means_waiting_for_the_run_once(
    workspace: str, doctor: _Doctor
) -> None:
    doctor.delay = 0.02

    usage = await _load(workspace)

    assert usage is not None and usage.available
    assert len(doctor.calls) == 1


async def test_concurrent_visitors_share_one_run(workspace: str, doctor: _Doctor) -> None:
    doctor.delay = 0.05

    readings = await asyncio.gather(*(_load(workspace) for _ in range(8)))

    assert len(doctor.calls) == 1
    assert all(r is readings[0] for r in readings)


async def test_a_visitor_who_leaves_does_not_cancel_the_run_for_the_next(
    workspace: str, doctor: _Doctor
) -> None:
    doctor.delay = 0.05
    leaver = asyncio.ensure_future(_load(workspace))
    await asyncio.sleep(0.01)
    stayer = asyncio.ensure_future(_load(workspace))
    await asyncio.sleep(0.01)

    leaver.cancel()
    usage = await stayer

    assert usage is not None and usage.available
    assert len(doctor.calls) == 1


async def test_recheck_waits_for_a_new_run_but_not_twice_in_half_a_minute(
    workspace: str, doctor: _Doctor, clock: list[float]
) -> None:
    first = await _load(workspace)

    clock[0] += docker_usage.USAGE_RECHECK_MIN_INTERVAL_SECONDS - 1
    assert await _load(workspace, force=True) is first
    assert len(doctor.calls) == 1

    clock[0] += 2
    doctor.stdout = _doc(types={"images": _entry(1, 1, 9_000_000_000, 0)})
    again = await _load(workspace, force=True)
    assert again is not first
    assert again is not None and again.total_bytes == 9_000_000_000
    assert len(doctor.calls) == 2


async def test_the_interval_from_settings_sets_the_pace(
    workspace: str, doctor: _Doctor, clock: list[float], tmp_path: Path
) -> None:
    config = _set_interval(tmp_path, 10)  # five minutes for this check
    first = await _load(workspace, config_dir=config)

    clock[0] += 299
    assert await _load(workspace, config_dir=config) is first
    assert len(doctor.calls) == 1

    clock[0] += 2
    await _load(workspace, config_dir=config)
    await _settle()
    assert len(doctor.calls) == 2


async def test_a_failed_reading_is_tried_again_sooner_than_a_good_one(
    workspace: str, doctor: _Doctor, clock: list[float]
) -> None:
    doctor.stdout = _doc(status="skip", summary="docker system df failed: no daemon")
    failed = await _load(workspace)
    assert failed is not None and not failed.available

    clock[0] += docker_usage.USAGE_FAILURE_TTL_SECONDS + 1
    doctor.stdout = _doc()
    stale = await _load(workspace)
    await _settle()
    recovered = await _load(workspace)

    assert stale is failed
    assert recovered is not None and recovered.available
    assert len(doctor.calls) == 2


# ---------------------------------------------------------------------------
# Never the page's problem
# ---------------------------------------------------------------------------


async def test_a_timeout_or_a_missing_interpreter_is_a_reading_not_an_exception(
    workspace: str, doctor: _Doctor
) -> None:
    doctor.error = CtlError("python3 -m lib.cli doctor timed out after 30s", exit_code=124)

    usage = await _load(workspace)

    assert usage is not None and not usage.available
    assert "timed out after 30s" in usage.reason


async def test_exit_two_with_nothing_on_stdout_is_a_refusal_with_stderrs_reason(
    workspace: str, doctor: _Doctor
) -> None:
    doctor.stdout, doctor.code = "", 2
    doctor.stderr = "ERROR: Unknown check(s): a_check_that_went_away.\n"

    usage = await _load(workspace)

    assert usage is not None and not usage.available
    assert usage.reason.startswith("ERROR: Unknown check(s)")


async def test_nothing_on_stdout_and_nothing_on_stderr_still_has_a_reason(
    workspace: str, doctor: _Doctor
) -> None:
    doctor.stdout, doctor.code = "", 1

    usage = await _load(workspace)

    assert usage is not None
    assert "status 1" in usage.reason


async def test_an_unexpected_error_inside_the_run_still_yields_a_reading(
    workspace: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def explode(**_: Any) -> tuple[int, str, str]:
        raise RuntimeError("boom")

    monkeypatch.setattr(docker_usage, "run_py_cli", explode)

    usage = await _load(workspace)

    assert usage is not None and not usage.available
    assert "unexpectedly" in usage.reason


async def test_a_core_that_skipped_the_check_reaches_the_page_as_a_reason(
    workspace: str, doctor: _Doctor
) -> None:
    doctor.stdout = _doc(status="skip", summary="docker not found")

    usage = await _load(workspace)

    assert usage is not None
    assert (usage.available, usage.reason) == (False, "docker not found")
