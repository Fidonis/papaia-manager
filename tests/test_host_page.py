"""The Host page, its nav dot, and the Host row in the sidebar status.

What matters most here is the edge between the two audiences. The page names
paths and host names and is for administrators; the chip is in front of every
authenticated account and may only say how many checks are fine, never which
disk or which domain is not.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

_CONFIG_DIR = tempfile.mkdtemp(prefix="papaia-host-config-")
_WORKSPACE_DIR = tempfile.mkdtemp(prefix="papaia-host-workspace-")

for _key, _value in {
    "OIDC_ISSUER_KC_AUTH": "https://kc.test/auth",
    "OIDC_ISSUER_KC_TOKEN": "https://kc.test/token",
    "OIDC_ISSUER_KC_CERTS": "https://kc.test/certs",
    "MANAGER_ADMIN_ROLE": "admin",
    "MANAGER_USER_ROLE": "user",
    "MANAGER_HOST": "http://localhost:8120",
    "MANAGER_OIDC_CLIENT_SECRET": "client-secret",
    "MANAGER_SESSION_SECRET": "test-session-secret-value",
    "PAPAIA_CONFIG_DIR": _CONFIG_DIR,
    "PAPAIA_WORKSPACE_DIR": _WORKSPACE_DIR,
}.items():
    os.environ.setdefault(_key, _value)

from fastapi.testclient import TestClient  # noqa: E402
from itsdangerous import TimestampSigner  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.core import host_health  # noqa: E402
from app.core.services import (  # noqa: E402
    ServiceContainer,
    ServiceHealth,
    ServiceModule,
    StackSnapshot,
)
from app.main import create_app  # noqa: E402
from app.routers import ui  # noqa: E402

_GIB = 1024**3
_BACKUP_PATH = "/mnt/backup-secret-volume"
_DOMAIN = "ai.internal-example.com"
_LE = f"infra/nginx/nginx-letsencrypt/live/{_DOMAIN}/fullchain.pem"


def _doc(*, backup_status: str = "warn", cert_status: str = "warn") -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "generated_at": "2026-10-02T12:00:00Z",
            "platform_version": "1.4.0",
            "checks": [
                {
                    "name": "disk_space",
                    "status": backup_status,
                    "summary": "",
                    "details": {
                        "paths": [
                            {
                                "label": "config_dir",
                                "path": "/srv/papaia-config",
                                "free_bytes": 189 * _GIB,
                                "total_bytes": 320 * _GIB,
                                "status": "pass",
                            },
                            {
                                "label": "backup_dir",
                                "path": _BACKUP_PATH,
                                "free_bytes": 9 * _GIB,
                                "total_bytes": 100 * _GIB,
                                "status": backup_status,
                            },
                        ],
                        "notes": [],
                    },
                },
                {
                    "name": "certs",
                    "status": cert_status,
                    "summary": "",
                    "details": {
                        "certificates": [
                            {
                                "path": _LE,
                                "not_after": "2026-10-14T11:15:00Z",
                                "days_left": 12,
                                "status": cert_status,
                            },
                            {
                                "path": "certs/keycloak.crt",
                                "not_after": "2036-06-03T11:15:00Z",
                                "days_left": 3412,
                                "status": "pass",
                            },
                        ]
                    },
                },
            ],
            "summary": {"pass": 0, "warn": 0, "fail": 0, "skip": 0},
            "ok": True,
        }
    )


class _Doctor:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.calls = 0

    async def __call__(self, **_: Any) -> tuple[int, str, str]:
        self.calls += 1
        return 0, self.stdout, ""


def _healthy_core() -> StackSnapshot:
    container = ServiceContainer(
        name="papaia-keycloak-1",
        service="keycloak",
        role="",
        state="running",
        status_text="Up 2 hours (healthy)",
        health=ServiceHealth.HEALTHY,
    )
    return StackSnapshot(core=[ServiceModule(name="keycloak", containers=[container])])


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    host_health.reset_cache()
    yield
    host_health.reset_cache()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A workspace whose core has a `doctor`."""
    lib = tmp_path / "workspace" / "papaia" / "tools" / "lib"
    lib.mkdir(parents=True)
    (lib / "doctor.py").write_text("", encoding="utf-8")
    return tmp_path / "workspace"


