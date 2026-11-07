"""Modules, overlays and the mail relay on a distributed installation.

The core's own reading of the placement (`lib.topology`) is replaced by a stand-in with the
same three calls, so nothing here needs a core checkout. `papaia-ctl` is a recorder, as in
`test_config_service.py`. A single host keeps one run with the flags as they were.
"""
from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from app.core import host_health, nodes
from app.core.config import service as svc
from app.core.jobs import Job, JobContext
from app.core.mail import service as mail_service
from app.core.mail import status as mail_status

TOPOLOGY = {
    "topology": {
        "mode": "distributed",
        "controller": "internal",
        "nodes": {
            "internal": {"address": "10.0.2.10", "executor": "local"},
            "edge": {"address": "10.0.1.10", "executor": "ssh"},
            "gpu": {"address": "10.0.3.10", "executor": "ssh"},
        },
        "placement": {"nginx": "edge", "localai": "gpu", "smtp": "edge"},
    }
}

# The profiles of the core's catalogue services that these tests use.
_CATALOGUE = {
    "nginx": ("nginx", "oauth2-proxy"),
    "keycloak": ("keycloak",),
    "librechat": ("librechat",),
    "localai": ("localai",),
    "rag": ("rag",),
    "smtp": ("smtp",),
}


def _fake_placement(config_dir: str) -> Any:
    placement = TOPOLOGY["topology"]["placement"]

    def service_of_profile(profile: str) -> str | None:
        return next((name for name, ps in _CATALOGUE.items() if profile in ps), None)

    def active_services(profiles: set[str]) -> list[str]:
        return [name for name, ps in _CATALOGUE.items() if set(ps) & set(profiles)]

    topology = SimpleNamespace(
        service_of_profile=service_of_profile, active_services=active_services
    )
    topo = SimpleNamespace(node_of=lambda service: placement.get(service) or "internal")
    return topology, topo


@pytest.fixture
def distributed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    config = tmp_path / "config"
    config.mkdir()
    (config / "deployment.yaml").write_text(yaml.safe_dump(TOPOLOGY), encoding="utf-8")
    (config / ".env").write_text("COMPOSE_PROFILES=nginx,keycloak,librechat\n", encoding="utf-8")
    monkeypatch.setattr(nodes, "_placement", _fake_placement)
    return str(config), str(tmp_path / "workspace")


@pytest.fixture
def single(tmp_path: Path) -> tuple[str, str]:
    config = tmp_path / "config"
    config.mkdir()
    (config / ".env").write_text("COMPOSE_PROFILES=nginx,keycloak\n", encoding="utf-8")
    return str(config), str(tmp_path / "workspace")


def _runs(verb: str, flags: tuple[str, ...], dirs: tuple[str, str]) -> list[tuple[str, ...]]:
    config, workspace = dirs
    return nodes.profile_runs(verb, flags, config_dir=config, workspace_dir=workspace)


# ---------------------------------------------------------------------------
# Which node a profile is on, and the runs a start or a stop takes
# ---------------------------------------------------------------------------


def test_a_single_host_keeps_one_run_with_its_flags(single: tuple[str, str]) -> None:
    flags = ("--profiles=localai",)
    assert _runs("start", flags, single) == [flags]
    assert nodes.profile_node(single[0], "localai") == ""


def test_a_profile_is_on_its_placement_or_the_controller(distributed: tuple[str, str]) -> None:
    config, _ = distributed
    assert nodes.profile_node(config, "localai") == "gpu"
    assert nodes.profile_node(config, "oauth2-proxy") == "edge"
    assert nodes.profile_node(config, "librechat") == "internal"
    assert nodes.profile_node(config, "not-a-profile") == ""


def test_a_start_ends_on_the_nodes_of_the_proxy_and_of_keycloak(
    distributed: tuple[str, str],
) -> None:
    # LocalAI on gpu, then edge for npm-provision and internal for keycloak-role-sync.
    assert _runs("start", ("--profiles=localai",), distributed) == [
        ("--profiles=localai", "--node=gpu"),
        ("--profiles=localai", "--node=edge"),
        ("--profiles=localai", "--node=internal"),
    ]


