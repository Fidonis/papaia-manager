"""Nodes of a distributed installation: the registry, the executors and the call sites.

Nothing here starts a process. The core's side is stubbed at `ctl._run_py` and at
`ctl._stream_subprocess`, the two places a call turns into a process, so the assertions are
on the argv the core would receive.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.core import ctl, images, nodes, runner, services, upgrade

TOPOLOGY = {
    "topology": {
        "mode": "distributed",
        "controller": "internal",
        "nodes": {
            "internal": {"address": "10.0.2.10", "executor": "local"},
            "edge": {"address": "10.0.1.10", "executor": "ssh"},
        },
    }
}


@pytest.fixture(autouse=True)
def _no_cached_overview() -> None:
    nodes.invalidate_overview()


@pytest.fixture
def config(tmp_path: Path) -> str:
    (tmp_path / "deployment.yaml").write_text(yaml.safe_dump(TOPOLOGY), encoding="utf-8")
    return str(tmp_path)


def test_a_single_host_has_no_nodes(tmp_path: Path) -> None:
    (tmp_path / "deployment.yaml").write_text("core: {}\n", encoding="utf-8")
    assert nodes.is_distributed(str(tmp_path)) is False
    assert nodes.registry(str(tmp_path)) == {}


def test_a_config_without_a_deployment_file_has_no_nodes(tmp_path: Path) -> None:
    assert nodes.is_distributed(str(tmp_path)) is False


def test_a_broken_deployment_file_is_not_a_distributed_one(tmp_path: Path) -> None:
    (tmp_path / "deployment.yaml").write_text("topology: [", encoding="utf-8")
    assert nodes.is_distributed(str(tmp_path)) is False


def test_the_registry_reads_the_topology(config: str) -> None:
    found = nodes.registry(config)
    assert set(found) == {"internal", "edge"}
    assert found["internal"].controller and not found["internal"].remote
    assert found["edge"].remote and found["edge"].address == "10.0.1.10"


def test_the_executor_follows_the_node(config: str) -> None:
    def pick(name: str, config_dir: str = config) -> nodes.Executor:
        return nodes.executor_for(name, workspace_dir="/w", config_dir=config_dir)

    assert isinstance(pick("internal"), nodes.LocalExecutor)
    assert isinstance(pick("edge"), nodes.SshExecutor)
    with pytest.raises(nodes.NodeError, match="no node named"):
        pick("elsewhere")


def test_a_single_host_is_always_local(tmp_path: Path) -> None:
    picked = nodes.executor_for("anything", workspace_dir="/w", config_dir=str(tmp_path))
    assert isinstance(picked, nodes.LocalExecutor)


# ---------------------------------------------------------------------------
# Calls
# ---------------------------------------------------------------------------


class FakePy:
    def __init__(self, answers: dict[str, tuple[int, str, str]] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.answers = answers or {}

    async def __call__(self, **kwargs: Any) -> tuple[int, str, str]:
        self.calls.append(kwargs)
        flags = kwargs.get("extra_flags") or []
        key = flags[-1] if kwargs["command"] == "node-exec" else kwargs["command"]
        return self.answers.get(key, (0, "", ""))


@pytest.fixture
def fake_py(monkeypatch: pytest.MonkeyPatch) -> FakePy:
    fake = FakePy()
    monkeypatch.setattr(ctl, "_run_py", fake)
    return fake


def test_a_read_on_a_node_goes_through_node_exec(fake_py: FakePy, config: str) -> None:
    edge = nodes.executor_for("edge", workspace_dir="/w", config_dir=config)
    asyncio.run(edge.run_py("doctor", limit=5.0))
    assert fake_py.calls[0]["command"] == "node-exec"
    assert fake_py.calls[0]["extra_flags"] == ["--name=edge", "doctor"]


def test_a_read_on_this_machine_takes_the_old_path(fake_py: FakePy, config: str) -> None:
    local = nodes.executor_for("internal", workspace_dir="/w", config_dir=config)
    asyncio.run(local.run_py("doctor"))
    assert fake_py.calls[0]["command"] == "doctor"


def test_only_reading_verbs_reach_a_node(fake_py: FakePy) -> None:
    with pytest.raises(ValueError, match="not in the allowed set"):
        asyncio.run(
            ctl.run_py_cli(command="hook", workspace_dir="/w", config_dir="/c", node="edge")
        )
    assert fake_py.calls == []


def test_node_commands_are_an_allowlist(fake_py: FakePy) -> None:
    with pytest.raises(ValueError, match="not in the allowed set"):
        asyncio.run(ctl.run_node_command(command="setup", workspace_dir="/w", config_dir="/c"))
    assert fake_py.calls == []


def test_a_stack_verb_on_a_node_streams_and_passes_the_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str]] = []

    async def stream(cmd: list[str], env: dict[str, str]) -> AsyncGenerator[str, None]:
        seen.append(cmd)
        yield "line one"
        raise ctl.CtlError("papaia-ctl exited with code 3", exit_code=3)

    monkeypatch.setattr(ctl, "_stream_subprocess", stream)

    async def run() -> list[str]:
        lines = await ctl.run_core_verb(
            verb="start",
            workspace_dir="/w",
            config_dir="/c",
            extra_flags=["keycloak"],
            node="edge",
        )
        collected = []
        with pytest.raises(ctl.CtlError) as caught:
            async for line in lines:
                collected.append(line)
        assert caught.value.exit_code == 3
        return collected

    assert asyncio.run(run()) == ["line one"]
    assert seen[0][-4:] == ["node-exec", "--name=edge", "compose-up", "keycloak"]


def test_a_stack_verb_on_this_machine_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    async def stream(cmd: list[str], env: dict[str, str]) -> AsyncGenerator[str, None]:
        seen.append(cmd)
        yield "ok"

    monkeypatch.setattr(ctl, "_stream_subprocess", stream)

    async def run() -> None:
        async for _line in await ctl.run_core_verb(
            verb="stop", workspace_dir="/w", config_dir="/c"
        ):
            pass

    asyncio.run(run())
    assert seen[0][0] == "bash" and seen[0][2:] == ["stop", "--config-dir=/c"]


def test_a_verb_without_a_node_form_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="no equivalent on a node"):
        asyncio.run(
            ctl.run_core_verb(verb="backup", workspace_dir="/w", config_dir="/c", node="edge")
        )


def test_what_a_node_has_no_verb_for_is_refused_not_run_here() -> None:
    with pytest.raises(runner.RunnerError, match="node edge"):
        asyncio.run(runner._docker("ps", node="edge"))
    with pytest.raises(images.ImagesError, match="node edge"):
        asyncio.run(images._run(["docker", "images"], limit=1.0, node="edge"))
    with pytest.raises(upgrade.UpgradeError, match="node edge"):
        asyncio.run(upgrade._git(Path("/w"), "status", node="edge"))
    assert services.query_containers(node="edge") is None


def test_put_bundle_pushes_to_a_node_and_is_a_no_op_here(fake_py: FakePy, config: str) -> None:
    asyncio.run(
        nodes.executor_for("internal", workspace_dir="/w", config_dir=config).put_bundle()
    )
    assert fake_py.calls == []
    asyncio.run(nodes.executor_for("edge", workspace_dir="/w", config_dir=config).put_bundle())
    assert fake_py.calls[0]["command"] == "node-push"
    assert fake_py.calls[0]["extra_flags"] == ["--name=edge"]


def test_a_failed_push_says_why(monkeypatch: pytest.MonkeyPatch, config: str) -> None:
    monkeypatch.setattr(ctl, "_run_py", FakePy({"node-push": (3, "", "ERROR: no route to host\n")}))
    edge = nodes.executor_for("edge", workspace_dir="/w", config_dir=config)
    with pytest.raises(nodes.NodeError, match="no route to host"):
        asyncio.run(edge.put_bundle())


# ---------------------------------------------------------------------------
# The overview
# ---------------------------------------------------------------------------

_ROWS = [
    {
        "name": "internal",
        "address": "10.0.2.10",
        "executor": "local",
        "controller": True,
        "services": ["librechat"],
        "host_key_pinned": True,
        "release": "1.8.0",
        "check": {"reachable": True, "papaia_version": "1.8.0"},
    },
    {
        "name": "edge",
        "address": "10.0.1.10",
        "executor": "ssh",
        "controller": False,
        "services": ["keycloak", "nginx"],
        "host_key_pinned": True,
        "release": "1.8.0",
        "check": {"reachable": False, "reason": "ssh: connect to host: timed out"},
    },
]
_DOCTOR = json.dumps(
    {
        "node": "internal",
        "links": [{"peer": "edge", "ok": False, "stage": "connect", "reason": "refused"}],
    }
)


def _overview(monkeypatch: pytest.MonkeyPatch, fake: FakePy) -> list[nodes.NodeState]:
    monkeypatch.setattr(ctl, "_run_py", fake)
    return asyncio.run(nodes.overview(workspace_dir="/w", config_dir="/c"))


def test_the_overview_reflects_a_node_that_does_not_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakePy({"node-list": (0, json.dumps(_ROWS), ""), "doctor": (0, _DOCTOR, "")})
    internal, edge = _overview(monkeypatch, fake)
    assert internal.reachable and internal.version == "1.8.0"
    assert internal.links_known and not internal.links_ok
    assert internal.links[0].reason == "refused"
    assert not edge.reachable and "timed out" in edge.reason
    # The last known release, and no doctor call to a node that did not answer.
    assert edge.version == "1.8.0" and not edge.links_known
    doctors = [c for c in fake.calls if c["command"] == "node-exec"]
    assert [c["extra_flags"][0] for c in doctors] == ["--name=internal"]


def test_the_overview_is_reused_until_asked_for_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakePy({"node-list": (0, "[]", "")})
    _overview(monkeypatch, fake)
    _overview(monkeypatch, fake)
    assert len(fake.calls) == 1
    asyncio.run(nodes.overview(workspace_dir="/w", config_dir="/c", fresh=True))
    assert len(fake.calls) == 2


def test_a_core_that_cannot_list_says_why(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(nodes.NodeError, match="not a distributed"):
        _overview(monkeypatch, FakePy({"node-list": (2, "", "ERROR: not a distributed one\n")}))


def test_host_keys_are_read_from_the_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    line = "HOSTKEY\tssh-ed25519\tSHA256:" + "a" * 43 + "\n"
    monkeypatch.setattr(ctl, "_run_py", FakePy({"node-keyscan": (0, line, "")}))
    (key,) = asyncio.run(nodes.host_keys("edge", workspace_dir="/w", config_dir="/c"))
    assert key.kind == "ssh-ed25519" and nodes.valid_fingerprint(key.fingerprint)


def test_adding_a_node_registers_it_and_writes_its_archive(fake_py: FakePy) -> None:
    new = nodes.NewNode(name="gpu", address="10.0.3.10", ssh_port=2222)
    archive = asyncio.run(nodes.add_node(new, workspace_dir="/w", config_dir="/c"))
    assert archive == "/c/nodes/gpu/papaia-join-gpu.tar.gz"
    assert [c["command"] for c in fake_py.calls] == ["node-register", "node-join-bundle"]
    assert "--ssh-port=2222" in fake_py.calls[0]["extra_flags"]