@pytest.fixture
def client(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    get_settings.cache_clear()
    settings = get_settings().model_copy(
        update={
            "papaia_config_dir": str(tmp_path / "config"),
            "papaia_workspace_dir": str(workspace),
        }
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    # The chip also reads the container snapshot; a healthy core makes the host
    # the only thing that can move its headline.
    monkeypatch.setattr(ui, "load_snapshot", lambda *_: _healthy_core())
    yield TestClient(app, follow_redirects=False)
    get_settings.cache_clear()


def _as(client: TestClient, *roles: str) -> TestClient:
    session: dict[str, Any] = {
        "user": {
            "sub": "u-1",
            "preferred_username": "tester",
            "roles": list(roles),
            "exp": int(time.time()) + 3600,
        }
    }
    payload = base64.b64encode(json.dumps(session).encode())
    signed = TimestampSigner(get_settings().manager_session_secret).sign(payload).decode()
    client.cookies.clear()
    client.cookies.set("papaia_manager_session", signed)
    return client


def _prime(monkeypatch: pytest.MonkeyPatch, workspace: Path, stdout: str) -> _Doctor:
    """Run the host check once so the cache-only views have something to read."""
    stub = _Doctor(stdout)
    monkeypatch.setattr(host_health, "run_py_cli", stub)
    asyncio.run(host_health.load_host_health(config_dir="/cfg", workspace_dir=str(workspace)))
    return stub


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


def test_the_page_shell_polls_its_partial_and_offers_a_recheck(client: TestClient) -> None:
    body = _as(client, "admin").get("/host").text

    assert 'hx-get="/partials/host"' in body
    assert "every 60s" in body
    assert "Re-check" in body
    assert "/partials/host?fresh=true" in body


def test_the_partial_lists_disks_and_certificates_with_the_cores_verdicts(
    client: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prime(monkeypatch, workspace, _doc())

    body = _as(client, "admin").get("/partials/host").text

    assert "Config disk" in body
    assert "/srv/papaia-config" in body
    assert "189 GiB free of 320 GiB" in body
    assert "Backup disk" in body
    assert _BACKUP_PATH in body
    assert "9.0 GiB free of 100 GiB" in body
    assert "91 % used" in body
    assert "progress-warning" in body
    assert _DOMAIN in body
    assert "Let&#39;s Encrypt" in body
    assert "12 days" in body
    assert "expires 14 Oct 2026" in body
    assert "keycloak.crt" in body
    assert "3412 days" in body


def test_a_docker_root_the_panel_cannot_see_is_said_so_in_the_list(
    client: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prime(monkeypatch, workspace, _doc())

    body = _as(client, "admin").get("/partials/host").text

    assert "Docker data" in body
    assert "Not measurable from this panel" in body


def test_a_critical_check_is_drawn_as_critical(
    client: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prime(monkeypatch, workspace, _doc(backup_status="fail"))

    body = _as(client, "admin").get("/partials/host").text

    assert "progress-error" in body
    assert "border-l-error" in body


def test_a_core_without_doctor_gets_an_explanation_instead_of_a_blank_page(
    client: TestClient, tmp_path: Path
) -> None:
    empty = tmp_path / "old-core"
    empty.mkdir()
    settings = get_settings().model_copy(update={"papaia_workspace_dir": str(empty)})
    client.app.dependency_overrides[get_settings] = lambda: settings  # type: ignore[attr-defined]

    body = _as(client, "admin").get("/partials/host").text

    assert "Host checks are not available" in body
    assert "1.4.0" in body


def test_re_checking_twice_in_a_row_runs_doctor_once(
    client: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _prime(monkeypatch, workspace, _doc())
    admin = _as(client, "admin")

    admin.get("/partials/host?fresh=true")
    admin.get("/partials/host?fresh=true")

    assert stub.calls == 1


# ---------------------------------------------------------------------------
# The chip
# ---------------------------------------------------------------------------


def test_the_chip_counts_the_host_in_for_every_role_and_shows_only_numbers(
    client: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prime(monkeypatch, workspace, _doc())

    for roles in (("user",), ("admin",)):
        body = _as(client, *roles).get("/partials/service-status").text

        assert "Host" in body
        # Two disks and two certificates; the backup disk and the Let's Encrypt
        # certificate are the two that warn.
        assert "2 / 4 ok" in body
        assert "2 issues" in body
        # What the page shows to administrators must not reach the chip.
        for secret in (_BACKUP_PATH, _DOMAIN, "/srv/papaia-config", "Backup disk", "keycloak.crt"):
            assert secret not in body


def test_only_an_administrator_is_pointed_at_the_host_page(
    client: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prime(monkeypatch, workspace, _doc())

    assert 'href="/host"' not in _as(client, "user").get("/partials/service-status").text
    assert 'href="/host"' in _as(client, "admin").get("/partials/service-status").text


def test_a_healthy_host_leaves_the_chip_green(
    client: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prime(monkeypatch, workspace, _doc(backup_status="pass", cert_status="pass"))

    body = _as(client, "user").get("/partials/service-status").text

    assert "All healthy" in body
    assert "4 / 4 ok" in body
    # A row of the sidebar, not a pill: in the rail the label goes and the dot stays.
    assert '<span class="sidebar-label">All healthy</span>' in body


def test_a_critical_host_turns_the_chip_red_without_touching_the_stack_rows(
    client: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prime(monkeypatch, workspace, _doc(backup_status="fail", cert_status="pass"))

    body = _as(client, "user").get("/partials/service-status").text

    assert "text-error" in body
    assert "1 issue" in body
    # The core stack row still reads fully running: the host has its own row.
    assert "1 / 1 running" in body


def test_without_a_reading_the_chip_has_no_host_row_and_is_not_dragged_down(
    client: TestClient, tmp_path: Path
) -> None:
    empty = tmp_path / "old-core"
    empty.mkdir()
    settings = get_settings().model_copy(update={"papaia_workspace_dir": str(empty)})
    client.app.dependency_overrides[get_settings] = lambda: settings  # type: ignore[attr-defined]

    body = _as(client, "user").get("/partials/service-status").text

    assert "All healthy" in body
    assert "ok</span>" not in body


# ---------------------------------------------------------------------------
# The sidebar
# ---------------------------------------------------------------------------


def _between(body: str, start: str, end: str) -> str:
    return body[body.index(start) : body.index(end)]


def _nav_groups(body: str) -> dict[str, list[str]]:
    """Caption -> the entries under it, in order; the caption-less lead is ''."""
    nav = _between(body, "<nav", "</nav>")
    parts = re.split(r'<p class="sidebar-label[^>]*>([^<]+)</p>', nav)
    groups = {"": re.findall(r'aria-label="([^"]+)"', parts[0])}
    for caption, chunk in zip(parts[1::2], parts[2::2], strict=True):
        groups[caption] = re.findall(r'aria-label="([^"]+)"', chunk)
    return groups


def test_the_admin_nav_is_grouped_by_what_an_entry_acts_on(client: TestClient) -> None:
    groups = _nav_groups(_as(client, "admin").get("/host").text)

    assert list(groups) == ["", "Monitor", "Extensions", "System"]
    assert groups[""] == ["Dashboard"]
    assert groups["Monitor"] == ["Services", "Host", "Jobs"]
    assert groups["Extensions"] == ["Add-Ons", "Catalogs"]
    # Backup and upgrade are stack-level commands, so they sit with the system.
    assert groups["System"] == ["Backup / Restore", "Upgrade", "Audit log", "Settings"]


def test_a_user_without_the_admin_role_sees_the_dashboard_and_no_groups(
    client: TestClient,
) -> None:
    assert _nav_groups(_as(client, "user").get("/").text) == {"": ["Dashboard"]}


def test_the_status_row_is_in_the_sidebar_for_every_role_and_not_in_the_header(
    client: TestClient,
) -> None:
    for roles in (("user",), ("admin",)):
        body = _as(client, *roles).get("/").text
        header = _between(body, "<header", "</header>")
        sidebar = _between(body, "<aside", "</aside>")

        assert "/partials/service-status" not in header
        # Between the nav and the footer, so the footer's divider sits right
        # under it and the nav can scroll without taking it along.
        assert (
            sidebar.index("</nav>")
            < sidebar.index('hx-get="/partials/service-status"')
            < sidebar.index('aria-label="Toggle sidebar"')
        )


# ---------------------------------------------------------------------------
# The nav dot
# ---------------------------------------------------------------------------


def test_the_nav_dot_is_empty_until_there_is_something_to_look_at(
    client: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = _as(client, "admin")
    assert "rounded-full" not in admin.get("/partials/nav/host-indicator").text

    _prime(monkeypatch, workspace, _doc(backup_status="pass", cert_status="pass"))
    assert "rounded-full" not in admin.get("/partials/nav/host-indicator").text


def test_the_nav_dot_follows_the_worst_state(
    client: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prime(monkeypatch, workspace, _doc())
    assert "bg-warning" in _as(client, "admin").get("/partials/nav/host-indicator").text

    host_health.reset_cache()
    _prime(monkeypatch, workspace, _doc(backup_status="fail"))
    assert "bg-error" in _as(client, "admin").get("/partials/nav/host-indicator").text


def test_the_sidebar_links_to_the_host_page_for_administrators_only(client: TestClient) -> None:
    assert 'href="/host"' in _as(client, "admin").get("/host").text
    assert 'href="/host"' not in _as(client, "user").get("/").text
