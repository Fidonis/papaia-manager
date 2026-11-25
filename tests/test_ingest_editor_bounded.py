"""The requests the job editor makes on its own are bounded on the server (MGR-034).

`/ingest/new` asks for the collections of the chosen connection, the preview of the schedule
and, while the person types, the validation of the job. Behind them a Qdrant and an ingester
stand in that accept a request and never answer (an in-process transport that waits for ever).
Each request still answers within its limit (`api_ingest_jobs.EDITOR_LIMIT`, made short here)
plus 2 s, with the unavailable state and a reason that names the limit only, in English and
in German. The schedule preview is computed by the manager itself and asks neither service.
The editor shows the reason of an unavailable collection list (tests/js).
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

_CONFIG_DIR = tempfile.mkdtemp(prefix="papaia-editor-bounded-config-")
_WORKSPACE_DIR = tempfile.mkdtemp(prefix="papaia-editor-bounded-workspace-")

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
from app.core.ingest import runs  # noqa: E402
from app.core.vectordb.ingest_file import CONNECTIONS_RELPATH  # noqa: E402
from app.main import create_app  # noqa: E402
from app.routers import api_ingest_jobs  # noqa: E402
from app.routers.rag_deps import get_http_transport  # noqa: E402

_CSRF = "test-csrf-token-value"
_BASE = "/api/v1/rag/ingest"
# Short, so the tests are quick; the real limit is checked against the browser checks below.
_LIMIT = 1.0
# What the browser checks of the kit give a page step (Playwright's default of 30 s).
_BROWSER_STEP = 30.0
_TOKEN = "editor-bounded-ingest-token"


class Silent(httpx.AsyncBaseTransport):
    """A Qdrant and an ingester that take every request and never answer."""

    def __init__(self) -> None:
        self.hosts: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.hosts.append(request.url.host)
        await asyncio.Event().wait()
        raise AssertionError("unreachable")  # pragma: no cover


def _job(**overrides: Any) -> dict[str, Any]:
    job: dict[str, Any] = {
        "id": "handbook",
        "enabled": True,
        "description": "",
        "source": {"type": "local", "label": "handbook", "path": "/data/local/handbook"},
        "target": {"collection": "kb", "connection": "default"},
        "mode": "upsert",
        "schedule": {"mode": "manual"},
        "embedding": {"model": "nomic-embed-text"},
    }
    job.update(overrides)
    return job


@pytest.fixture
def silent() -> Silent:
    return Silent()


@pytest.fixture
def client(
    tmp_path: Path, silent: Silent, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    config_dir = tmp_path / "config"
    (config_dir / "manager").mkdir(parents=True)
    (config_dir / CONNECTIONS_RELPATH.parent).mkdir(parents=True, exist_ok=True)
    (config_dir / ".env").write_text("COMPOSE_PROFILES=keycloak,rag\n", encoding="utf-8")
    (config_dir / "ai" / "rag" / ".env").write_text(
        f"QI_CONNECTIONS_SECRET=s\nQI_API_TOKEN={_TOKEN}\nQI_TIMEZONE=Europe/Berlin\n",
        encoding="utf-8",
    )
    (config_dir / CONNECTIONS_RELPATH).write_text(
        yaml.safe_dump(
            {"version": 1, "connections": [{"name": "default", "url": "http://qdrant:6333"}]}
        ),
        encoding="utf-8",
    )

    async def idle(_kind: object) -> None:
        return None

    monkeypatch.setattr(runs.runner, "find_runner", idle)
    monkeypatch.setattr(api_ingest_jobs, "EDITOR_LIMIT", _LIMIT)
    get_settings.cache_clear()
    settings = get_settings().model_copy(update={"papaia_config_dir": str(config_dir)})
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_http_transport] = lambda: silent
    session: dict[str, Any] = {
        "user": {
            "sub": "u-1",
            "preferred_username": "Tester",
            "roles": ["admin"],
            "exp": int(time.time()) + 3600,
        },
        "_csrf_token": _CSRF,
    }
    payload = base64.b64encode(json.dumps(session).encode())
    signed = TimestampSigner(settings.manager_session_secret).sign(payload).decode()
    with TestClient(app, follow_redirects=False) as browser:
        browser.cookies.set("papaia_manager_session", signed)
        yield browser
    get_settings.cache_clear()


def _timed(call: Any) -> tuple[httpx.Response, float]:
    started = time.monotonic()
    response = call()
    return response, time.monotonic() - started


REASONS = {
    "en": ("Qdrant did not answer within 1 seconds.", "The ingester did not answer within 1"),
    "de": ("Qdrant hat nicht innerhalb von 1 Sekunden geantwortet.", "Der Ingester hat nicht"),
}


def test_the_limit_stays_below_the_browser_checks() -> None:
    assert 0 < api_ingest_jobs.EDITOR_LIMIT < _BROWSER_STEP - 5


@pytest.mark.parametrize("lang", ["en", "de"])
def test_the_collections_answer_within_the_limit_when_qdrant_never_answers(
    client: TestClient, silent: Silent, lang: str
) -> None:
    response, took = _timed(
        lambda: client.get(
            f"{_BASE}/collections?connection=default", headers={"Accept-Language": lang}
        )
    )
    assert response.status_code == 200, response.text
    assert took < _LIMIT + 2, took
    assert "qdrant" in silent.hosts, "the request reached the stand-in"
    body = response.json()
    assert body["available"] is False
    assert body["collections"] == []
    assert body["connection"] == "default"
    assert body["reason"] == REASONS[lang][0]
    # The limit only: no host, address or exception name.
    assert "qdrant:" not in body["reason"] and "6333" not in body["reason"]


@pytest.mark.parametrize("lang", ["en", "de"])
def test_the_validation_answers_within_the_limit_when_the_ingester_never_answers(
    client: TestClient, silent: Silent, lang: str
) -> None:
    response, took = _timed(
        lambda: client.post(
            f"{_BASE}/jobs/validate",
            json={"job": _job(), "create": True},
            headers={"X-CSRF-Token": _CSRF, "Accept-Language": lang},
        )
    )
    assert response.status_code == 200, response.text
    assert took < _LIMIT + 2, took
    assert "qdrant-ingest" in silent.hosts, "the request reached the stand-in"
    body = response.json()
    # The manager's own checks still ran; the ingester's verdict is missing, and says why.
    assert body["ok"] is True
    assert body["issues"] == []
    assert body["reason"].startswith(REASONS[lang][1])
    assert "qdrant-ingest" not in body["reason"] and "8300" not in body["reason"]


def test_the_validation_keeps_the_managers_own_problems_when_the_ingester_never_answers(
    client: TestClient,
) -> None:
    response, took = _timed(
        lambda: client.post(
            f"{_BASE}/jobs/validate",
            json={"job": _job(id=""), "create": True},
            headers={"X-CSRF-Token": _CSRF},
        )
    )
    assert response.status_code == 200, response.text
    assert took < _LIMIT + 2, took
    body = response.json()
    assert body["ok"] is False
    assert any(issue["field"] == "id" for issue in body["issues"])


def test_a_validation_that_waited_for_nothing_carries_no_reason(client: TestClient) -> None:
    # With problems of its own the manager does not ask the ingester: nothing to wait for.
    response = client.post(
        f"{_BASE}/jobs/validate",
        json={"job": _job(id=""), "create": True},
        headers={"X-CSRF-Token": _CSRF},
    )
    assert "reason" not in response.json()


@pytest.mark.parametrize("lang", ["en", "de"])
def test_the_schedule_preview_answers_at_once_and_asks_no_service(
    client: TestClient, silent: Silent, lang: str
) -> None:
    response, took = _timed(
        lambda: client.post(
            f"{_BASE}/schedule/preview",
            json={"schedule": {"mode": "daily", "time": "02:00"}},
            headers={"X-CSRF-Token": _CSRF, "Accept-Language": lang},
        )
    )
    assert response.status_code == 200, response.text
    assert took < _LIMIT + 2, took
    assert silent.hosts == []
    assert response.json()["ok"] is True
