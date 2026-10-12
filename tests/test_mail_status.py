"""The relay's status, as the core's `doctor` reports it.

The reading is taken in a `doctor` of its own with every other check skipped, because the host
health poll no longer runs the `smtp` check (it does `docker exec` into the relay). What is
pinned: what is asked for, what is made of the answer, and that a failure is a reading.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.core import host_health
from app.core.ctl import CtlError
from app.core.mail import status as mail_status
from app.core.mail.status import RelayStatus, parse_status

_REGISTRY = """
CHECKS = [
    ("docker_version", check_docker_version),
    ("ports", check_ports),
    ("disk_space", check_disk_space),
    ("smtp", check_smtp),
    ("docker_usage", check_docker_usage),
]
"""


def _document(*checks: dict[str, Any], schema: int = 1) -> str:
    return json.dumps({"schema_version": schema, "checks": list(checks)})


def _smtp(
    status: str = "pass", summary: str = "relay running; queue empty", **details: Any
) -> dict[str, Any]:
    return {"name": "smtp", "status": status, "summary": summary, "details": details}


@pytest.fixture(autouse=True)
def _fresh_cache() -> Any:
    mail_status.reset_cache()
    yield
    mail_status.reset_cache()


def _workspace(tmp_path: Path, registry: str = _REGISTRY) -> str:
    lib = tmp_path / "papaia" / "tools" / "lib"
    lib.mkdir(parents=True)
    (lib / "doctor.py").write_text(registry, encoding="utf-8")
    return str(tmp_path)


# -- reading the answer --------------------------------------------------------------


def test_a_passing_check_is_a_reading_with_the_relay_and_its_queue() -> None:
    reading = parse_status(_document(_smtp(relay="running", queued=0)))
    assert reading.available
    assert (reading.verdict, reading.relay, reading.queued) == ("pass", "running", 0)
    assert reading.summary == "relay running; queue empty"
    assert reading.checked_at is not None


def test_a_warning_carries_the_cores_sentence_and_the_queue_length() -> None:
    reading = parse_status(
        _document(
            _smtp("warn", "3 message(s) waiting in the relay's queue", relay="running", queued=3)
        )
    )
    assert (reading.verdict, reading.queued) == ("warn", 3)
    assert "3 message(s)" in reading.summary


def test_only_the_smtp_check_is_taken_whatever_else_the_document_carries() -> None:
    other = {"name": "disk_space", "status": "fail", "summary": "full", "details": {}}
    reading = parse_status(_document(other, _smtp(relay="running", queued=1)))
    assert reading.verdict == "pass" and reading.queued == 1


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        "not json",
        "[]",
        json.dumps({"schema_version": 2, "checks": []}),
        _document(),
        _document({"name": "ports", "status": "pass", "summary": "", "details": {}}),
    ],
)
def test_an_answer_that_cannot_be_used_is_a_reading_with_a_reason(stdout: str) -> None:
    reading = parse_status(stdout)
    assert not reading.available
    assert reading.reason


def test_a_boolean_is_not_a_queue_length() -> None:
    assert parse_status(_document(_smtp(relay="running", queued=True))).queued is None


# -- asking for it -------------------------------------------------------------------


async def test_the_run_skips_every_other_check_of_the_cores_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    async def fake(**kwargs: Any) -> tuple[int, str, str]:
        seen.update(kwargs)
        return 0, _document(_smtp(relay="running", queued=0)), ""

    monkeypatch.setattr(mail_status, "run_py_cli", fake)
    reading = await mail_status.load_status(config_dir="/c", workspace_dir=_workspace(tmp_path))

    assert reading.available
    assert seen["command"] == "doctor"
    flags = seen["extra_flags"]
    assert flags[0] == "--json"
    skipped = flags[1].removeprefix("--skip=").split(",")
    assert sorted(skipped) == ["disk_space", "docker_usage", "docker_version", "ports"]
    assert "smtp" not in skipped
    assert seen["limit"] == mail_status.STATUS_RUN_TIMEOUT_SECONDS


async def test_a_core_without_the_check_is_never_asked_for_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake(**_: Any) -> tuple[int, str, str]:  # pragma: no cover - must not run
        raise AssertionError("doctor must not be run")

    monkeypatch.setattr(mail_status, "run_py_cli", fake)
    workspace = _workspace(tmp_path, '    ("disk_space", check_disk_space),\n')
    reading = await mail_status.load_status(config_dir="/c", workspace_dir=workspace)
    assert not reading.available
    assert "no smtp check" in reading.reason


async def test_a_doctor_that_fails_to_run_is_a_reading_not_an_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake(**_: Any) -> tuple[int, str, str]:
        raise CtlError("python3 -m lib.cli doctor timed out after 30s", exit_code=124)

    monkeypatch.setattr(mail_status, "run_py_cli", fake)
    reading = await mail_status.load_status(config_dir="/c", workspace_dir=_workspace(tmp_path))
    assert not reading.available
    assert "timed out" in reading.reason


async def test_an_empty_answer_is_explained_by_the_first_line_of_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake(**_: Any) -> tuple[int, str, str]:
        return 2, "", "\nusage: doctor: error: unknown check 'smtp'\n"

    monkeypatch.setattr(mail_status, "run_py_cli", fake)
    reading = await mail_status.load_status(config_dir="/c", workspace_dir=_workspace(tmp_path))
    assert reading.reason == "usage: doctor: error: unknown check 'smtp'"


async def test_a_reading_is_served_from_the_cache_and_a_recheck_shortens_it_to_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = 0
    clock = [1000.0]

    async def fake(**_: Any) -> tuple[int, str, str]:
        nonlocal runs
        runs += 1
        return 0, _document(_smtp(relay="running", queued=runs)), ""

    monkeypatch.setattr(mail_status, "run_py_cli", fake)
    # The module's own name for `time`, not the global one: the event loop reads that too.
    monkeypatch.setattr(mail_status, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    workspace = _workspace(tmp_path)

    first = await mail_status.load_status(config_dir="/c", workspace_dir=workspace)
    again = await mail_status.load_status(config_dir="/c", workspace_dir=workspace)
    assert (runs, first.queued, again.queued) == (1, 1, 1)

    clock[0] += 1.0  # "Re-check" a second later: folded into the reading just taken
    assert (
        await mail_status.load_status(config_dir="/c", workspace_dir=workspace, force=True)
    ).queued == 1

    clock[0] += 4.0  # past the re-check minimum, inside the ordinary TTL
    assert (await mail_status.load_status(config_dir="/c", workspace_dir=workspace)).queued == 1
    assert (
        await mail_status.load_status(config_dir="/c", workspace_dir=workspace, force=True)
    ).queued == 2

    clock[0] += mail_status.STATUS_TTL_SECONDS + 1
    assert (await mail_status.load_status(config_dir="/c", workspace_dir=workspace)).queued == 3
    assert runs == 3


async def test_concurrent_callers_share_one_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio  # noqa: PLC0415

    runs = 0

    async def fake(**_: Any) -> tuple[int, str, str]:
        nonlocal runs
        runs += 1
        await asyncio.sleep(0.05)
        return 0, _document(_smtp(relay="running", queued=0)), ""

    monkeypatch.setattr(mail_status, "run_py_cli", fake)
    workspace = _workspace(tmp_path)
    readings = await asyncio.gather(
        *(mail_status.load_status(config_dir="/c", workspace_dir=workspace) for _ in range(5))
    )
    assert runs == 1
    assert all(isinstance(r, RelayStatus) and r.available for r in readings)


# -- the host health poll ------------------------------------------------------------


def test_the_host_poll_skips_the_smtp_check_where_the_core_has_it(tmp_path: Path) -> None:
    skipped = host_health._skipped(_workspace(tmp_path))
    assert "smtp" in skipped
    assert "docker_usage" in skipped
    assert set(host_health._SKIPPED_CHECKS) <= set(skipped)


def test_the_host_poll_never_names_a_check_the_core_does_not_know(tmp_path: Path) -> None:
    # `--skip` refuses an unknown name and the whole run with it.
    workspace = _workspace(tmp_path, '    ("disk_space", check_disk_space),\n')
    assert host_health._skipped(workspace) == host_health._SKIPPED_CHECKS


def test_a_workspace_without_doctor_asks_for_nothing_extra(tmp_path: Path) -> None:
    assert host_health._skipped(str(tmp_path)) == host_health._SKIPPED_CHECKS
