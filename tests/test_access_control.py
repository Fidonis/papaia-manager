"""End-to-end access control: every route, through the real dependency stack.

The role predicates are unit-tested in `test_roles.py`. What this module adds
is the wiring proof: that each route actually carries the tier it should, and
that the JSON API is gated exactly like the pages. A route that silently loses
its dependency would pass every unit test and fail here.
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

# The seeded tiles resolve their links against the core .env, so the config
# directory has to look like a real one for the dashboard to render anything.
Path(_CONFIG_DIR, ".env").write_text("PAPAIA_HOST=https://papaia.test\n", encoding="utf-8")

# Settings are read at import time by the app factory, so the environment has
# to be in place before `app.*` is imported.
os.environ.update(
    OIDC_ISSUER_KC_AUTH="https://kc.test/auth",
    OIDC_ISSUER_KC_TOKEN="https://kc.test/token",
    OIDC_ISSUER_KC_CERTS="https://kc.test/certs",
    MANAGER_ADMIN_ROLE="admin",
    MANAGER_USER_ROLE="user",
    MANAGER_HOST="http://localhost:8120",
    MANAGER_OIDC_CLIENT_SECRET="client-secret",
    MANAGER_SESSION_SECRET=_SESSION_SECRET,
    PAPAIA_CONFIG_DIR=_CONFIG_DIR,
    PAPAIA_WORKSPACE_DIR=_WORKSPACE_DIR,
)

from fastapi.testclient import TestClient  # noqa: E402
from itsdangerous import TimestampSigner  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.main import create_app  # noqa: E402

# Surfaces reserved for administrators, and the dashboard surfaces every
# authenticated account reaches. Kept as data so a new route is one line.
ADMIN_PAGES = ["/addons", "/addons/paperless", "/catalogs", "/maintenance", "/services"]
ADMIN_PARTIALS = [
    "/partials/addons",
    "/partials/catalogs",
    "/partials/maintenance/restore-points",
    "/partials/maintenance/restore-status",
    "/partials/services",
]
ADMIN_APIS = [
    "/api/v1/addons",
    "/api/v1/catalogs",
    "/api/v1/jobs",
    "/api/v1/maintenance/backup-dir",
    "/api/v1/maintenance/restore-points",
]
# The status pill sits in the header of every page, so it has to answer for
# both roles even though the page it links to is admin-only.
DASHBOARD_PATHS = ["/", "/partials/tiles", "/partials/service-status"]

# Denials are decided by the dependency, before the handler runs, so every
# path above can be asserted on cheaply. Confirming that an admin gets through
# is different -- it runs the handler. `/api/v1/addons` queries Docker for
# running compose projects, and `/api/v1/jobs` needs the job queue that the
# skipped lifespan never started, so neither is asserted positively here. The
# two maintenance reads only touch the config directory's .env, which this
# module writes, so they are safe to exercise for real.
ADMIN_APIS_SELF_CONTAINED = [
    "/api/v1/catalogs",
    "/api/v1/maintenance/backup-dir",
    "/api/v1/maintenance/restore-points",
]


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    get_settings.cache_clear()
    # No `with` block on purpose: skipping the lifespan keeps the job-queue
    # worker thread and the workspace handshake out of these tests.
    yield TestClient(create_app(), follow_redirects=False)
    get_settings.cache_clear()


def _session_cookie(*roles: str) -> str:
    session: dict[str, Any] = {
        "user": {
            "sub": "u-1",
            "preferred_username": "tester",
            "roles": list(roles),
            "exp": int(time.time()) + 3600,
        }
    }
    payload = base64.b64encode(json.dumps(session).encode())
    return TimestampSigner(_SESSION_SECRET).sign(payload).decode()


def _as(client: TestClient, *roles: str) -> TestClient:
    # Every response carries a refreshed session cookie scoped to `testserver`,
    # while `cookies.set` stores a domain-less one. Without clearing first the
    # jar holds both and the stale session is the one that gets sent.
    client.cookies.clear()
    client.cookies.set("papaia_manager_session", _session_cookie(*roles))
    return client


# ---------------------------------------------------------------------------
# Unauthenticated
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [*DASHBOARD_PATHS, *ADMIN_PAGES, *ADMIN_APIS])
def test_anonymous_is_sent_to_login(client: TestClient, path: str) -> None:
    client.cookies.clear()
    response = client.get(path)
    assert response.status_code == 307
    assert response.headers["location"] == "/auth/login"


def test_health_stays_open(client: TestClient) -> None:
    client.cookies.clear()
    assert client.get("/health").status_code == 200


# ---------------------------------------------------------------------------
# Dashboard: reachable by both roles
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("roles", [("user",), ("admin",), ("admin", "user")])
@pytest.mark.parametrize("path", DASHBOARD_PATHS)
def test_dashboard_is_open_to_every_role(
    client: TestClient, roles: tuple[str, ...], path: str
) -> None:
    assert _as(client, *roles).get(path).status_code == 200


def test_account_without_any_known_role_is_denied(client: TestClient) -> None:
    response = _as(client, "offline_access").get("/")
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Administrative surfaces
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ADMIN_PAGES + ADMIN_PARTIALS + ADMIN_APIS)
def test_user_role_is_denied_admin_surfaces(client: TestClient, path: str) -> None:
    assert _as(client, "user").get(path).status_code == 403


@pytest.mark.parametrize("path", ADMIN_APIS_SELF_CONTAINED)
def test_admin_role_reaches_the_api(client: TestClient, path: str) -> None:
    assert _as(client, "admin").get(path).status_code == 200


@pytest.mark.parametrize("path", ["/addons", "/catalogs", "/maintenance", "/services"])
def test_admin_reaches_the_admin_pages(client: TestClient, path: str) -> None:
    assert _as(client, "admin").get(path).status_code == 200


# ---------------------------------------------------------------------------
# Denial shape: HTML for navigations, JSON for the API
# ---------------------------------------------------------------------------


def test_denied_page_renders_html(client: TestClient) -> None:
    response = _as(client, "user").get("/addons")
    assert response.status_code == 403
    assert response.headers["content-type"].startswith("text/html")
    assert "Access denied" in response.text


def test_denied_api_call_stays_json(client: TestClient) -> None:
    response = _as(client, "user").get("/api/v1/catalogs")
    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["detail"]


# ---------------------------------------------------------------------------
# Tile visibility is enforced in the response, not in CSS
# ---------------------------------------------------------------------------


def test_restricted_tiles_are_absent_from_a_user_response(client: TestClient) -> None:
    user_body = _as(client, "user").get("/partials/tiles").text
    admin_body = _as(client, "admin").get("/partials/tiles").text

    # Seeded defaults: LibreChat is open to all, Keycloak is admin-only.
    assert "LibreChat" in user_body
    assert "Keycloak" not in user_body, "an admin-only tile must not reach a user at all"
    assert "Keycloak" in admin_body
