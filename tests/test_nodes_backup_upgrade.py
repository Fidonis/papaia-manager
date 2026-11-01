"""Backups, restores and upgrades of a distributed installation, without a process."""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from app.core import backup_run, ctl, nodes, restore_scope, upgrade

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


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[str, str]:
    config = tmp_path / "config"
    config.mkdir()
    (config / "deployment.yaml").write_text(yaml.safe_dump(TOPOLOGY), encoding="utf-8")
    workspace = tmp_path / "workspace"
    (workspace / "papaia").mkdir(parents=True)
    (workspace / "papaia" / "VERSION").write_text("1.8.0\n", encoding="utf-8")
    return str(config), str(workspace)


def _state(config: str, node: str, release: str) -> None:
    path = Path(config) / "nodes" / node / "state.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"release": release}), encoding="utf-8")


def test_nothing_is_locked_while_the_releases_agree(dirs: tuple[str, str]) -> None:
    config, workspace = dirs
    _state(config, "edge", "1.8.0")
    assert nodes.lock_reason(config, workspace) == ""


def test_a_node_on_another_release_locks(dirs: tuple[str, str]) -> None:
    config, workspace = dirs
    _state(config, "edge", "1.7.0")
    reason = nodes.lock_reason(config, workspace)
    assert "edge (1.7.0)" in reason and "1.8.0" in reason


def test_an_unfinished_upgrade_locks(dirs: tuple[str, str]) -> None:
    config, workspace = dirs
    (Path(config) / "upgrade.lock").write_text(
        json.dumps({"from": "1.7.0", "to": "1.8.0"}), encoding="utf-8"
    )
    assert "1.7.0" in nodes.lock_reason(config, workspace)


def test_the_lock_names_the_restore_point_it_can_go_back_to(dirs: tuple[str, str]) -> None:
    config, workspace = dirs
    lock = {"from": "1.7.0", "to": "1.8.0", "restore_point": "2026-11-01_10-00-00"}
    (Path(config) / "upgrade.lock").write_text(json.dumps(lock), encoding="utf-8")
    assert "2026-11-01_10-00-00" in nodes.lock_reason(config, workspace)


def test_a_lock_without_a_restore_point_gives_the_way_out(dirs: tuple[str, str]) -> None:
    config, workspace = dirs
    lock = {"from": "1.7.0", "to": "1.8.0", "restore_point": None}
    (Path(config) / "upgrade.lock").write_text(json.dumps(lock), encoding="utf-8")
    assert "papaia-ctl start --addons --unlock" in nodes.lock_reason(config, workspace)


def test_a_single_host_is_never_locked(tmp_path: Path) -> None:
    (tmp_path / "deployment.yaml").write_text("core: {}\n", encoding="utf-8")
    (tmp_path / "upgrade.lock").write_text("{}", encoding="utf-8")
    assert nodes.lock_reason(str(tmp_path), str(tmp_path)) == ""


def test_a_node_selector_passes_the_selection_check() -> None:
    assert ctl.selection_flag(["node:edge"], allowed={"node:edge"}) == "--only=node:edge"


def test_a_node_backup_is_scoped_to_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    flags: list[str] = []

    async def verb(**kwargs: Any) -> AsyncGenerator[str, None]:
        flags.extend(kwargs["extra_flags"])

        async def lines() -> AsyncGenerator[str, None]:
            yield "ok"

        return lines()

    class Queue:
        async def enqueue(self, **kwargs: Any) -> Any:
            self.params = kwargs["params"]
            context = SimpleNamespace(log=lambda line: None, job=SimpleNamespace(id="j"))
            await kwargs["callback"](context)
            return SimpleNamespace(id="j")

    monkeypatch.setattr(backup_run, "run_core_verb", verb)
    queue = Queue()
    settings = SimpleNamespace(papaia_workspace_dir="/w", papaia_config_dir=str(tmp_path))
    asyncio.run(
        backup_run.enqueue_backup(
            queue, settings, Path("/b"), user="u", retention_days=None, node="edge"  # type: ignore[arg-type]
        )
    )
    assert flags == [f"--backup-dir={Path('/b')}", "--only=node:edge"]
    assert queue.params["node"] == "edge"


MANIFEST = {
    "core_project": "papaia",
    "artifacts": [
        {"kind": "volume", "target": "papaia_kc", "module": "keycloak", "owner": "core",
         "node": "edge", "profiles": ["keycloak"]},
        {"kind": "volume", "target": "papaia_chat", "module": "librechat", "owner": "core",
         "node": "internal", "profiles": ["librechat"]},
    ],
}


def test_a_restore_offers_remote_nodes_but_not_the_controller() -> None:
    groups = restore_scope.build_groups(MANIFEST, controller="internal")
    nodes_offered = [g.selector for g in groups if g.kind == "node"]
    assert nodes_offered == ["node:edge"]
    assert "node:edge" in restore_scope.allowed_selectors(groups)


def test_a_node_restore_keeps_the_guards_of_its_modules() -> None:
    groups = restore_scope.build_groups(MANIFEST, controller="internal")
    assert restore_scope.requires_full_restore(groups, ["module:keycloak"])
    assert restore_scope.requires_full_restore(groups, ["node:edge"])
    assert "keycloak" in restore_scope.selected_profiles(groups, ["node:edge"])
    (node,) = [g for g in groups if g.kind == "node"]
    (module,) = [g for g in groups if g.selector == "module:keycloak"]
    assert set(module.hazards) <= set(node.hazards)


def test_a_node_of_plain_modules_stays_a_scoped_restore() -> None:
    artifact = {**MANIFEST["artifacts"][1], "node": "edge"}
    manifest = {**MANIFEST, "artifacts": [artifact]}
    groups = restore_scope.build_groups(manifest, controller="internal")
    assert not restore_scope.requires_full_restore(groups, ["node:edge"])


def test_a_single_host_manifest_offers_no_node() -> None:
    artifacts = [{k: v for k, v in a.items() if k != "node"} for a in MANIFEST["artifacts"]]
    groups = restore_scope.build_groups({**MANIFEST, "artifacts": artifacts})
    assert not [g for g in groups if g.kind == "node"]


DISTRIBUTED_LOG = (
    "[papaia-ctl] Stopping and removing the containers (volumes are kept)...\n"
    "[papaia-ctl] Moving the checkout to v1.8.0...\n"
    "[papaia-ctl] Installing 1.8.0 on every node and running the migrations...\n"
    "[papaia-ctl] node edge: installing release 1.8.0\n"
    "[papaia-ctl] node edge: installing its bundle\n"
    "[papaia-ctl] node internal: installing its bundle\n"
    "[papaia-ctl] Applying 1.8.0's configuration to /c...\n"
    "[papaia-ctl] Starting every node...\n"
    "[papaia-ctl] node edge: starting keycloak\n"
)


def test_a_distributed_upgrade_reaches_its_phases() -> None:
    states = {p.key: p.state for p in upgrade.phases_from_log(DISTRIBUTED_LOG, running=True)}
    assert states["migrations"] == upgrade.PHASE_DONE
    assert states["render"] == upgrade.PHASE_DONE
    assert states["start"] == upgrade.PHASE_RUNNING


def test_progress_shows_each_nodes_latest_step() -> None:
    assert upgrade.node_progress(DISTRIBUTED_LOG) == [
        ("edge", "starting keycloak"),
        ("internal", "installing its bundle"),
    ]


def test_a_single_host_log_has_no_node_progress() -> None:
    assert upgrade.node_progress("[papaia-ctl] Starting the stack...\n") == []
