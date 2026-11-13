"""LocalAI as a process on an Apple Silicon Mac (`LOCALAI_RUNTIME=native` in the core `.env`).

The manager cannot see the process from its container. It reads the runtime the core stored and
the core's doctor check `localai_native`; the fixtures under `fixtures/` are that check's JSON as
the core's `doctor --json` writes it from inside a container (the `gpu` check skipping beside it).
These tests pin what follows from both: no `localai` container is expected, the process is a row
of its own with the check's state, the Modules row is read-only, and no LocalAI image or volume is
offered for removal or restore. The container runtime keeps its behaviour, which the existing
tests of each module pin as well.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.core import images, inventory, localai_runtime, restore_scope, services
from app.core.config import modules
from app.core.host_health import HostState, parse_doctor
from app.core.images import LocalImage, build_report
from app.core.localai_runtime import NativeCheck, find_check, parse_check
from app.core.services import ServiceHealth, build_snapshot

_FIXTURES = Path(__file__).parent / "fixtures"


def _doctor(name: str) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(
        (_FIXTURES / f"doctor_localai_native_{name}.json").read_text(encoding="utf-8")
    )
    return document


def _native(name: str) -> NativeCheck:
    found = find_check(_doctor(name)["checks"])
    assert found is not None
    return found


# A core checkout as far as these tests read it: LocalAI's compose file with the model-init job
# and the inference service, and LibreChat beside it.
_LOCALAI_COMPOSE = """\
services:
  localai-model-init:
    image: curlimages/curl:latest
    labels:
      de.fidonis.module: papaia-localai
      de.fidonis.role: model-init
    profiles: [localai]
  localai:
    image: localai/localai:v4.7.1
    labels:
      de.fidonis.module: papaia-localai
      de.fidonis.role: inference
    profiles: [localai]
"""
_LIBRECHAT_COMPOSE = """\
services:
  librechat:
    labels:
      de.fidonis.module: papaia-librechat
    profiles: [librechat]