def test_a_node_is_visited_once(distributed: tuple[str, str]) -> None:
    # The relay is on edge, where the proxy is as well.
    assert _runs("start", ("--profiles=smtp",), distributed) == [
        ("--profiles=smtp", "--node=edge"),
        ("--profiles=smtp", "--node=internal"),
    ]


def test_a_hook_whose_service_is_off_adds_no_node(
    distributed: tuple[str, str],
) -> None:
    config, _ = distributed
    Path(config, ".env").write_text("COMPOSE_PROFILES=librechat\n", encoding="utf-8")
    assert _runs("start", ("--profiles=localai",), distributed) == [
        ("--profiles=localai", "--node=gpu"),
    ]


def test_a_stop_concerns_the_owner_only(distributed: tuple[str, str]) -> None:
    flags = ("--clean-up", "--volumes", "--profiles=localai")
    assert _runs("stop", flags, distributed) == [(*flags, "--node=gpu")]


@pytest.mark.parametrize(
    ("verb", "flags"),
    [
        ("keycloak-role-sync", ()),
        ("start", ("--profiles=librechat,localai",)),
        ("start", ()),
    ],
    ids=["other-verb", "several-profiles", "no-profile"],
)
def test_anything_else_is_left_as_it_is(
    distributed: tuple[str, str], verb: str, flags: tuple[str, ...]
) -> None:
    assert _runs(verb, flags, distributed) == [flags]


def test_a_run_that_names_its_node_is_left_as_it_is(distributed: tuple[str, str]) -> None:
    flags = ("--profiles=keycloak", "--node=edge")
    assert _runs("start", flags, distributed) == [flags]


def test_a_core_without_the_placement_names_no_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "deployment.yaml").write_text(yaml.safe_dump(TOPOLOGY), encoding="utf-8")
    # An entry of None makes the import fail, as for a core without the module.
    monkeypatch.setitem(sys.modules, "lib.topology", None)
    flags = ("--profiles=localai",)
    assert nodes.profile_runs(
        "start", flags, config_dir=str(config), workspace_dir=str(tmp_path)
    ) == [flags]


# ---------------------------------------------------------------------------
# The jobs
# ---------------------------------------------------------------------------


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self.fail_on: str | None = None
        self.lines: dict[str, list[str]] = {}

    async def setup(self, spec: Any, **kwargs: Any) -> tuple[int, str, str]:
        return 0, "Setup complete.", ""

    async def verb(self, **kwargs: Any) -> AsyncGenerator[str, None]:
        flags = list(kwargs["extra_flags"])
        self.calls.append((kwargs["verb"], flags))
        node = next((f for f in flags if f.startswith("--node=")), "")
        fail = self.fail_on is not None and node == self.fail_on

        async def lines() -> AsyncGenerator[str, None]:
            for line in self.lines.get(node, []):
                yield line
            if fail:
                raise svc.CtlError("node did not answer", exit_code=3)

        return lines()


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    rec = _Recorder()
    monkeypatch.setattr(svc, "run_core_verb", rec.verb)
    monkeypatch.setattr(svc, "run_module_setup", rec.setup)
    monkeypatch.setattr(svc, "run_smtp_setup", rec.setup)
    monkeypatch.setattr(mail_service, "run_core_verb", rec.verb)
    return rec


def _context(tmp_path: Path) -> JobContext:
    job = Job(id="job-1", action="x", target="t", user="admin", created_at=None)  # type: ignore[arg-type]
    return JobContext(job=job, log_path=tmp_path / "job.log")


def test_a_step_runs_once_per_node_and_logs_each(
    distributed: tuple[str, str], recorder: _Recorder, tmp_path: Path
) -> None:
    config, workspace = distributed
    ctx = _context(tmp_path)
    asyncio.run(
        svc._verb(ctx, "start", "--profiles=localai", workspace_dir=workspace, config_dir=config)
    )
    assert recorder.calls == [
        ("start", ["--profiles=localai", "--node=gpu"]),
        ("start", ["--profiles=localai", "--node=edge"]),
        ("start", ["--profiles=localai", "--node=internal"]),
    ]
    log = ctx.log_path.read_text(encoding="utf-8")
    assert "[ctl] papaia-ctl start --profiles=localai --node=gpu" in log


