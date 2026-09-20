"""The image-cleanup half of the upgrade API.

Two things are asserted here: that a request cannot widen what `images.prune`
decides may be removed, and that the cleanup never fails an operation that has
already happened -- a dismissed upgrade stays dismissed however the follow-up
goes. The verdict itself is in `test_images.py`; nothing here needs Docker.
"""
from __future__ import annotations

import base64
import json
import os
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

_SESSION_SECRET = "test-session-secret-value"
_CONFIG_DIR = tempfile.mkdtemp(prefix="papaia-config-")
_WORKSPACE_DIR = tempfile.mkdtemp(prefix="papaia-workspace-")

os.environ.update(
    OIDC_ISSUER_KC_AUTH="https://kc.test/auth",
    OIDC_ISSUER_KC_TOKEN="https://kc.test/token",
    OIDC_ISSUER_KC_CERTS="https://kc.test/certs",
    MANAGER_ADMIN_ROLE="admin",
    MANAGER_USER_ROLE="user",
    MANAGER_HOST="http://localhost:8120",
    MANAGER_OIDC_CLIENT_SECRET="client-secret",
    MANAGER_SESSION_SECRET=_SESSION_SECRET,
)
# PAPAIA_CONFIG_DIR / PAPAIA_WORKSPACE_DIR are set per test by the fixture, for
# the reason spelled out in test_api_restore_scoped.py.

from fastapi.testclient import TestClient  # noqa: E402
from itsdangerous import TimestampSigner  # noqa: E402

from app import main as app_main  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.core import images, runner, upgrade  # noqa: E402
from app.core.jobs import JobQueue  # noqa: E402
from app.core.upgrade import (  # noqa: E402
    CheckoutState,
    Gate,
    UpgradeCheck,
)
from app.main import create_app  # noqa: E402
from app.routers import api_upgrade  # noqa: E402

_CSRF = "test-csrf-token"
_OUTDATED = "sha256:" + "a" * 64
_OTHER = "sha256:" + "b" * 64


def _image(image_id: str = _OUTDATED, name: str = "postgres:15") -> images.OutdatedImage:
    return images.OutdatedImage(
        id=image_id, name=name, remove=(name,), size=650_000_000, created="2026-01-01T00:00:00Z",
        source="core",
    )


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("PAPAIA_CONFIG_DIR", _CONFIG_DIR)
    monkeypatch.setenv("PAPAIA_WORKSPACE_DIR", _WORKSPACE_DIR)
    get_settings.cache_clear()
    app_main._job_queue = JobQueue(config_dir=_CONFIG_DIR)  # noqa: SLF001

    async def idle(kind: object = None) -> None:
        return None

    monkeypatch.setattr(api_upgrade.runner, "find_runner", idle)
    yield TestClient(create_app(), follow_redirects=False)
    get_settings.cache_clear()
    app_main._job_queue = None  # noqa: SLF001


def _admin(client: TestClient) -> TestClient:
    session: dict[str, Any] = {
        "user": {
            "sub": "u-1",
            "preferred_username": "tester",
            "roles": ["admin"],
            "exp": int(time.time()) + 3600,
        },
        "_csrf_token": _CSRF,
    }
    payload = base64.b64encode(json.dumps(session).encode())
    client.cookies.clear()
    client.cookies.set(
        "papaia_manager_session", TimestampSigner(_SESSION_SECRET).sign(payload).decode()
    )
    return client


def _post(client: TestClient, url: str, body: dict[str, Any] | None = None) -> Any:
    return _admin(client).post(url, json=body or {}, headers={"X-CSRF-Token": _CSRF})


def _audit() -> list[dict[str, Any]]:
    path = Path(_CONFIG_DIR, "manager", "audit.log")
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


@pytest.fixture(autouse=True)
def _fresh_audit_log() -> None:
    Path(_CONFIG_DIR, "manager", "audit.log").unlink(missing_ok=True)


