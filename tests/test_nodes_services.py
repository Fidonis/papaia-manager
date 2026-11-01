"""Services, host health and widgets on a distributed installation.

The fixture is the shape of the core's merged `status --json`; nothing here starts a
process. The single-host path is `test_services.py` and the other existing tests, which
stay as they were.
"""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from app.core import ctl, host_health, nodes, services
from app.core.services import ServiceHealth
from app.core.widgets import builtin


def _container(service: str, state: str = "running", health: str = "healthy") -> dict[str, Any]:
    return {
        "name": f"papaia-{service}-1",
        "service": service,
        "role": "",
        "state": state,
        "health": health,
        "status_text": "Up 2 hours",
        "host_ports": [],
    }


REPORT = {
    "schema_version": 1,
    "modules": [
        {
            "name": "keycloak",
            "kind": "core",
            "compose_project": "papaia",
            "profiles": ["keycloak"],
            "declared": True,
            "status": "stopped",
            "containers": [_container("keycloak", "exited", "stopped")],
            "node": "edge",
        },
        {
            "name": "librechat",
            "kind": "core",
            "compose_project": "papaia",
            "profiles": ["librechat"],
            "declared": True,
            "status": "healthy",
            "containers": [_container("librechat")],
            "node": "internal",
        },
        {
            "name": "docs",
            "kind": "addon",
            "compose_project": "docs",
            "profiles": [],
            "declared": True,
            "status": "healthy",
            "containers": [_container("docs")],
            "node": "internal",
        },
    ],
    "nodes": [
        {"name": "internal", "reachable": True, "reason": None, "papaia_version": "1.8.0"},
        {"name": "edge", "reachable": True, "reason": None, "papaia_version": "1.8.0"},
        {"name": "gpu", "reachable": False, "reason": "timed out", "papaia_version": None},
    ],
}


def test_the_merged_report_keeps_the_node_of_each_module() -> None:
    snapshot = services.parse_status_report(json.dumps(REPORT))
    assert {m.name: m.node for m in snapshot.core} == {"keycloak": "edge", "librechat": "internal"}
    assert [m.addon for m in snapshot.addons] == ["docs"]
    assert snapshot.core[0].health is ServiceHealth.STOPPED
    assert snapshot.running_projects == {"papaia", "docs"}
    assert {g.name for g in snapshot.groups} == {"keycloak", "librechat"}


def test_a_node_that_did_not_answer_is_named() -> None:
    snapshot = services.parse_status_report(json.dumps(REPORT))
    assert [n.name for n in snapshot.unreachable_nodes] == ["gpu"]
    assert snapshot.unreachable_nodes[0].reason == "timed out"


def test_modules_are_grouped_by_node_in_the_order_of_the_nodes() -> None:
    sections = services.modules_by_node(services.parse_status_report(json.dumps(REPORT)))
    assert [(name, [m.name for m in found]) for name, found in sections] == [
        ("internal", ["docs", "librechat"]),
        ("edge", ["keycloak"]),
    ]


def test_an_answer_that_is_no_report_is_refused() -> None:
    with pytest.raises(ValueError):
        services.parse_status_report("[]")


def _deployment(path: Path, body: dict[str, Any]) -> str:
    (path / "deployment.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")
    return str(path)