def test_the_first_node_that_fails_ends_the_step(
    distributed: tuple[str, str], recorder: _Recorder, tmp_path: Path
) -> None:
    config, workspace = distributed
    recorder.fail_on = "--node=gpu"
    with pytest.raises(svc.StepFailedError):
        asyncio.run(
            svc._verb(
                _context(tmp_path),
                "start",
                "--profiles=localai",
                workspace_dir=workspace,
                config_dir=config,
            )
        )
    assert len(recorder.calls) == 1


def test_the_relay_finds_a_realm_problem_on_the_node_of_keycloak(
    distributed: tuple[str, str], recorder: _Recorder, tmp_path: Path
) -> None:
    config, workspace = distributed
    recorder.lines["--node=internal"] = [
        "Keycloak role sync skipped: Keycloak at https://keycloak:8443 did not become ready"
    ]
    problem = asyncio.run(
        mail_service.run_verb(
            _context(tmp_path),
            "start",
            "--profiles=smtp",
            workspace_dir=workspace,
            config_dir=config,
        )
    )
    assert [flags[-1] for _, flags in recorder.calls] == ["--node=edge", "--node=internal"]
    assert problem == (
        "Keycloak role sync skipped: Keycloak at https://keycloak:8443 did not become ready"
    )


# ---------------------------------------------------------------------------
# The relay's status from its node
# ---------------------------------------------------------------------------

_DOCTOR = {
    "schema_version": 1,
    "checks": [
        {
            "name": "smtp",
            "status": "ok",
            "summary": "relay running, queue empty",
            "details": {"relay": "running", "queued": 0},
        }
    ],
}