def _fake_prune(monkeypatch: pytest.MonkeyPatch, result: images.PruneResult) -> list[Any]:
    calls: list[Any] = []

    async def prune(config: str, workspace: str, *, only: Any = None) -> images.PruneResult:
        calls.append(only)
        return result

    monkeypatch.setattr(images, "prune", prune)
    return calls


def _status(*, running: bool, prune: bool, exit_code: int = 0) -> runner.RunnerStatus:
    return runner.RunnerStatus(
        name="papaia-upgrade-1.2.0",
        target="1.2.0",
        status="running" if running else "exited",
        exit_code=None if running else exit_code,
        started_at="2026-09-20T10:00:00Z",
        finished_at="",
        prune_images=prune,
    )


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def test_the_listing_needs_an_administrator(client: TestClient) -> None:
    client.cookies.clear()
    assert client.get("/api/v1/upgrade/images").status_code == 401


def test_the_listing_carries_size_and_source(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def report(config: str, workspace: str) -> images.ImageReport:
        return images.ImageReport(outdated=[_image()])

    monkeypatch.setattr(images, "gather_report", report)
    body = _admin(client).get("/api/v1/upgrade/images").json()
    assert body["errors"] == []
    assert body["total_size_human"] == "650.0 MB"
    assert body["outdated"][0]["name"] == "postgres:15"
    assert body["outdated"][0]["source"] == "core"


def test_the_images_section_renders_rows_and_the_fail_closed_reason(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def with_rows(config: str, workspace: str) -> images.ImageReport:
        return images.ImageReport(outdated=[_image()])

    monkeypatch.setattr(images, "gather_report", with_rows)
    html = _admin(client).get("/partials/upgrade/images").text
    assert "postgres:15" in html
    assert f'value="{_OUTDATED}"' in html
    assert "Remove all (1)" in html

    async def unreadable(config: str, workspace: str) -> images.ImageReport:
        return images.ImageReport(errors=["core: docker compose config failed: boom"])

    monkeypatch.setattr(images, "gather_report", unreadable)
    html = _admin(client).get("/partials/upgrade/images").text
    assert "cannot be determined" in html
    assert "boom" in html
    assert "Remove all" not in html


def test_the_images_section_is_part_of_the_upgrade_page(client: TestClient) -> None:
    html = _admin(client).get("/upgrade").text
    assert 'hx-get="/partials/upgrade/images"' in html
    assert 'id="prune-images-modal"' in html
    # The dialog offers the cleanup, and offers it selected.
    assert 'x-model="pruneImages"' in html
    assert "pruneImages: true" in html


def test_the_menu_entry_and_the_heading_say_upgrade(client: TestClient) -> None:
    html = _admin(client).get("/upgrade").text
    assert '<span class="sidebar-label">Upgrade</span>' in html
    assert "<title>Upgrade — papAIa manager</title>" in html
    assert 'aria-label="Update"' not in html


# ---------------------------------------------------------------------------
# Removal
# ---------------------------------------------------------------------------


def test_removal_needs_the_csrf_token(client: TestClient) -> None:
    response = _admin(client).post("/api/v1/upgrade/images/prune", json={})
    assert response.status_code == 403


def test_a_malformed_id_is_refused_before_anything_is_computed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_prune(monkeypatch, images.PruneResult())
    response = _post(client, "/api/v1/upgrade/images/prune", {"images": ["postgres:15"]})
    assert response.status_code == 400
    assert calls == []


def test_removal_is_refused_while_an_upgrade_runs(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_prune(monkeypatch, images.PruneResult())

    async def running(kind: runner.RunnerKind) -> runner.RunnerStatus | None:
        return _status(running=True, prune=False) if kind is runner.UPGRADE_KIND else None

    monkeypatch.setattr(api_upgrade.runner, "find_runner", running)
    response = _post(client, "/api/v1/upgrade/images/prune")
    assert response.status_code == 409
    assert "an upgrade is still running" in response.json()["detail"]
    assert calls == []


def test_a_finished_upgrade_does_not_block_removal(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Its outcome is what the operator is looking at before cleaning up.
    calls = _fake_prune(monkeypatch, images.PruneResult())

    async def finished(kind: runner.RunnerKind) -> runner.RunnerStatus | None:
        return _status(running=False, prune=True) if kind is runner.UPGRADE_KIND else None

    monkeypatch.setattr(api_upgrade.runner, "find_runner", finished)
    assert _post(client, "/api/v1/upgrade/images/prune").status_code == 200
    assert calls == [None]


def test_removal_names_only_the_requested_ids_and_is_audited(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = images.PruneResult(removed=[_image()], failed=[(_image(_OTHER, "redis:8"), "in use")])
    calls = _fake_prune(monkeypatch, result)

    response = _post(client, "/api/v1/upgrade/images/prune", {"images": [_OUTDATED, _OTHER]})

    assert response.status_code == 200
    body = response.json()
    assert [i["name"] for i in body["removed"]] == ["postgres:15"]
    assert body["failed"] == [{**body["failed"][0], "error": "in use"}]
    assert body["reclaimed"] == 650_000_000
    assert calls == [{_OUTDATED, _OTHER}]
    (entry,) = [e for e in _audit() if e["action"] == "images-prune"]
    assert entry["target"] == "manual"
    assert entry["params"]["removed"] == ["postgres:15"]
    assert entry["result"] == "partial"


def test_removal_without_a_list_means_every_outdated_image(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_prune(monkeypatch, images.PruneResult())
    assert _post(client, "/api/v1/upgrade/images/prune").status_code == 200
    assert calls == [None]


def test_unreadable_declarations_are_a_conflict_not_a_server_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def prune(config: str, workspace: str, *, only: Any = None) -> images.PruneResult:
        raise images.ImagesError("core: docker compose config failed: boom")

    monkeypatch.setattr(images, "prune", prune)
    response = _post(client, "/api/v1/upgrade/images/prune")
    assert response.status_code == 409
    assert "boom" in response.json()["detail"]
    assert _audit() == []


# ---------------------------------------------------------------------------
# Dismissing a finished upgrade
# ---------------------------------------------------------------------------


def _finished_runner(monkeypatch: pytest.MonkeyPatch, status: runner.RunnerStatus) -> list[str]:
    cleared: list[str] = []

    async def find(kind: runner.RunnerKind) -> runner.RunnerStatus | None:
        return status if kind is runner.UPGRADE_KIND else None

    async def clear(name: str, kind: runner.RunnerKind = runner.RESTORE_KIND) -> None:
        cleared.append(name)

    monkeypatch.setattr(api_upgrade.runner, "find_runner", find)
    monkeypatch.setattr(api_upgrade.runner, "clear_runner", clear)
    return cleared


def test_dismissing_a_successful_cleanup_run_removes_what_the_runner_held(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    cleared = _finished_runner(monkeypatch, _status(running=False, prune=True))
    previous = _image(name="ghcr.io/x/manager:1.0.0")
    calls = _fake_prune(monkeypatch, images.PruneResult(removed=[previous]))

    body = _post(client, "/api/v1/upgrade/runner/clear").json()

    assert cleared == ["papaia-upgrade-1.2.0"]
    assert calls == [None]
    assert body["status"] == "cleared"
    assert [i["name"] for i in body["images"]["removed"]] == ["ghcr.io/x/manager:1.0.0"]
    (entry,) = [e for e in _audit() if e["action"] == "images-prune"]
    assert entry["target"] == "dismiss"


def test_a_failed_cleanup_never_fails_the_dismissal(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    cleared = _finished_runner(monkeypatch, _status(running=False, prune=True))

    async def prune(config: str, workspace: str, *, only: Any = None) -> images.PruneResult:
        raise images.ImagesError("daemon went away")

    monkeypatch.setattr(images, "prune", prune)

    response = _post(client, "/api/v1/upgrade/runner/clear")

    assert response.status_code == 200
    assert cleared == ["papaia-upgrade-1.2.0"]
    assert response.json()["images"] == {"removed": [], "error": "daemon went away"}


@pytest.mark.parametrize(
    "status",
    [
        # Started without the option.
        _status(running=False, prune=False),
        # A failed upgrade: the checkout and the configuration may disagree, and
        # with them what is still needed.
        _status(running=False, prune=True, exit_code=3),
    ],
    ids=["not requested", "failed run"],
)
def test_dismissing_anything_else_leaves_the_images_alone(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, status: runner.RunnerStatus
) -> None:
    _finished_runner(monkeypatch, status)
    calls = _fake_prune(monkeypatch, images.PruneResult())

    body = _post(client, "/api/v1/upgrade/runner/clear").json()

    assert body == {"status": "cleared"}
    assert calls == []


# ---------------------------------------------------------------------------
# Starting an upgrade with the option
# ---------------------------------------------------------------------------


def _check() -> UpgradeCheck:
    return UpgradeCheck(
        current="1.0.0", target="1.2.0", tag="v1.2.0", status="ok", gate=Gate(passed=True)
    )


def _start(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]
) -> dict[str, Any]:
    started: dict[str, Any] = {}

    async def clean_checkout(workspace_dir: str) -> CheckoutState:
        return CheckoutState(is_git=True, clean=True, tag="v1.0.0")

    async def start(**kwargs: Any) -> runner.RunnerStatus:
        started.update(kwargs)
        return _status(running=True, prune=kwargs["prune_images"])

    monkeypatch.setattr(upgrade, "cached_check", _check)
    monkeypatch.setattr(upgrade, "checkout_state", clean_checkout)
    monkeypatch.setattr(api_upgrade.runner, "start_upgrade", start)
    response = _post(client, "/api/v1/upgrade", {"version": "1.2.0", "no_backup": True, **body})
    assert response.status_code == 202, response.text
    return started


def test_the_api_does_not_prune_unless_asked(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The page preselects the option; a script that never heard of it must not
    # start deleting images.
    assert _start(client, monkeypatch, {})["prune_images"] is False


def test_the_option_reaches_the_runner_and_the_audit_log(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _start(client, monkeypatch, {"prune_images": True})["prune_images"] is True
    (entry,) = [e for e in _audit() if e["action"] == "upgrade"]
    assert entry["params"]["prune_images"] is True


# ---------------------------------------------------------------------------
# The runner panel
# ---------------------------------------------------------------------------


def _render_runner(status: runner.RunnerStatus, log: str) -> str:
    from app.templating import templates

    return templates.env.get_template("partials/upgrade_runner.html").render(
        upgrade_runner=status,
        upgrade_log=log,
        upgrade_error="",
        phases=upgrade.phases_from_log(
            log, running=status.is_running, prune_images=status.prune_images
        ),
        recovery="",
        recovery_generated=False,
    )


_LOG = (
    "[papaia-ctl] Starting the stack...\n"
    "[ok] upgrade complete: 1.0.0 -> 1.2.0\n"
    "[papaia-manager] Removing outdated Docker images...\n"
    "[papaia-manager] Image cleanup finished: 3 removed, up to 4.2 GB reclaimed\n"
)


def test_the_panel_lists_the_cleanup_step_and_what_it_did() -> None:
    html = _render_runner(_status(running=False, prune=True), _LOG)
    assert "Remove outdated Docker images" in html
    assert "3 removed, up to 4.2 GB reclaimed" in html


def test_the_panel_says_the_previous_panel_image_goes_on_dismissal() -> None:
    html = _render_runner(_status(running=False, prune=True), _LOG)
    assert "previous papaia-manager image" in html
    assert "when you dismiss" in html


def test_a_run_without_the_cleanup_shows_neither() -> None:
    html = _render_runner(_status(running=False, prune=False), _LOG)
    assert "Remove outdated Docker images" not in html
    assert "previous papaia-manager image" not in html


def test_a_skipped_cleanup_shows_why() -> None:
    log = _LOG.replace(
        "Image cleanup finished: 3 removed, up to 4.2 GB reclaimed",
        "Image cleanup skipped -- nothing was removed: core: docker compose config failed: boom",
    )
    html = _render_runner(_status(running=False, prune=True), log)
    assert "skipped" in html
    assert "core: docker compose config failed: boom" in html