"""


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    src = tmp_path / "workspace" / "papaia" / "src"
    (src / "ai" / "localai").mkdir(parents=True)
    (src / "ai" / "librechat").mkdir(parents=True)
    (src / "docker-compose.yml").write_text(
        "include:\n  - ai/localai/docker-compose.yml\n  - ai/librechat/docker-compose.yml\n",
        encoding="utf-8",
    )
    (src / "ai" / "localai" / "docker-compose.yml").write_text(_LOCALAI_COMPOSE, encoding="utf-8")
    (src / "ai" / "librechat" / "docker-compose.yml").write_text(
        _LIBRECHAT_COMPOSE, encoding="utf-8"
    )
    return tmp_path / "workspace"


def _config(tmp_path: Path, runtime: str | None) -> Path:
    directory = tmp_path / "config"
    directory.mkdir(exist_ok=True)
    lines = ["COMPOSE_PROJECT_NAME=papaia", "COMPOSE_PROFILES=librechat,localai"]
    if runtime is not None:
        lines.append(f"LOCALAI_RUNTIME={runtime}")
    (directory / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return directory


@pytest.fixture
def native(tmp_path: Path) -> Path:
    return _config(tmp_path, "native")


@pytest.fixture
def container(tmp_path: Path) -> Path:
    return _config(tmp_path, None)


# ---------------------------------------------------------------------------
# The stored runtime
# ---------------------------------------------------------------------------


def test_the_runtime_is_the_container_when_nothing_is_stored(tmp_path: Path) -> None:
    # No `.env` at all, no key, an empty value: the core's default.
    assert modules.stored_runtime(str(tmp_path)) == "container"
    (tmp_path / ".env").write_text("COMPOSE_PROFILES=localai\n", encoding="utf-8")
    assert modules.stored_runtime(str(tmp_path)) == "container"
    (tmp_path / ".env").write_text("LOCALAI_RUNTIME=\n", encoding="utf-8")
    assert modules.stored_runtime(str(tmp_path)) == "container"


def test_the_runtime_is_read_from_the_root_env(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("LOCALAI_RUNTIME=native\n", encoding="utf-8")
    assert modules.stored_runtime(str(tmp_path)) == "native"
    assert localai_runtime.is_native(str(tmp_path))
    (tmp_path / ".env").write_text("LOCALAI_RUNTIME=container\n", encoding="utf-8")
    assert modules.stored_runtime(str(tmp_path)) == "container"


def test_a_value_the_core_does_not_know_reads_as_the_container(tmp_path: Path) -> None:
    # The core's `localai_native.runtime` reads anything but `native` as the container.
    (tmp_path / ".env").write_text("LOCALAI_RUNTIME=metal\n", encoding="utf-8")
    assert modules.stored_runtime(str(tmp_path)) == "container"


# ---------------------------------------------------------------------------
# The doctor check, from the core's JSON
# ---------------------------------------------------------------------------


def test_a_native_localai_that_answers_is_running() -> None:
    check = _native("pass")
    assert check.status == "pass"
    assert check.running
    assert check.ready is True
    assert check.url == "http://host.docker.internal:8080/readyz"
    assert check.installed == check.pinned == "v4.7.1"
    assert check.summary.startswith("LocalAI v4.7.1 answers on")


def test_a_native_localai_that_does_not_answer_is_not_running_and_says_how_to_start_it() -> None:
    check = _native("warn")
    assert check.status == "warn"
    assert not check.running
    assert check.summary.endswith(
        "start it with /Users/operator/workspace/localai/papaia/run-localai"
    )


def test_an_outdated_binary_that_still_answers_is_running() -> None:
    entry = {
        "name": "localai_native",
        "status": "warn",
        "summary": "LocalAI v4.6.0 is installed, this release pins v4.7.1",
        "details": {"ready": True, "installed": "v4.6.0", "pinned": "v4.7.1"},
    }
    found = parse_check(entry)
    assert found is not None
    assert found.running


def test_the_skip_of_the_container_runtime_is_nothing_to_show() -> None:
    entry = {"name": "localai_native", "status": "skip", "summary": "LocalAI runs in its container"}
    assert parse_check(entry) is None
    assert find_check([entry]) is None


@pytest.mark.parametrize(
    "entry",
    [
        None,
        "localai_native",
        {"name": "gpu", "status": "pass"},
        {"name": "localai_native"},
        {"name": "localai_native", "status": "maybe"},
    ],
)
def test_what_is_not_the_check_is_ignored(entry: Any) -> None:
    assert parse_check(entry) is None


def test_details_of_the_wrong_shape_are_left_out() -> None:
    found = parse_check(
        {"name": "localai_native", "status": "pass", "summary": 3, "details": {"ready": "yes"}}
    )
    assert found == NativeCheck(status="pass", summary="", ready=None)
    # Without a `ready`, a pass is what says it answers.
    assert found.running


def test_a_later_sentence_of_the_core_is_shown_as_it_is() -> None:
    # Fidonis/papaia#367 adds the launchd state to the message; it is not parsed.
    document = _doctor("warn")
    document["checks"][1]["summary"] += " (launchd: not loaded)"
    check = find_check(document["checks"])
    assert check is not None
    assert check.summary.endswith("(launchd: not loaded)")
    assert not check.running


# ---------------------------------------------------------------------------
# Host page reading
# ---------------------------------------------------------------------------


def _with_memory(document: dict[str, Any], status: str = "pass") -> str:
    document["checks"].insert(
        0,
        {
            "name": "memory",
            "status": status,
            "summary": "16.0 GiB total",
            "details": {"total_bytes": 16 * 1024**3, "available_bytes": 8 * 1024**3},
        },
    )
    return json.dumps(document)


@pytest.mark.parametrize("name", ["pass", "warn"])
def test_the_host_reading_carries_the_check_without_judging_the_host_by_it(name: str) -> None:
    health = parse_doctor(_with_memory(_doctor(name)))

    assert health.available
    assert health.localai == _native(name)
    # The GPU check skips for the native runtime without a variant: no row, no verdict.
    assert [c.kind for c in health.checks] == ["memory"]
    assert health.overall is HostState.OK
    assert health.warn_count == 0
    assert health.total == 1


def test_the_container_runtime_has_no_native_reading() -> None:
    document = _doctor("pass")
    document["checks"][1] = {
        "name": "localai_native",
        "status": "skip",
        "summary": "LocalAI runs in its container",
        "details": {},
    }
    assert parse_doctor(_with_memory(document)).localai is None


def test_a_core_without_the_check_has_no_native_reading() -> None:
    document = _doctor("pass")
    del document["checks"][1]
    assert parse_doctor(_with_memory(document)).localai is None


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------


def _ps(*rows: tuple[str, str, str, str, str, str]) -> str:
    """`docker ps` lines: (name, state, status, service, module, role) in the core project."""
    return "\n".join(
        "\t".join((name, state, status, "papaia", service, module, role, ""))
        for name, state, status, service, module, role in rows
    )


_MODEL_INIT = (
    "papaia-localai-model-init-1",
    "exited",
    "Exited (0) 1 hour ago",
    "localai-model-init",
    "papaia-localai",
    "model-init",
)
_LIBRECHAT = ("papaia-librechat-1", "running", "Up 1 hour", "librechat", "papaia-librechat", "")


def _localai(snapshot: services.StackSnapshot) -> services.ServiceModule:
    return next(m for m in snapshot.core if m.name == "localai")


def test_the_inventory_expects_no_localai_container_for_native(
    native: Path, workspace: Path
) -> None:
    expected = {s.service for s in inventory.expected_core(str(native), str(workspace))}
    assert expected == {"librechat", "localai-model-init"}


def test_the_inventory_of_the_container_runtime_is_unchanged(
    container: Path, workspace: Path
) -> None:
    expected = {s.service for s in inventory.expected_core(str(container), str(workspace))}
    assert expected == {"librechat", "localai-model-init", "localai"}


@pytest.mark.parametrize(
    ("name", "health"), [("pass", ServiceHealth.HEALTHY), ("warn", ServiceHealth.STOPPED)]
)
def test_the_services_list_has_the_native_process_and_nothing_missing(
    native: Path, workspace: Path, name: str, health: ServiceHealth
) -> None:
    snapshot = build_snapshot(
        str(native), str(workspace), _ps(_MODEL_INIT, _LIBRECHAT), _native(name)
    )

    module = _localai(snapshot)
    assert all(c.health is not ServiceHealth.MISSING for m in snapshot.core for c in m.containers)
    process = next(c for c in module.containers if c.native)
    assert process.title == "LocalAI (native)"
    assert process.role == "inference"
    assert process.health is health
    assert process.status_display == _native(name).summary
    assert module.health is health
    assert module.profiles == ("localai",)


def test_without_a_reading_the_native_process_is_unknown_not_missing(
    native: Path, workspace: Path
) -> None:
    snapshot = build_snapshot(str(native), str(workspace), _ps(_MODEL_INIT, _LIBRECHAT))

    process = next(c for c in _localai(snapshot).containers if c.native)
    assert process.health is ServiceHealth.UNKNOWN
    assert process.status_display == "not checked yet"


def test_the_localai_group_waits_for_its_containers_only(native: Path, workspace: Path) -> None:
    # `papaia-ctl start` cannot start the process, so the group the Modules and Overlay jobs
    # wait for (`wait_healthy`) is the model download alone, done here.
    snapshot = build_snapshot(
        str(native), str(workspace), _ps(_MODEL_INIT, _LIBRECHAT), _native("warn")
    )

    group = next(g for g in snapshot.groups if g.name == "localai")
    assert group.health is ServiceHealth.COMPLETED
    assert group.containers == 1


def test_the_container_runtime_still_reports_a_missing_localai(
    container: Path, workspace: Path
) -> None:
    snapshot = build_snapshot(
        str(container), str(workspace), _ps(_MODEL_INIT, _LIBRECHAT), _native("pass")
    )

    module = _localai(snapshot)
    assert not any(c.native for c in module.containers)
    missing = [c.service for c in module.containers if c.health is ServiceHealth.MISSING]
    assert missing == ["localai"]
    assert next(g for g in snapshot.groups if g.name == "localai").health is (
        ServiceHealth.MISSING
    )


def test_the_snapshot_takes_the_check_from_the_last_host_reading(
    native: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core import host_health

    reading = parse_doctor(_with_memory(_doctor("pass")))
    monkeypatch.setattr(host_health, "cached_host_health", lambda: reading)
    monkeypatch.setattr(services, "_CACHE", None)
    monkeypatch.setattr(services, "query_containers", lambda: _ps(_MODEL_INIT, _LIBRECHAT))
    monkeypatch.setattr(services, "query_restart_policies", lambda names: {})

    snapshot = services.load_snapshot(str(native), str(workspace))
    services.invalidate_snapshot()

    process = next(c for c in _localai(snapshot).containers if c.native)
    assert process.health is ServiceHealth.HEALTHY


def test_the_container_runtime_does_not_look_at_the_host_reading(
    container: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core import host_health

    def unexpected() -> None:
        raise AssertionError("the container runtime has no native check to read")

    monkeypatch.setattr(host_health, "cached_host_health", unexpected)
    assert services._native_check(str(container)) is None  # noqa: SLF001


# ---------------------------------------------------------------------------
# Modules tab
# ---------------------------------------------------------------------------


def _row(config_dir: Path, workspace: Path) -> dict[str, object]:
    return next(r for r in modules.state(str(config_dir), str(workspace)) if r["name"] == "localai")


def test_the_localai_row_is_read_only_for_native(native: Path, workspace: Path) -> None:
    row = _row(native, workspace)

    assert row["enabled"] is True
    assert row["switchable"] is False
    assert row["runtime"] == "native"
    assert row["variant"] is None
    assert row["can_delete"] is False
    assert "papaia-ctl setup" in str(row["note"])
    assert "--localai-runtime=container" in str(row["note"])
    # The model download job is the one container the module runs.
    assert row["services"] == 1


def test_the_localai_row_of_the_container_runtime_is_unchanged(
    container: Path, workspace: Path
) -> None:
    row = _row(container, workspace)

    assert row["switchable"] is True
    assert row["runtime"] == "container"
    assert row["variant"] == "cpu"
    assert row["note"] == ""
    assert row["services"] == 2


def test_a_switch_of_a_native_localai_is_refused(native: Path, workspace: Path) -> None:
    runtime = modules.stored_runtime(str(native))
    with pytest.raises(modules.ModuleError, match="papaia-ctl setup"):
        modules.validate_enable("localai", "cpu", runtime=runtime)
    with pytest.raises(modules.ModuleError, match="papaia-ctl setup"):
        modules.validate_enable("localai", None, runtime=runtime)
    with pytest.raises(modules.ModuleError, match="papaia-ctl setup"):
        modules.validate_scope(
            "localai", "keep", None, str(workspace), config_dir=str(native)
        )


def test_the_other_modules_are_switched_as_before_with_a_native_localai(
    native: Path, workspace: Path
) -> None:
    runtime = modules.stored_runtime(str(native))
    assert modules.validate_enable("web-search", None, runtime=runtime) is None
    assert (
        modules.validate_scope("rag", "keep", None, str(workspace), config_dir=str(native))
        == "keep"
    )


def test_the_container_runtime_switches_localai_as_before(
    container: Path, workspace: Path
) -> None:
    runtime = modules.stored_runtime(str(container))
    assert modules.validate_enable("localai", "cpu", runtime=runtime) == "cpu"
    assert (
        modules.validate_scope("localai", "keep", None, str(workspace), config_dir=str(container))
        == "keep"
    )


def test_the_revision_changes_with_the_runtime(native: Path, tmp_path: Path) -> None:
    before = modules.revision(str(native))
    (native / ".env").write_text(
        "COMPOSE_PROJECT_NAME=papaia\nCOMPOSE_PROFILES=librechat,localai\n", encoding="utf-8"
    )
    assert modules.revision(str(native)) != before


def test_the_revision_of_the_container_runtime_is_what_it_was(container: Path) -> None:
    # A page loaded before this change is not called stale by it.
    import hashlib

    material = "librechat,localai\n"
    assert modules.revision(str(container)) == hashlib.sha256(material.encode()).hexdigest()[:16]
    (container / ".env").write_text(
        "COMPOSE_PROFILES=librechat,localai\nLOCALAI_RUNTIME=container\n", encoding="utf-8"
    )
    assert modules.revision(str(container)) == hashlib.sha256(material.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------


def _image(image_id: str, *tags: str, size: int = 1000) -> LocalImage:
    return LocalImage(id=image_id, tags=tags, size=size)


_DECLARED = {
    "core": frozenset({"localai/localai:v4.7.1", "curlimages/curl:latest", "librechat/app:v1"})
}
_LOCAL = [
    _image("sha256:old-localai", "localai/localai:v4.6.0"),
    _image("sha256:old-localai-gpu", "localai/localai:v4.6.0-gpu-nvidia-cuda-13"),
    _image("sha256:old-librechat", "librechat/app:v0"),
]


def test_no_localai_image_is_offered_for_native(native: Path, workspace: Path) -> None:
    withheld = images.withheld_repositories(str(native), str(workspace))
    report = build_report(_DECLARED, _LOCAL, used=set(), withheld=withheld)

    assert withheld == frozenset({"localai/localai"})
    assert [i.name for i in report.outdated] == ["librechat/app:v0"]


def test_the_container_runtime_offers_the_outdated_localai_images(
    container: Path, workspace: Path
) -> None:
    withheld = images.withheld_repositories(str(container), str(workspace))
    report = build_report(_DECLARED, _LOCAL, used=set(), withheld=withheld)

    assert withheld == frozenset()
    assert {i.name for i in report.outdated} == {
        "localai/localai:v4.6.0",
        "localai/localai:v4.6.0-gpu-nvidia-cuda-13",
        "librechat/app:v0",
    }


def test_the_localai_repository_comes_from_the_core_compose_file(
    native: Path, workspace: Path
) -> None:
    compose = workspace / "papaia" / "src" / "ai" / "localai" / "docker-compose.yml"
    compose.write_text(
        _LOCALAI_COMPOSE.replace("localai/localai:v4.7.1", "quay.io/go-skynet/local-ai:v4.8.0"),
        encoding="utf-8",
    )
    assert images.withheld_repositories(str(native), str(workspace)) == frozenset(
        {"localai/localai", "quay.io/go-skynet/local-ai"}
    )
    compose.unlink()
    assert images.withheld_repositories(str(native), str(workspace)) == frozenset(
        {"localai/localai"}
    )


# ---------------------------------------------------------------------------
# Restore scopes
# ---------------------------------------------------------------------------


def _manifest(*extra: dict[str, Any]) -> dict[str, Any]:
    def volume(module: str, key: str) -> dict[str, Any]:
        return {
            "kind": "volume",
            "module": module,
            "owner": "core",
            "target": f"papaia_{key}",
            "project": "papaia",
            "profiles": [module],
        }

    return {
        "core_project": "papaia",
        "artifacts": [
            {"kind": "configdir", "target": "config.tar.gz"},
            volume("localai", "localai-data"),
            volume("localai", "localai-models"),
            volume("librechat", "librechat-mongodb"),
            *extra,
        ],
    }


def test_no_localai_volume_is_offered_for_native() -> None:
    groups = restore_scope.build_groups(_manifest(), localai_native=True)

    assert [g.selector for g in groups] == ["config", "module:librechat"]
    allowed = restore_scope.allowed_selectors(groups)
    assert not any("localai" in selector for selector in allowed)


def test_the_container_runtime_offers_the_localai_volumes() -> None:
    groups = restore_scope.build_groups(_manifest())

    localai = next(g for g in groups if g.selector == "module:localai")
    assert [i.target for i in localai.items] == ["papaia_localai-data", "papaia_localai-models"]


def test_an_add_on_volume_of_the_same_name_is_still_offered_for_native() -> None:
    addon = {
        "kind": "volume",
        "module": "llm",
        "owner": "addon:llm",
        "target": "llm_localai-data",
        "project": "llm",
    }
    groups = restore_scope.build_groups(_manifest(addon), localai_native=True)

    assert [i.target for g in groups if g.selector == "addon:llm" for i in g.items] == [
        "llm_localai-data"
    ]