@pytest.fixture
def node_health(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    """The node runs of the host health reading, recorded; its caches start empty."""
    asked: list[dict[str, Any]] = []
    host_health.reset_cache()
    monkeypatch.setattr(host_health, "refresh_interval", lambda config_dir: 60)
    yield asked
    host_health.reset_cache()


def test_the_relay_status_is_read_from_the_node_health_reading(
    distributed: tuple[str, str],
    node_health: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, workspace = distributed

    async def fake(**kwargs: Any) -> tuple[int, str, str]:
        node_health.append(kwargs)
        envelope = {"node": "edge", "doctor": _DOCTOR, "links": []}
        return 0, json.dumps(envelope) + "\n", ""

    async def no_local_doctor(**kwargs: Any) -> tuple[int, str, str]:
        raise AssertionError("a relay on another node needs no doctor of its own")

    monkeypatch.setattr(mail_status, "core_checks", lambda workspace_dir: ["smtp", "disk"])
    monkeypatch.setattr(mail_status, "run_py_cli", no_local_doctor)
    monkeypatch.setattr(host_health, "run_py_cli", fake)

    async def twice() -> tuple[Any, ...]:
        first = await mail_status._run(config, workspace)
        second = await mail_status._run(config, workspace)
        return first, second

    (status, out, _), (again, _, _) = asyncio.run(twice())
    assert [a["node"] for a in node_health] == ["edge"]
    assert node_health[0]["command"] == "doctor"
    assert out is not None and json.loads(out) == _DOCTOR
    assert status.available
    assert status.queued == 0
    assert again.available


def test_a_recheck_asks_the_node_again(
    distributed: tuple[str, str],
    node_health: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, workspace = distributed

    async def fake(**kwargs: Any) -> tuple[int, str, str]:
        node_health.append(kwargs)
        return 0, json.dumps({"node": "edge", "doctor": _DOCTOR, "links": []}), ""

    monkeypatch.setattr(mail_status, "core_checks", lambda workspace_dir: ["smtp"])
    monkeypatch.setattr(host_health, "run_py_cli", fake)
    monkeypatch.setattr(host_health, "RECHECK_MIN_INTERVAL_SECONDS", 0.0)

    async def run() -> None:
        await mail_status._run(config, workspace)
        await mail_status._run(config, workspace, force=True)

    asyncio.run(run())
    assert len(node_health) == 2


def test_an_unreadable_node_answer_is_a_reading_that_says_so(
    distributed: tuple[str, str],
    node_health: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, workspace = distributed

    async def fake(**kwargs: Any) -> tuple[int, str, str]:
        return 255, "", "ssh: connect to host 10.0.1.10 port 22: Connection timed out\n"

    monkeypatch.setattr(mail_status, "core_checks", lambda workspace_dir: ["smtp"])
    monkeypatch.setattr(host_health, "run_py_cli", fake)
    status, out, _ = asyncio.run(mail_status._run(config, workspace))
    assert out is None
    assert not status.available
    assert "Connection timed out" in status.reason


def test_a_relay_on_the_controller_keeps_the_local_check(
    distributed: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config, workspace = distributed
    monkeypatch.setitem(TOPOLOGY["topology"]["placement"], "smtp", "internal")
    asked: list[dict[str, Any]] = []

    async def fake(**kwargs: Any) -> tuple[int, str, str]:
        asked.append(kwargs)
        return 0, json.dumps(_DOCTOR), ""

    monkeypatch.setattr(mail_status, "core_checks", lambda workspace_dir: ["smtp", "disk"])
    monkeypatch.setattr(mail_status, "run_py_cli", fake)
    asyncio.run(mail_status._run(config, workspace))
    assert "node" not in asked[0]
    assert asked[0]["extra_flags"] == ["--json", "--skip=disk"]


# ---------------------------------------------------------------------------
# Users & Roles and Models & Usage
# ---------------------------------------------------------------------------


def test_users_and_models_address_the_gateway_names(distributed: tuple[str, str]) -> None:
    """Neither page asks where Keycloak or LiteLLM run: the link gateway serves their names on
    the controller, so the addresses are the ones a single host uses."""
    from app.config import Settings
    from app.core.keycloak_users import admin_endpoint

    endpoint = admin_endpoint("https://keycloak:8443/realms/papaia/protocol/openid-connect/token")
    assert endpoint is not None
    assert endpoint.base == "https://keycloak:8443"
    assert Settings.model_fields["litellm_url"].default == "http://litellm:4000"


# ---------------------------------------------------------------------------
# Switching the relay off clears the realm's mail server where Keycloak runs
# ---------------------------------------------------------------------------


def _disable_mail(dirs: tuple[str, str], tmp_path: Path) -> None:
    config, workspace = dirs
    callback = svc.disable_callback(
        module="mail", scope="keep", user="admin", workspace_dir=workspace, config_dir=config
    )
    asyncio.run(callback(_context(tmp_path)))


def test_switching_the_relay_off_starts_keycloak_on_its_node(
    distributed: tuple[str, str],
    recorder: _Recorder,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # keycloak-role-sync would run on edge with the bundle edge already has, which still
    # names the relay; the start hands edge its new bundle first and runs the sync after it.
    config, _ = distributed
    monkeypatch.setitem(TOPOLOGY["topology"]["placement"], "keycloak", "edge")
    Path(config, ".env").write_text(
        "COMPOSE_PROFILES=nginx,keycloak,librechat,smtp\n", encoding="utf-8"
    )
    _disable_mail(distributed, tmp_path)
    assert ("start", ["--profiles=keycloak", "--node=edge"]) in recorder.calls
    assert all(verb != "keycloak-role-sync" for verb, _ in recorder.calls)


def test_a_realm_left_with_the_relay_fails_the_job(
    distributed: tuple[str, str],
    recorder: _Recorder,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _ = distributed
    monkeypatch.setitem(TOPOLOGY["topology"]["placement"], "keycloak", "edge")
    recorder.lines["--node=edge"] = [
        "[!] Keycloak role sync failed. Once Keycloak is healthy, run it again."
    ]
    with pytest.raises(svc.StepFailedError, match="the realm still names it"):
        _disable_mail(distributed, tmp_path)


def test_switching_the_relay_off_on_one_host_runs_the_sync(
    single: tuple[str, str], recorder: _Recorder, tmp_path: Path
) -> None:
    _disable_mail(single, tmp_path)
    assert ("keycloak-role-sync", []) in recorder.calls
    assert all(verb != "start" for verb, _ in recorder.calls)


def test_switching_the_relay_off_without_keycloak_runs_the_sync(
    distributed: tuple[str, str], recorder: _Recorder, tmp_path: Path
) -> None:
    # An external identity provider: the core's sync says there is nothing to do, and no
    # start may bring up a Keycloak the installation does not run.
    config, _ = distributed
    Path(config, ".env").write_text("COMPOSE_PROFILES=nginx,librechat,smtp\n", encoding="utf-8")
    _disable_mail(distributed, tmp_path)
    assert ("keycloak-role-sync", []) in recorder.calls
    assert all("--profiles=keycloak" not in flags for _, flags in recorder.calls)


# ---------------------------------------------------------------------------
# Nodes that only run hooks
# ---------------------------------------------------------------------------


def test_a_hook_node_that_fails_is_skipped_and_named(
    distributed: tuple[str, str], recorder: _Recorder, tmp_path: Path
) -> None:
    config, workspace = distributed
    recorder.fail_on = "--node=edge"
    ctx = _context(tmp_path)
    skipped: list[str] = []
    asyncio.run(
        svc._verb(
            ctx,
            "start",
            "--profiles=localai",
            workspace_dir=workspace,
            config_dir=config,
            skipped=skipped,
        )
    )
    assert [flags[-1] for _, flags in recorder.calls] == [
        "--node=gpu", "--node=edge", "--node=internal"
    ]
    assert skipped == ["papaia-ctl start --profiles=localai --node=edge"]
    assert "[warn] papaia-ctl start --profiles=localai --node=edge failed" in (
        ctx.log_path.read_text(encoding="utf-8")
    )


def test_the_owner_failing_still_ends_the_step(
    distributed: tuple[str, str], recorder: _Recorder, tmp_path: Path
) -> None:
    config, workspace = distributed
    recorder.fail_on = "--node=gpu"
    with pytest.raises(svc.StepFailedError):
        asyncio.run(
            svc._verb(
                _context(tmp_path),
                "start",
                "--profiles=localai",
                workspace_dir=workspace,
                config_dir=config,
                skipped=[],
            )
        )
    assert len(recorder.calls) == 1


def test_an_overlay_is_kept_when_only_a_hook_node_fails(
    distributed: tuple[str, str],
    recorder: _Recorder,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # LibreChat runs on the controller; edge (the proxy) only runs its hooks and does not
    # answer. The new file stays, nothing is rolled back, and the job names the retry.
    config, workspace = distributed
    recorder.fail_on = "--node=edge"

    async def healthy(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(svc, "wait_healthy", healthy)
    assert svc.overlay.write(config, "librechat", b"old: 1\n", expected_revision="") is not None
    callback = svc.overlay_callback(
        key="librechat",
        content=b"new: 1\n",
        expected_revision=svc.overlay.read(config, "librechat").revision,
        event="apply",
        user="admin",
        workspace_dir=workspace,
        config_dir=config,
    )
    ctx = _context(tmp_path)
    with pytest.raises(
        svc.HookNodesFailedError,
        match=r"papaia-ctl start --profiles=librechat --node=edge",
    ):
        asyncio.run(callback(ctx))
    assert svc.overlay.read(config, "librechat").raw == b"new: 1\n"
    assert "[rollback]" not in ctx.log_path.read_text(encoding="utf-8")
    entries = [
        json.loads(line)
        for line in Path(config, "manager", "audit.log").read_text(encoding="utf-8").splitlines()
    ]
    assert entries[-1]["action"] == "config.overlay.apply"
    assert entries[-1]["result"] == "partial"


def test_switching_a_module_on_ends_with_the_node_it_missed(
    distributed: tuple[str, str],
    recorder: _Recorder,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, workspace = distributed
    recorder.fail_on = "--node=edge"

    async def healthy(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(svc, "wait_healthy", healthy)
    callback = svc.enable_callback(
        module="localai", variant="cpu", user="admin", workspace_dir=workspace, config_dir=config
    )
    with pytest.raises(svc.HookNodesFailedError, match="--node=edge"):
        asyncio.run(callback(_context(tmp_path)))
    assert ("start", ["--profiles=localai", "--node=internal"]) in recorder.calls
