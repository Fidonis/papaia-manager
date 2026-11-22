"""The services page read through papaia-docker-gate, with no Docker socket in the manager."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core import docker_gate, services
from app.core.docker_gate import GateClient, GateUnavailableError
from app.core.services import ServiceHealth
from tests.fake_docker_gate import Container, FakeGate

# The app with a signed-in administrator, as the node tests build it.
from tests.test_api_nodes import _session, client  # noqa: F401

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def deployment(tmp_path: Path) -> Path:
    """Config and workspace in one directory: a core with two services and one add-on."""
    (tmp_path / ".env").write_text(
        "COMPOSE_PROJECT_NAME=papaia\nCOMPOSE_PROFILES=librechat\n", encoding="utf-8"
    )
    src = tmp_path / "papaia" / "src"
    (src / "chat").mkdir(parents=True)
    (src / "docker-compose.yml").write_text("include:\n  - chat/docker-compose.yml\n")
    (src / "chat" / "docker-compose.yml").write_text(
        "services:\n"
        "  librechat:\n"
        "    restart: unless-stopped\n"
        "    profiles: [librechat]\n"
        "    labels:\n"
        "      de.fidonis.module: papaia-librechat\n"
        "      de.fidonis.role: chat-interface\n"
        "  librechat-init:\n"
        "    restart: \"no\"\n"
        "    profiles: [librechat]\n"
        "    labels:\n"
        "      de.fidonis.module: papaia-librechat\n"
        "      de.fidonis.role: init\n",
        encoding="utf-8",
    )
    addon = tmp_path / "addons" / "_managed" / "fidonis" / "paperless"
    addon.mkdir(parents=True)
    (addon / "docker-compose.yml").write_text(
        "services:\n"
        "  paperless:\n"
        "    labels:\n"
        "      de.fidonis.module: papaia-paperless\n",
        encoding="utf-8",
    )
    (tmp_path / "deployment.yaml").write_text(
        f"addons:\n  - name: paperless\n    path: {addon}\n    active: true\n", encoding="utf-8"
    )
    return tmp_path


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> FakeGate:
    fake = FakeGate(
        containers=[
            Container("papaia-librechat-1", "papaia", "librechat", module="papaia-librechat",
                      role="chat-interface", status="Up 2 hours (healthy)"),
            Container("papaia-librechat-init-1", "papaia", "librechat-init",
                      module="papaia-librechat", role="init", state="exited",
                      status="Exited (0) 2 hours ago"),
            # Its own compose project, which the gate does not name.
            Container("paperless-paperless-1", "paperless", "paperless",
                      module="papaia-paperless"),
        ]
    )
    monkeypatch.setattr(
        docker_gate,
        "default_client",
        lambda: GateClient("http://papaia-docker-gate:2375", transport=fake.transport()),
    )
    monkeypatch.setattr(services, "_CACHE", None)
    return fake


def test_the_status_comes_from_the_gate(deployment: Path, gate: FakeGate) -> None:
    snapshot = services.load_snapshot(str(deployment), str(deployment))

    assert [m.name for m in snapshot.core] == ["librechat"]
    containers = {c.name: c for c in snapshot.core[0].containers}
    assert containers["papaia-librechat-1"].health is ServiceHealth.HEALTHY
    assert containers["papaia-librechat-1"].role == "chat-interface"
    # A one-shot that is allowed to finish stays completed.
    assert containers["papaia-librechat-init-1"].health is ServiceHealth.COMPLETED
    assert snapshot.docker_problem == ""
    assert gate.calls() == [("GET", "/containers/json")]


def test_a_service_meant_to_keep_running_reads_as_stopped_after_a_clean_exit(
    deployment: Path, gate: FakeGate
) -> None:
    # A stop through the gate ends the process cleanly. The compose file says the service
    # is meant to keep running, so its clean exit is an outage, not a finished job.
    gate.containers[0].state = "exited"
    gate.containers[0].status = "Exited (0) 1 minute ago"

    snapshot = services.load_snapshot(str(deployment), str(deployment))

    containers = {c.name: c for c in snapshot.core[0].containers}
    assert containers["papaia-librechat-1"].health is ServiceHealth.STOPPED
    assert containers["papaia-librechat-init-1"].health is ServiceHealth.COMPLETED
    assert snapshot.groups[0].health is ServiceHealth.STOPPED


def test_an_add_on_the_gate_does_not_show_is_unknown_not_missing(
    deployment: Path, gate: FakeGate
) -> None:
    snapshot = services.load_snapshot(str(deployment), str(deployment))

    [paperless] = snapshot.addons
    assert paperless.health is ServiceHealth.UNKNOWN
    assert paperless.containers[0].status_display == "not visible through papaia-docker-gate"


def test_a_missing_gate_is_reported_as_not_supported_and_not_as_an_outage(
    deployment: Path, gate: FakeGate
) -> None:
    gate.down = True

    snapshot = services.load_snapshot(str(deployment), str(deployment))

    assert snapshot.core == [] and snapshot.addons == []
    assert "not supported" in snapshot.docker_problem
    # Not cached: the next poll asks again.
    assert services._CACHE is None  # noqa: SLF001


def test_the_services_partial_shows_the_not_supported_message(
    client: TestClient,  # noqa: F811 - the fixture imported above
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    problem = GateUnavailableError("http://papaia-docker-gate:2375").message
    snapshot = services.StackSnapshot(docker_problem=problem)
    monkeypatch.setattr("app.routers.ui.load_snapshot", lambda *a: snapshot)

    html = _session(client).get("/partials/services").text

    assert "papaia-docker-gate does not answer" in html
    assert "not supported" in html
    assert "Docker socket" not in html


def test_the_manager_compose_file_mounts_no_docker_socket() -> None:
    text = (REPO / "docker" / "docker-compose.yml").read_text(encoding="utf-8")
    assert "docker.sock" not in text
    assert "DOCKER_GID" not in text