DISTRIBUTED = {
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
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    services.invalidate_snapshot()
    host_health.reset_cache()


def test_a_distributed_snapshot_comes_from_the_core_not_from_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _deployment(tmp_path, DISTRIBUTED)
    asked: list[str] = []

    def status(config_dir: str, workspace_dir: str) -> str:
        asked.append(config_dir)
        return json.dumps(REPORT)

    def no_docker(node: str | None = None) -> str:
        raise AssertionError("docker ps sees only the controller")

    monkeypatch.setattr(services, "query_status", status)
    monkeypatch.setattr(services, "query_containers", no_docker)
    assert services.load_snapshot(config, "/w").nodes
    # Every reading is an SSH round trip per node: the cache outlives the single-host one.
    now = services._CACHE[0]  # type: ignore[index]
    monkeypatch.setattr(services.time, "monotonic", lambda: now + 20)
    services.load_snapshot(config, "/w")
    assert len(asked) == 1


def test_a_single_host_still_reads_docker_and_never_the_core(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _deployment(tmp_path, {"core": {}})

    def no_status(config_dir: str, workspace_dir: str) -> str:
        raise AssertionError("no core process on a single host")

    monkeypatch.setattr(services, "query_status", no_status)
    monkeypatch.setattr(services, "query_containers", lambda node=None: None)
    assert services.load_snapshot(config, "/w").nodes == []


def test_a_core_that_gives_no_status_invents_no_outage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _deployment(tmp_path, DISTRIBUTED)
    monkeypatch.setattr(services, "query_status", lambda c, w: None)
    assert services.load_snapshot(config, "/w").core == []
    assert services._CACHE is None


# ---------------------------------------------------------------------------
# Host health
# ---------------------------------------------------------------------------

_DOCTOR = {
    "schema_version": 1,
    "checks": [
        {
            "name": "disk_space",
            "status": "warn",
            "summary": "low",
            "details": {
                "paths": [
                    {
                        "path": "/",
                        "status": "warn",
                        "free_bytes": 10,
                        "total_bytes": 100,
                        "label": "root",
                    }
                ]
            },
        },
        {"name": "nodes", "status": "fail", "summary": "edge does not answer"},
        {"name": "links", "status": "pass", "summary": "2 links up"},
    ],
}


def test_the_controllers_doctor_carries_the_nodes_and_links_checks() -> None:
    health = host_health.parse_doctor(json.dumps(_DOCTOR))
    assert [(c.title, c.state, c.summary) for c in health.cluster_checks] == [
        ("Nodes", host_health.HostState.CRITICAL, "edge does not answer"),
        ("Links", host_health.HostState.OK, "2 links up"),
    ]


def test_a_nodes_doctor_is_read_out_of_its_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    async def fake(**kwargs: Any) -> tuple[int, str, str]:
        calls.append(kwargs)
        return 0, json.dumps({"node": "edge", "doctor": _DOCTOR, "links": []}) + "\n", ""

    monkeypatch.setattr(ctl, "_run_py", fake)
    monkeypatch.setattr(host_health, "refresh_interval", lambda c: 300)
    health = asyncio.run(host_health.load_node_health("edge", config_dir="/c", workspace_dir="/w"))
    assert health.available and health.disks
    assert calls[0]["extra_flags"] == ["--name=edge", "doctor"]


@pytest.mark.parametrize("answer", [(255, "", "ssh: timed out\n"), (0, "not json\n", "")])
def test_a_node_without_a_reading_says_so(
    monkeypatch: pytest.MonkeyPatch, answer: tuple[int, str, str]
) -> None:
    async def fake(**kwargs: Any) -> tuple[int, str, str]:
        return answer

    monkeypatch.setattr(ctl, "_run_py", fake)
    monkeypatch.setattr(host_health, "refresh_interval", lambda c: 300)
    health = asyncio.run(host_health.load_node_health("edge", config_dir="/c", workspace_dir="/w"))
    assert not health.available and health.reason


def test_all_nodes_are_read_and_a_single_host_is_one_nameless_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def local(**kwargs: Any) -> host_health.HostHealth:
        return host_health.HostHealth(available=True)

    async def remote(node: str, **kwargs: Any) -> host_health.HostHealth:
        return host_health.unavailable("timed out")

    monkeypatch.setattr(host_health, "load_host_health", local)
    monkeypatch.setattr(host_health, "load_node_health", remote)
    config = _deployment(tmp_path, DISTRIBUTED)
    found = asyncio.run(host_health.load_all_nodes(config_dir=config, workspace_dir="/w"))
    assert sorted((n, h.available) for n, h in found) == [("edge", False), ("internal", True)]
    single = _deployment(tmp_path, {"core": {}})
    only = asyncio.run(host_health.load_all_nodes(config_dir=single, workspace_dir="/w"))
    assert [n for n, _h in only] == [""]


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------


def _context(config: str) -> Any:
    return SimpleNamespace(
        settings=SimpleNamespace(papaia_config_dir=config, papaia_workspace_dir="/w"),
        now=datetime.now(UTC),
        queue=None,
    )


def test_the_services_card_names_the_node_with_the_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = {**REPORT, "nodes": REPORT["nodes"][:2]}
    snapshot = services.parse_status_report(json.dumps(report))
    monkeypatch.setattr(services, "load_snapshot", lambda c, w: snapshot)
    document = asyncio.run(builtin._services_health(_context("/c")))
    assert "edge" in document["data"]["label"]


def test_the_services_card_names_a_node_that_does_not_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = services.parse_status_report(json.dumps(REPORT))
    monkeypatch.setattr(services, "load_snapshot", lambda c, w: snapshot)
    document = asyncio.run(builtin._services_health(_context("/c")))
    assert "gpu" in document["data"]["label"]


def _disk(label: str, percent_free: int, state: host_health.HostState) -> host_health.HostCheck:
    return host_health.HostCheck(
        kind="disk",
        key=label,
        label=label,
        detail="",
        state=state,
        free_bytes=percent_free,
        total_bytes=100,
    )


def test_the_disk_card_takes_the_worst_disk_of_all_nodes_and_names_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _deployment(tmp_path, DISTRIBUTED)
    ok, warn = host_health.HostState.OK, host_health.HostState.WARN

    async def every(**kwargs: Any) -> list[tuple[str, host_health.HostHealth]]:
        return [
            ("internal", host_health.HostHealth(available=True, disks=(_disk("/", 80, ok),))),
            ("edge", host_health.HostHealth(available=True, disks=(_disk("/", 5, warn),))),
            ("gpu", host_health.unavailable("timed out")),
        ]

    monkeypatch.setattr(host_health, "load_all_nodes", every)
    document = asyncio.run(builtin._disk_space(_context(config)))
    label = document["data"]["label"]
    assert label.startswith("edge: /") and "Not read: gpu." in label


def test_the_disk_card_is_not_ok_while_a_node_gives_no_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _deployment(tmp_path, DISTRIBUTED)
    ok = host_health.HostState.OK

    async def every(**kwargs: Any) -> list[tuple[str, host_health.HostHealth]]:
        return [
            ("internal", host_health.HostHealth(available=True, disks=(_disk("/", 80, ok),))),
            ("edge", host_health.unavailable("timed out")),
        ]

    monkeypatch.setattr(host_health, "load_all_nodes", every)
    assert asyncio.run(builtin._disk_space(_context(config)))["state"] == "warn"


def test_the_gateway_names_stay_the_defaults() -> None:
    # Users & Roles and Models & Usage dial these names; on a distributed installation the
    # link gateway carries them, wherever the service runs.
    from app.config import Settings

    assert Settings.model_fields["litellm_url"].default == "http://litellm:4000"
    assert nodes.DISTRIBUTED == "distributed"
