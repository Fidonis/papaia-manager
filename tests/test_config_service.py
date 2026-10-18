"""The jobs behind the Modules and Overlay tabs.

`setup` and `papaia-ctl` are replaced by recorders, so what is checked is the order of the steps,
what is passed to each, what a failure leaves behind, and that no overlay content reaches the job
log or the audit log. The real `setup` is run by `test_config_contract.py`.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest

from app.core import services
from app.core.audit import audit_path
from app.core.config import history, overlay
from app.core.config import service as svc
from app.core.ctl import CtlError, ModuleSetup
from app.core.jobs import Job, JobContext
from app.core.services import ServiceGroup, ServiceHealth, StackSnapshot

SECRET = "sk-very-secret-api-key"
OLD = f"endpoints:\n  custom:\n    - apiKey: {SECRET}\n".encode()
NEW = b"memory:\n  personalize: true\n"


class _Run:
    """The two ways out of this module, recorded in order."""

    def __init__(self) -> None:
        self.steps: list[tuple[str, Any]] = []
        self.setup_result: tuple[int, str, str] = (0, "Setup complete.", "")
        # verb name -> how many of its first calls fail
        self.fail: dict[str, int] = {}
        self.lines: dict[str, list[str]] = {}

    async def setup(self, spec: ModuleSetup, **kwargs: Any) -> tuple[int, str, str]:
        self.steps.append(("setup", spec))
        return self.setup_result

    async def verb(self, **kwargs: Any) -> AsyncGenerator[str, None]:
        verb = kwargs["verb"]
        self.steps.append((verb, list(kwargs["extra_flags"])))
        failing = self.fail.get(verb, 0) > 0
        if failing:
            self.fail[verb] -= 1

        async def lines() -> AsyncGenerator[str, None]:
            yield f"running {verb}"
            for line in self.lines.get(verb, []):
                yield line
            if failing:
                raise CtlError(f"{verb} failed", exit_code=3)

        return lines()

    def names(self) -> list[str]:
        return [name for name, _ in self.steps]


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch) -> _Run:
    recorder = _Run()
    monkeypatch.setattr(svc, "run_module_setup", recorder.setup)
    monkeypatch.setattr(svc, "run_core_verb", recorder.verb)
    return recorder


@pytest.fixture
def health(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """What the snapshot says about the group: tests change `state["health"]`."""
    state: dict[str, Any] = {"health": ServiceHealth.HEALTHY, "empty": False}

    def load(config_dir: str, workspace_dir: str) -> StackSnapshot:
        if state["empty"]:
            return StackSnapshot()
        return StackSnapshot(
            groups=[
                ServiceGroup(name=name, modules=(name,), containers=1, health=state["health"])
                for name in ("librechat", "localai", "librechat-websearch")
            ]
        )

    monkeypatch.setattr(services, "load_snapshot", load)
    monkeypatch.setattr(svc, "HEALTH_TIMEOUT_SECONDS", 0.0)
    monkeypatch.setattr(svc, "HEALTH_INTERVAL_SECONDS", 0.0)
    return state


def _config(tmp_path: Path, profiles: str = "nginx,librechat") -> str:
    config = tmp_path / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / ".env").write_text(f"COMPOSE_PROFILES={profiles}\n", encoding="utf-8")
    return str(config)


def _context(tmp_path: Path, action: str = "x") -> JobContext:
    job = Job(id="job-1", action=action, target="t", user="admin", created_at=None)  # type: ignore[arg-type]
    return JobContext(job=job, log_path=tmp_path / "job.log")


def _log(ctx: JobContext) -> str:
    return ctx.log_path.read_text(encoding="utf-8") if ctx.log_path.exists() else ""


def _audit(config: str) -> list[dict[str, Any]]:
    path = audit_path(config)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _go(callback: Any, ctx: JobContext) -> None:
    asyncio.run(callback(ctx))


def _overlay_job(
    config: str,
    *,
    content: bytes | None = NEW,
    expected: str | None = None,
    event: str = "apply",
    key: str = "librechat",
    version: str | None = None,
) -> Any:
    if expected is None:
        expected = overlay.read(config, key).revision
    return svc.overlay_callback(
        key=key,
        content=content,
        expected_revision=expected,
        event=event,
        user="admin",
        workspace_dir="/w",
        config_dir=config,
        version=version,
    )


def _seed(config: str, data: bytes = OLD, key: str = "librechat") -> None:
    assert overlay.write(config, key, data, expected_revision="") is not None


# -- modules -----------------------------------------------------------------------------------


def test_switching_localai_on_stores_the_choice_and_then_starts_it(
    tmp_path: Path, run: _Run, health: dict[str, Any]
) -> None:
    config = _config(tmp_path)
    ctx = _context(tmp_path)
    _go(
        svc.enable_callback(
            module="localai", variant="intel", user="admin", workspace_dir="/w", config_dir=config
        ),
        ctx,
    )
    assert run.steps == [
        ("setup", ModuleSetup("localai", True, "intel")),
        ("start", ["--profiles=localai"]),
    ]
    [entry] = _audit(config)
    assert (entry["action"], entry["target"], entry["result"]) == (
        "config.module.enable",
        "module:localai",
        "ok",
    )
    assert entry["params"]["variant"] == "intel"


def test_a_refusal_by_the_core_stops_before_anything_starts(
    tmp_path: Path, run: _Run, health: dict[str, Any]
) -> None:
    run.setup_result = (3, "", "ERROR: variant 'x' is not known\n")
    config = _config(tmp_path)
    with pytest.raises(svc.SetupRefusedError, match="variant 'x' is not known"):
        _go(
            svc.enable_callback(
                module="web-search",
                variant=None,
                user="admin",
                workspace_dir="/w",
                config_dir=config,
            ),
            _context(tmp_path),
        )
    assert run.names() == ["setup"]
    assert _audit(config) == []


def test_a_module_that_does_not_become_healthy_is_said_so_and_recorded_as_partial(
    tmp_path: Path, run: _Run, health: dict[str, Any]
) -> None:
    health["health"] = ServiceHealth.UNHEALTHY
    config = _config(tmp_path)
    with pytest.raises(svc.NotHealthyError, match="switched on, but the localai containers"):
        _go(
            svc.enable_callback(
                module="localai", variant="cpu", user="admin", workspace_dir="/w", config_dir=config
            ),
            _context(tmp_path),
        )
    assert [e["result"] for e in _audit(config)] == ["partial"]


def test_switching_a_module_off_stops_it_while_its_profile_is_still_active(
    tmp_path: Path, run: _Run
) -> None:
    config = _config(tmp_path, "nginx,localai")
    _go(
        svc.disable_callback(module="localai", user="admin", workspace_dir="/w", config_dir=config),
        _context(tmp_path),
    )
    assert run.steps == [
        ("stop", ["--clean-up", "--profiles=localai"]),
        ("setup", ModuleSetup("localai", False)),
    ]
    assert [e["action"] for e in _audit(config)] == ["config.module.disable"]


def test_switching_off_a_module_that_is_not_active_stops_nothing(tmp_path: Path, run: _Run) -> None:
    config = _config(tmp_path, "nginx")
    _go(
        svc.disable_callback(
            module="web-search", user="admin", workspace_dir="/w", config_dir=config
        ),
        _context(tmp_path),
    )
    assert run.names() == ["setup"]


def test_a_step_that_fails_names_what_it_complained_about(tmp_path: Path, run: _Run) -> None:
    run.fail["stop"] = 1
    run.lines["stop"] = ["noise", "ERROR: no such container", "more noise"]
    config = _config(tmp_path, "nginx,localai")
    with pytest.raises(svc.StepFailedError, match="no such container"):
        _go(
            svc.disable_callback(
                module="localai", user="admin", workspace_dir="/w", config_dir=config
            ),
            _context(tmp_path),
        )
    assert run.names() == ["stop"]


# -- overlay: apply ------------------------------------------------------------------------------


def test_apply_saves_the_old_file_writes_the_new_one_and_recreates_the_group(
    tmp_path: Path, run: _Run, health: dict[str, Any]
) -> None:
    config = _config(tmp_path)
    _seed(config)
    ctx = _context(tmp_path)
    _go(_overlay_job(config), ctx)

    assert overlay.read(config, "librechat").raw == NEW
    [version] = history.versions(config, "librechat")
    assert version.event == "apply"
    assert history.read(config, "librechat", version.id) == OLD
    assert run.steps == [
        ("stop", ["--clean-up", "--profiles=librechat"]),
        ("start", ["--profiles=librechat"]),
    ]
    [entry] = _audit(config)
    assert (entry["action"], entry["target"], entry["result"]) == (
        "config.overlay.apply",
        "overlay:librechat",
        "ok",
    )
    assert entry["params"]["restarted"] is True
    assert entry["params"]["version_saved"] == version.id


def test_the_first_overlay_has_nothing_to_save(
    tmp_path: Path, run: _Run, health: dict[str, Any]
) -> None:
    config = _config(tmp_path)
    ctx = _context(tmp_path)
    _go(_overlay_job(config), ctx)
    assert history.versions(config, "librechat") == []
    assert "there was no overlay file" in _log(ctx)
    assert overlay.read(config, "librechat").raw == NEW


def test_the_content_is_in_no_log_and_no_audit_entry(
    tmp_path: Path, run: _Run, health: dict[str, Any]
) -> None:
    config = _config(tmp_path)
    _seed(config)
    ctx = _context(tmp_path)
    _go(_overlay_job(config, content=f"apiKey: {SECRET}-new\n".encode()), ctx)
    assert SECRET not in _log(ctx)
    assert SECRET not in audit_path(config).read_text(encoding="utf-8")


def test_a_file_changed_on_the_host_stops_the_job_before_it_touches_anything(
    tmp_path: Path, run: _Run, health: dict[str, Any]
) -> None:
    config = _config(tmp_path)
    _seed(config)
    stale = overlay.read(config, "librechat").revision
    overlay.path_of(config, "librechat").write_bytes(b"edited: on-the-host\n")
    with pytest.raises(svc.ConflictError, match="changed after the page loaded it"):
        _go(_overlay_job(config, expected=stale), _context(tmp_path))
    assert overlay.read(config, "librechat").raw == b"edited: on-the-host\n"
    assert history.versions(config, "librechat") == []
    assert run.steps == []


def test_a_group_that_is_not_switched_on_gets_the_file_and_no_restart(
    tmp_path: Path, run: _Run, health: dict[str, Any]
) -> None:
    config = _config(tmp_path, "nginx")
    ctx = _context(tmp_path)
    _go(_overlay_job(config, key="searxng"), ctx)
    assert overlay.read(config, "searxng").raw == NEW
    assert run.steps == []
    assert "applies the next time it starts" in _log(ctx)
    [entry] = _audit(config)
    assert entry["params"]["restarted"] is False


def test_a_start_that_fails_puts_the_previous_file_back_and_starts_the_group_again(
    tmp_path: Path, run: _Run, health: dict[str, Any]
) -> None:
    config = _config(tmp_path)
    _seed(config)
    run.fail["start"] = 1
    run.lines["start"] = ["ERROR: render failed"]
    ctx = _context(tmp_path)
    with pytest.raises(svc.RolledBackError, match="previous version was put back"):
        _go(_overlay_job(config), ctx)
    assert overlay.read(config, "librechat").raw == OLD
    assert run.names() == ["stop", "start", "start"]
    assert "the group was started with the previous overlay" in _log(ctx)
    actions = [(e["action"], e["result"]) for e in _audit(config)]
    assert actions == [("config.overlay.rollback.auto", "error")]
    # The failed attempt is in the history too: the new file is what was lost.
    assert len(history.versions(config, "librechat")) == 1


def test_a_start_that_fails_after_a_first_overlay_removes_it_again(
    tmp_path: Path, run: _Run, health: dict[str, Any]
) -> None:
    config = _config(tmp_path)
    run.fail["start"] = 1
    with pytest.raises(svc.RolledBackError):
        _go(_overlay_job(config), _context(tmp_path))
    assert not overlay.path_of(config, "librechat").exists()


def test_a_stop_that_fails_is_rolled_back_as_well(
    tmp_path: Path, run: _Run, health: dict[str, Any]
) -> None:
    config = _config(tmp_path)
    _seed(config)
    run.fail["stop"] = 1
    with pytest.raises(svc.RolledBackError):
        _go(_overlay_job(config), _context(tmp_path))
    assert overlay.read(config, "librechat").raw == OLD
    assert run.names() == ["stop", "start"]


def test_a_second_start_that_fails_too_is_reported_and_the_file_is_still_restored(
    tmp_path: Path, run: _Run, health: dict[str, Any]
) -> None:
    config = _config(tmp_path)
    _seed(config)
    run.fail["start"] = 2
    ctx = _context(tmp_path)
    with pytest.raises(svc.RolledBackError):
        _go(_overlay_job(config), ctx)
    assert overlay.read(config, "librechat").raw == OLD
    assert "did not start again either" in _log(ctx)


def test_a_file_edited_during_the_rollback_window_is_left_alone(
    tmp_path: Path, run: _Run, health: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    _seed(config)
    run.fail["start"] = 1
    original = run.verb

    async def edit_then_run(**kwargs: Any) -> AsyncGenerator[str, None]:
        if kwargs["verb"] == "start" and run.fail["start"] == 1:
            overlay.path_of(config, "librechat").write_bytes(b"hand: edit\n")
        return await original(**kwargs)

    monkeypatch.setattr(svc, "run_core_verb", edit_then_run)
    ctx = _context(tmp_path)
    with pytest.raises(svc.RolledBackError):
        _go(_overlay_job(config), ctx)
    assert overlay.read(config, "librechat").raw == b"hand: edit\n"
    assert "left as it is" in _log(ctx)


def test_a_group_that_starts_but_is_not_healthy_keeps_the_file_and_says_so(
    tmp_path: Path, run: _Run, health: dict[str, Any]
) -> None:
    health["health"] = ServiceHealth.STOPPED
    config = _config(tmp_path)
    _seed(config)
    with pytest.raises(svc.NotHealthyError, match="Versions"):
        _go(_overlay_job(config), _context(tmp_path))
    assert overlay.read(config, "librechat").raw == NEW
    assert [e["result"] for e in _audit(config)] == ["partial"]


def test_an_unreadable_docker_does_not_fail_the_job(
    tmp_path: Path, run: _Run, health: dict[str, Any]
) -> None:
    health["empty"] = True
    config = _config(tmp_path)
    ctx = _context(tmp_path)
    _go(_overlay_job(config), ctx)
    assert "health not checked" in _log(ctx)


# -- overlay: discard and rollback ---------------------------------------------------------------


def test_discard_removes_the_file_after_saving_it(
    tmp_path: Path, run: _Run, health: dict[str, Any]
) -> None:
    config = _config(tmp_path)
    _seed(config)
    _go(_overlay_job(config, content=None, event="discard"), _context(tmp_path))
    assert not overlay.path_of(config, "librechat").exists()
    [version] = history.versions(config, "librechat")
    assert version.event == "discard"
    assert history.read(config, "librechat", version.id) == OLD
    assert run.names() == ["stop", "start"]
    assert [e["action"] for e in _audit(config)] == ["config.overlay.discard"]


def test_a_rollback_saves_the_current_file_and_names_the_version_it_restored(
    tmp_path: Path, run: _Run, health: dict[str, Any]
) -> None:
    config = _config(tmp_path)
    _seed(config, NEW)
    old_id = history.save(config, "librechat", OLD, "apply")
    _go(
        _overlay_job(config, content=OLD, event="rollback", version=old_id),
        _context(tmp_path),
    )
    assert overlay.read(config, "librechat").raw == OLD
    events = sorted(v.event for v in history.versions(config, "librechat"))
    assert events == ["apply", "rollback"]
    [entry] = _audit(config)
    assert entry["action"] == "config.overlay.rollback"
    assert entry["params"]["version_restored"] == old_id


# -- the health check -----------------------------------------------------------------------------


def test_a_group_that_is_starting_is_waited_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    states = iter([ServiceHealth.STARTING, ServiceHealth.STARTING, ServiceHealth.HEALTHY])

    def load(config_dir: str, workspace_dir: str) -> StackSnapshot:
        return StackSnapshot(groups=[ServiceGroup("librechat", ("librechat",), 1, next(states))])

    monkeypatch.setattr(services, "load_snapshot", load)
    ctx = _context(tmp_path)
    problem = asyncio.run(
        svc.wait_healthy(
            ctx, "librechat", config_dir="/c", workspace_dir="/w", limit=5.0, interval=0.0
        )
    )
    assert problem is None
    assert _log(ctx).count("waiting") == 2


def test_a_group_that_never_shows_up_is_not_healthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        services,
        "load_snapshot",
        lambda c, w: StackSnapshot(
            groups=[ServiceGroup("other", ("o",), 1, ServiceHealth.HEALTHY)]
        ),
    )
    problem = asyncio.run(
        svc.wait_healthy(
            _context(tmp_path), "librechat", config_dir="/c", workspace_dir="/w", limit=0.0
        )
    )
    assert problem == "The librechat containers are not reported."


def test_the_core_sentence_is_picked_out_of_setups_output() -> None:
    assert svc.refusal(3, "ok\n", "noise\nERROR: nope\n") == "nope"
    assert svc.refusal(1, "first line\n", "") == "first line"
    assert svc.refusal(2, "", "") == "setup exited with status 2"
    assert svc.last_problem(["a", "", "ERROR: boom", "tail"]) == "boom"
    assert svc.last_problem(["a", "tail"]) == "tail"
    assert svc.last_problem([]) == "the step failed"
