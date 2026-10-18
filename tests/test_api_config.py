"""The Modules and Overlay tabs of Settings and their API: who may call what, what is refused before
a job is queued, and that the overlay's content is nowhere it should not be.

The jobs are pinned in `test_config_service.py`, the rules in `test_config_modules.py` and
`test_config_overlay.py`. This is the HTTP surface around them: the role that gates it, the status
codes, that nothing is changed without the CSRF token, and that a stale page is a 409.
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

for _key, _value in {
    "OIDC_ISSUER_KC_AUTH": "https://kc.test/auth",
    "OIDC_ISSUER_KC_TOKEN": "https://kc.test/token",
    "OIDC_ISSUER_KC_CERTS": "https://kc.test/certs",
    "MANAGER_ADMIN_ROLE": "admin",
    "MANAGER_USER_ROLE": "user",
    "MANAGER_HOST": "http://localhost:8120",
    "MANAGER_OIDC_CLIENT_SECRET": "client-secret",
    "MANAGER_SESSION_SECRET": "test-session-secret-value",
    "PAPAIA_CONFIG_DIR": tempfile.mkdtemp(prefix="papaia-config-api-config-"),
    "PAPAIA_WORKSPACE_DIR": tempfile.mkdtemp(prefix="papaia-config-api-workspace-"),
}.items():
    os.environ.setdefault(_key, _value)

from fastapi.testclient import TestClient  # noqa: E402
from itsdangerous import TimestampSigner  # noqa: E402

import app.main as app_main  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.core import runner  # noqa: E402
from app.core.config import history, modules, overlay, preview, service  # noqa: E402
from app.core.jobs import JobQueue  # noqa: E402
from app.main import create_app  # noqa: E402

_CSRF = "test-csrf-token-value"
_CSRF_HEADER = {"X-CSRF-Token": _CSRF}
_BASE = "/api/v1/config"
_SECRET = "sk-very-secret-api-key"
_DRAFT = f"endpoints:\n  custom:\n    - apiKey: {_SECRET}\n"

_COMPOSE = """\
services:
  localai:
    profiles: [localai]
    labels:
      de.fidonis.module: papaia-localai
  localai-init:
    profiles: [localai]
    labels:
      de.fidonis.module: papaia-localai
  librechat:
    profiles: [librechat]
    labels:
      de.fidonis.module: papaia-librechat
  searxng:
    profiles: [librechat-websearch]
    labels:
      de.fidonis.module: papaia-searxng
  qdrant:
    profiles: [rag]
    labels:
      de.fidonis.module: papaia-qdrant
  smtp-relay:
    profiles: [smtp]
    labels:
      de.fidonis.module: papaia-smtp-relay
"""


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace" / "papaia"
    (root / "src").mkdir(parents=True)
    (root / "src" / "docker-compose.yml").write_text(
        "include:\n  - svc/docker-compose.yml\n", encoding="utf-8"
    )
    (root / "src" / "svc").mkdir()
    (root / "src" / "svc" / "docker-compose.yml").write_text(_COMPOSE, encoding="utf-8")
    (root / "tools" / "lib").mkdir(parents=True)
    (root / "tools" / "lib" / "cli.py").write_text("'render-preview'\n", encoding="utf-8")
    (root / "tools" / "lib" / "modules.py").write_text("", encoding="utf-8")
    return tmp_path / "workspace"


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "config"
    directory.mkdir()
    (directory / ".env").write_text("COMPOSE_PROFILES=nginx,librechat\n", encoding="utf-8")
    return directory


@pytest.fixture
def queue(config_dir: Path) -> Iterator[JobQueue]:
    # Not started: a job is enqueued and stays there, which is all these tests look at.
    app_main._job_queue = JobQueue(config_dir=str(config_dir))  # noqa: SLF001
    yield app_main._job_queue  # noqa: SLF001
    app_main._job_queue = None  # noqa: SLF001


class _Runners:
    def __init__(self) -> None:
        self.running: set[str] = set()


@pytest.fixture(autouse=True)
def runners(monkeypatch: pytest.MonkeyPatch) -> _Runners:
    state = _Runners()

    kinds = {
        "restore": runner.RESTORE_KIND,
        "upgrade": runner.UPGRADE_KIND,
        "stack": runner.STACK_KIND,
    }

    async def find_runner(kind: Any = None) -> Any:
        if any(name in state.running and kind is kinds[name] for name in kinds):
            return type("R", (), {"is_running": True})()
        return None

    monkeypatch.setattr(runner, "find_runner", find_runner)
    return state


def _make_client(config_dir: Path, workspace: Path) -> TestClient:
    get_settings.cache_clear()
    settings = get_settings().model_copy(
        update={"papaia_config_dir": str(config_dir), "papaia_workspace_dir": str(workspace)}
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def client(config_dir: Path, workspace: Path, queue: JobQueue) -> Iterator[TestClient]:
    yield _make_client(config_dir, workspace)
    get_settings.cache_clear()


def _as(client: TestClient, *roles: str) -> TestClient:
    session: dict[str, Any] = {
        "user": {
            "sub": "11111111-1111-1111-1111-111111111111",
            "preferred_username": "admin",
            "roles": list(roles),
            "exp": int(time.time()) + 3600,
        },
        "_csrf_token": _CSRF,
    }
    payload = base64.b64encode(json.dumps(session).encode())
    signed = TimestampSigner(get_settings().manager_session_secret).sign(payload).decode()
    client.cookies.clear()
    client.cookies.set("papaia_manager_session", signed)
    return client


def _admin(client: TestClient) -> TestClient:
    return _as(client, "admin")


def _modules_revision(client: TestClient) -> str:
    return str(_admin(client).get(f"{_BASE}/modules").json()["revision"])


def _file_revision(client: TestClient, key: str = "librechat") -> str:
    return str(_admin(client).get(f"{_BASE}/overlay/{key}").json()["revision"])


def _seed(config_dir: Path, data: bytes = b"a: 1\n", key: str = "librechat") -> None:
    assert overlay.write(str(config_dir), key, data, expected_revision="") is not None


# -- who may call it -----------------------------------------------------------------

_READS = [
    f"{_BASE}/modules",
    f"{_BASE}/modules/localai/info",
    f"{_BASE}/overlay",
    f"{_BASE}/overlay/librechat",
    f"{_BASE}/overlay/librechat/versions",
    f"{_BASE}/overlay/librechat/versions/20261018T142530Z-apply",
    "/settings/modules",
    "/settings/overlay",
    "/partials/config/job-status",
]
_WRITES = [
    ("post", f"{_BASE}/modules/localai/enable", {"revision": "x", "variant": "cpu"}),
    ("post", f"{_BASE}/modules/localai/disable", {"revision": "x", "scope": "keep"}),
    ("post", f"{_BASE}/overlay/librechat/check", {"content": ""}),
    ("post", f"{_BASE}/overlay/librechat/preview", {"content": ""}),
    ("put", f"{_BASE}/overlay/librechat", {"content": "", "revision": "x"}),
    ("delete", f"{_BASE}/overlay/librechat?revision=x", None),
    ("post", f"{_BASE}/overlay/librechat/rollback", {"version": "v", "revision": "x"}),
]


@pytest.mark.parametrize("roles", [("user",), ("papaia-admin",), ()])
@pytest.mark.parametrize("path", _READS)
def test_only_an_administrator_may_read(
    client: TestClient, roles: tuple[str, ...], path: str
) -> None:
    assert _as(client, *roles).get(path).status_code == 403


@pytest.mark.parametrize("roles", [("user",), ("papaia-admin",)])
@pytest.mark.parametrize(("method", "path", "body"), _WRITES)
def test_only_an_administrator_may_change_or_try(
    client: TestClient, roles: tuple[str, ...], method: str, path: str, body: Any
) -> None:
    response = _as(client, *roles).request(method, path, json=body, headers=_CSRF_HEADER)
    assert response.status_code == 403


@pytest.mark.parametrize("path", _READS)
def test_anonymous_requests_are_turned_away(client: TestClient, path: str) -> None:
    client.cookies.clear()
    api_or_partial = path.startswith(("/api/", "/partials/"))
    response = client.get(path, headers={"HX-Request": "true"} if "partials" in path else {})
    assert response.status_code == (401 if api_or_partial else 307)


@pytest.mark.parametrize(("method", "path", "body"), _WRITES)
def test_anonymous_writes_are_turned_away(
    client: TestClient, method: str, path: str, body: Any
) -> None:
    client.cookies.clear()
    assert client.request(method, path, json=body).status_code == 401


@pytest.mark.parametrize(("method", "path", "body"), _WRITES)
def test_nothing_is_done_without_the_csrf_token(
    client: TestClient, queue: JobQueue, config_dir: Path, method: str, path: str, body: Any
) -> None:
    _seed(config_dir)
    before = overlay.read(str(config_dir), "librechat").raw
    response = _admin(client).request(method, path, json=body)
    assert response.status_code == 403
    assert queue.list_jobs() == []
    assert overlay.read(str(config_dir), "librechat").raw == before


def test_every_route_that_changes_or_tries_something_is_one_this_module_covers(
    client: TestClient,
) -> None:
    """A new mutating route has to be added to `_WRITES` -- and so to the tests above."""
    paths: dict[str, dict[str, Any]] = client.app.openapi()["paths"]  # type: ignore[attr-defined]
    changing = {
        (method.upper(), path.replace("{name}", "localai").replace("{key}", "librechat"))
        for path, operations in paths.items()
        if path.startswith(_BASE)
        for method in operations
        if method.upper() in {"POST", "PUT", "PATCH", "DELETE"}
    }
    covered = {(m.upper(), p.split("?")[0]) for m, p, _ in _WRITES}
    assert changing == covered


# -- modules -------------------------------------------------------------------------------


def test_the_modules_document_has_the_state_and_the_variants(client: TestClient) -> None:
    body = _admin(client).get(f"{_BASE}/modules").json()
    assert {m["name"]: m["enabled"] for m in body["modules"]}["localai"] is False
    assert [v["id"] for v in body["variants"]][0] == "cpu"
    assert "vulkan" not in [v["id"] for v in body["variants"]]
    assert body["host_only_variants"] == ["vulkan"]
    localai = next(m for m in body["modules"] if m["name"] == "localai")
    assert (localai["services"], localai["modules"]) == (2, 1)


def test_switching_localai_on_queues_a_job(client: TestClient, queue: JobQueue) -> None:
    response = _admin(client).post(
        f"{_BASE}/modules/localai/enable",
        json={"revision": _modules_revision(client), "variant": "nvidia-cuda-12"},
        headers=_CSRF_HEADER,
    )
    assert response.status_code == 202
    [job] = queue.list_jobs()
    assert (job.action, job.target) == (service.ENABLE_ACTION, "module:localai")
    assert job.params == {"module": "localai", "variant": "nvidia-cuda-12", "public_url": None}


def test_switching_web_search_off_queues_a_job(
    client: TestClient, queue: JobQueue, config_dir: Path
) -> None:
    (config_dir / ".env").write_text(
        "COMPOSE_PROFILES=nginx,librechat-websearch\n", encoding="utf-8"
    )
    response = _admin(client).post(
        f"{_BASE}/modules/web-search/disable",
        json={"revision": _modules_revision(client)},
        headers=_CSRF_HEADER,
    )
    assert response.status_code == 202
    assert [j.action for j in queue.list_jobs()] == [service.DISABLE_ACTION]


@pytest.mark.parametrize(
    ("name", "body", "code", "message"),
    [
        ("localai", {"variant": None}, 422, "Choose"),
        ("localai", {"variant": "vulkan"}, 422, "set on the host"),
        ("localai", {"variant": "bogus"}, 422, "Unknown"),
        ("web-search", {"variant": "cpu"}, 422, "Only LocalAI"),
        ("manager", {}, 404, "can't be switched"),
        ("nope", {}, 404, "can't be switched"),
    ],
)
def test_a_request_the_page_would_never_make_is_refused_before_a_job(
    client: TestClient, queue: JobQueue, name: str, body: dict[str, Any], code: int, message: str
) -> None:
    response = _admin(client).post(
        f"{_BASE}/modules/{name}/enable",
        json={"revision": _modules_revision(client), **body},
        headers=_CSRF_HEADER,
    )
    assert response.status_code == code
    assert message in response.json()["detail"]
    assert queue.list_jobs() == []


def test_a_module_page_that_is_stale_is_a_409(client: TestClient, queue: JobQueue) -> None:
    response = _admin(client).post(
        f"{_BASE}/modules/web-search/enable", json={"revision": "old"}, headers=_CSRF_HEADER
    )
    assert response.status_code == 409
    assert "changed" in response.json()["detail"]
    assert queue.list_jobs() == []


def test_a_module_that_is_already_on_cannot_be_switched_on(
    client: TestClient, config_dir: Path
) -> None:
    (config_dir / ".env").write_text("COMPOSE_PROFILES=nginx,localai\n", encoding="utf-8")
    response = _admin(client).post(
        f"{_BASE}/modules/localai/enable",
        json={"revision": _modules_revision(client), "variant": "cpu"},
        headers=_CSRF_HEADER,
    )
    assert response.status_code == 409
    assert "already" in response.json()["detail"]


def _on(config_dir: Path, *profiles: str) -> None:
    (config_dir / ".env").write_text(
        "COMPOSE_PROFILES=" + ",".join(["nginx", "librechat", *profiles]) + "\n", encoding="utf-8"
    )


def _mail_server(config_dir: Path) -> None:
    module_env = config_dir / "infra" / "smtp-relay" / ".env"
    module_env.parent.mkdir(parents=True)
    module_env.write_text(
        "SMTP_RELAY_UPSTREAM_HOST=smtp.example.org\nSMTP_RELAY_FROM_ADDRESS=a@example.org\n",
        encoding="utf-8",
    )


def test_the_modules_document_marks_what_can_be_deleted(client: TestClient) -> None:
    rows = {m["name"]: m for m in _admin(client).get(f"{_BASE}/modules").json()["modules"]}
    for name in ("localai", "web-search", "rag", "mail"):
        assert rows[name]["switchable"] is True
        assert rows[name]["can_delete"] is True
    assert rows["manager"]["switchable"] is False
    assert rows["mail"]["configured"] is False


def test_switching_the_rag_system_on_queues_a_job(client: TestClient, queue: JobQueue) -> None:
    response = _admin(client).post(
        f"{_BASE}/modules/rag/enable",
        json={"revision": _modules_revision(client)},
        headers=_CSRF_HEADER,
    )
    assert response.status_code == 202
    [job] = queue.list_jobs()
    assert (job.action, job.target) == (service.ENABLE_ACTION, "module:rag")


def test_an_addon_with_the_same_services_refuses_the_rag_system_before_a_job(
    client: TestClient, queue: JobQueue, config_dir: Path
) -> None:
    (config_dir / "deployment.yaml").write_text(
        "addons:\n  - name: qdrant-ingest\n    active: true\n", encoding="utf-8"
    )
    response = _admin(client).post(
        f"{_BASE}/modules/rag/enable",
        json={"revision": _modules_revision(client)},
        headers=_CSRF_HEADER,
    )
    assert response.status_code == 422
    assert "qdrant-ingest" in response.json()["detail"]
    assert queue.list_jobs() == []


def test_the_relay_without_a_mail_server_is_refused_before_a_job(
    client: TestClient, queue: JobQueue
) -> None:
    response = _admin(client).post(
        f"{_BASE}/modules/mail/enable",
        json={"revision": _modules_revision(client)},
        headers=_CSRF_HEADER,
    )
    assert response.status_code == 422
    assert "No mail server" in response.json()["detail"]
    assert queue.list_jobs() == []


def test_the_relay_with_a_mail_server_is_switched_on_by_a_job(
    client: TestClient, queue: JobQueue, config_dir: Path
) -> None:
    _mail_server(config_dir)
    response = _admin(client).post(
        f"{_BASE}/modules/mail/enable",
        json={"revision": _modules_revision(client)},
        headers=_CSRF_HEADER,
    )
    assert response.status_code == 202
    assert [j.target for j in queue.list_jobs()] == ["module:mail"]


def test_a_core_that_cannot_manage_modules_cannot_switch_rag_or_mail(
    client: TestClient, queue: JobQueue, workspace: Path, config_dir: Path
) -> None:
    _mail_server(config_dir)
    (workspace / "papaia" / "tools" / "lib" / "modules.py").unlink()
    for name in ("rag", "mail"):
        response = _admin(client).post(
            f"{_BASE}/modules/{name}/enable",
            json={"revision": _modules_revision(client)},
            headers=_CSRF_HEADER,
        )
        assert response.status_code == 422
        assert "too old" in response.json()["detail"]
    assert queue.list_jobs() == []


# -- modules: the answers to what setup asks ----------------------------------------------


def _enable(client: TestClient, name: str, **body: Any) -> Any:
    return _admin(client).post(
        f"{_BASE}/modules/{name}/enable",
        json={"revision": _modules_revision(client), **body},
        headers=_CSRF_HEADER,
    )


def test_the_public_url_is_part_of_the_job(client: TestClient, queue: JobQueue) -> None:
    response = _enable(client, "rag", public_url=" https://q.example.org:6333/ ")
    assert response.status_code == 202
    [job] = queue.list_jobs()
    assert job.params == {"module": "rag", "variant": None, "public_url": "https://q.example.org:6333"}


@pytest.mark.parametrize("url", ["q.example.org", "ftp://q.example.org", "https://u:p@q.example.org"])
def test_a_url_that_is_wrong_is_a_422_before_a_job(
    client: TestClient, queue: JobQueue, url: str
) -> None:
    response = _enable(client, "rag", public_url=url)
    assert response.status_code == 422
    assert "public URL is not valid" in response.json()["detail"]
    assert queue.list_jobs() == []


@pytest.mark.parametrize("name", ["web-search", "mail"])
def test_a_module_without_a_url_question_refuses_one(
    client: TestClient, queue: JobQueue, config_dir: Path, name: str
) -> None:
    _mail_server(config_dir)
    response = _enable(client, name, public_url="https://x.example.org")
    assert response.status_code == 422
    assert "takes no public URL" in response.json()["detail"]
    assert queue.list_jobs() == []


_MAIL_BODY = {
    "host": "smtp.example.org",
    "port": 587,
    "security": "starttls",
    "user": "mailer",
    "password": "a-long-password-value",
    "from_address": "noreply@example.org",
    "from_name": "Our Mail",
}


def test_the_relay_is_switched_on_with_the_mail_server_the_dialog_brought(
    client: TestClient, queue: JobQueue
) -> None:
    response = _enable(client, "mail", mail=_MAIL_BODY)
    assert response.status_code == 202
    [job] = queue.list_jobs()
    assert job.target == "module:mail"
    assert "a-long-password-value" not in json.dumps(job.params)
    assert "a-long-password-value" not in response.text


def test_a_mail_server_is_checked_like_on_the_mail_page(
    client: TestClient, queue: JobQueue
) -> None:
    for change, message in (
        ({"host": "smtp.example.org:587"}, "not a valid mail server host name"),
        ({"from_address": "nobody"}, "not a valid sender address"),
        ({"password": None}, "login needs its password"),
        ({"security": "none"}, "sent unencrypted"),
        ({"security": "ssl"}, "not a valid connection security"),
    ):
        response = _enable(client, "mail", mail={**_MAIL_BODY, **change})
        assert response.status_code == 422, change
        assert message in response.json()["detail"]
        assert "a-long-password-value" not in response.text
    assert queue.list_jobs() == []


def test_a_mail_server_for_another_module_is_refused(
    client: TestClient, queue: JobQueue
) -> None:
    response = _enable(client, "rag", mail=_MAIL_BODY)
    assert response.status_code == 422
    assert "Only the mail relay" in response.json()["detail"]
    assert queue.list_jobs() == []


def test_a_mail_body_that_cannot_be_read_is_refused_without_repeating_the_password(
    client: TestClient,
) -> None:
    response = _enable(client, "mail", mail={"password": "a-long-password-value"})
    assert response.status_code == 422
    assert "a-long-password-value" not in response.text


def test_the_mail_row_carries_what_the_dialog_starts_from(
    client: TestClient, config_dir: Path
) -> None:
    rows = {m["name"]: m for m in _admin(client).get(f"{_BASE}/modules").json()["modules"]}
    defaults = rows["mail"]["mail_defaults"]
    assert (defaults["host"], defaults["security"], defaults["port"]) == ("", "starttls", 587)
    assert rows["rag"]["mail_defaults"] is None
    html = _admin(client).get("/settings/modules").text
    # The questions open under the row, not in a dialog (it reads the same on a phone).
    assert 'id="module-on-dialog"' not in html
    for name in ("localai", "rag", "mail"):
        assert f'id="panel-{name}"' in html
    assert 'id="panel-web-search"' not in html
    assert 'id="om-host"' in html
    assert "The relay forwards every mail to this server" in html


# -- modules: what a switch-off may delete -------------------------------------------------


def _off(client: TestClient, name: str, **body: Any) -> Any:
    return _admin(client).post(
        f"{_BASE}/modules/{name}/disable",
        json={"revision": _modules_revision(client), **body},
        headers=_CSRF_HEADER,
    )


def test_switching_off_without_a_scope_keeps_the_data_and_the_settings(
    client: TestClient, queue: JobQueue, config_dir: Path
) -> None:
    _on(config_dir, "rag")
    assert _off(client, "rag").status_code == 202
    [job] = queue.list_jobs()
    assert job.params == {"module": "rag", "scope": "keep"}


@pytest.mark.parametrize("scope", ["data", "all"])
def test_a_deletion_is_confirmed_with_the_modules_name(
    client: TestClient, queue: JobQueue, config_dir: Path, scope: str
) -> None:
    _on(config_dir, "rag")
    for confirm in (None, "", "mail", "RAG"):
        body: dict[str, Any] = {"scope": scope}
        if confirm is not None:
            body["confirm"] = confirm
        response = _off(client, "rag", **body)
        assert response.status_code == 422
        assert "Type the module name (rag)" in response.json()["detail"]
    assert queue.list_jobs() == []
    assert _off(client, "rag", scope=scope, confirm="rag").status_code == 202
    [job] = queue.list_jobs()
    assert job.params["scope"] == scope


@pytest.mark.parametrize("scope", ["config", "settings", "both", ""])
def test_there_is_no_settings_only_state(
    client: TestClient, queue: JobQueue, config_dir: Path, scope: str
) -> None:
    _on(config_dir, "rag")
    response = _off(client, "rag", scope=scope, confirm="rag")
    assert response.status_code == 422
    assert queue.list_jobs() == []


def test_a_core_that_cannot_delete_refuses_a_deletion_but_not_a_plain_switch_off(
    client: TestClient, queue: JobQueue, workspace: Path, config_dir: Path
) -> None:
    _on(config_dir, "localai")
    (workspace / "papaia" / "tools" / "lib" / "modules.py").unlink()
    response = _off(client, "localai", scope="data", confirm="localai")
    assert response.status_code == 422
    assert "can't delete" in response.json()["detail"]
    assert _off(client, "localai").status_code == 202


def test_the_info_is_what_the_core_reports(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []

    async def fake(name: str, **kwargs: Any) -> tuple[int, str, str]:
        asked.append(name)
        return 0, '{"module": "rag", "volumes": [], "settings": {"dirs": []}}', ""

    monkeypatch.setattr(modules, "run_module_info", fake)
    body = _admin(client).get(f"{_BASE}/modules/rag/info").json()
    assert body["available"] is True
    assert body["module"] == "rag"
    assert asked == ["rag"]


@pytest.mark.parametrize("name", ["manager", "nope", "keycloak"])
def test_the_info_of_a_module_that_cannot_be_switched_is_a_404(
    client: TestClient, name: str
) -> None:
    assert _admin(client).get(f"{_BASE}/modules/{name}/info").status_code == 404


def test_a_module_that_is_off_cannot_be_switched_off(client: TestClient) -> None:
    response = _admin(client).post(
        f"{_BASE}/modules/localai/disable",
        json={"revision": _modules_revision(client)},
        headers=_CSRF_HEADER,
    )
    assert response.status_code == 409
    assert "not switched on" in response.json()["detail"]


@pytest.mark.parametrize("kind", ["restore", "upgrade", "stack"])
def test_a_running_restore_upgrade_or_stack_action_refuses_every_change(
    client: TestClient, queue: JobQueue, runners: _Runners, kind: str
) -> None:
    runners.running.add(kind)
    revision = _modules_revision(client)
    response = _admin(client).post(
        f"{_BASE}/modules/web-search/enable", json={"revision": revision}, headers=_CSRF_HEADER
    )
    assert response.status_code == 409
    assert "still running" in response.json()["detail"]
    assert queue.list_jobs() == []


def test_a_body_that_cannot_be_read_is_refused_without_repeating_it(client: TestClient) -> None:
    response = _admin(client).post(
        f"{_BASE}/overlay/librechat/check",
        content=b"{not json " + _SECRET.encode(),
        headers={**_CSRF_HEADER, "Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert _SECRET not in response.text
    response = _admin(client).put(
        f"{_BASE}/overlay/librechat", json={"revision": "x"}, headers=_CSRF_HEADER
    )
    assert response.status_code == 422


# -- overlay: reading ----------------------------------------------------------------------


def test_the_overlay_list_has_the_three_targets_and_the_other_files(
    client: TestClient, config_dir: Path
) -> None:
    _seed(config_dir)
    stray = config_dir / "overlay" / "ai" / "typo.yaml"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text("x: 1\n", encoding="utf-8")
    body = _admin(client).get(f"{_BASE}/overlay").json()
    assert body["preview_available"] is True
    targets = {t["key"]: t for t in body["targets"]}
    assert set(targets) == {"librechat", "litellm", "searxng"}
    assert targets["librechat"]["exists"] is True and targets["librechat"]["lines"] == 1
    assert targets["litellm"]["exists"] is False
    assert targets["librechat"]["group_active"] is True
    assert targets["searxng"]["group_active"] is False
    assert targets["librechat"]["services"] == 1
    assert body["other"] == [{"path": "ai/typo.yaml", "note": "ignored by the render"}]


def test_one_overlay_has_its_content_revision_and_versions(
    client: TestClient, config_dir: Path
) -> None:
    _seed(config_dir, _DRAFT.encode())
    history.save(str(config_dir), "librechat", b"old: 1\n", "apply")
    body = _admin(client).get(f"{_BASE}/overlay/librechat").json()
    assert body["content"] == _DRAFT
    assert body["revision"] == overlay.revision_of(_DRAFT.encode())
    assert [v["event"] for v in body["versions"]] == ["apply"]


def test_a_missing_overlay_is_an_empty_one(client: TestClient) -> None:
    body = _admin(client).get(f"{_BASE}/overlay/litellm").json()
    assert (body["exists"], body["content"], body["revision"]) == (False, "", "")


def test_an_unknown_overlay_is_a_404(client: TestClient) -> None:
    assert _admin(client).get(f"{_BASE}/overlay/keycloak").status_code == 404
    assert _admin(client).get(f"{_BASE}/overlay/..%2Fetc").status_code == 404


def test_a_file_that_is_not_utf8_is_not_opened(client: TestClient, config_dir: Path) -> None:
    _seed(config_dir, b"a: \xff\xfe\n")
    response = _admin(client).get(f"{_BASE}/overlay/librechat")
    assert response.status_code == 409
    assert "UTF-8" in response.json()["detail"]


# -- overlay: check and preview ----------------------------------------------------------------


def test_the_check_says_what_is_wrong_and_where(client: TestClient) -> None:
    body = (
        _admin(client)
        .post(
            f"{_BASE}/overlay/librechat/check",
            json={"content": "a: 1\nb: [\n"},
            headers=_CSRF_HEADER,
        )
        .json()
    )
    assert body["ok"] is False
    assert body["problems"][0]["line"] >= 2


def test_the_check_of_a_good_draft_has_a_summary(client: TestClient) -> None:
    body = (
        _admin(client)
        .post(f"{_BASE}/overlay/librechat/check", json={"content": "a: 1\n"}, headers=_CSRF_HEADER)
        .json()
    )
    assert body == {"ok": True, "problems": [], "summary": "top level is a mapping with 1 key"}


def _fake_preview(
    monkeypatch: pytest.MonkeyPatch, result: preview.Preview | Exception
) -> list[Any]:
    calls: list[Any] = []

    async def run(**kwargs: Any) -> preview.Preview:
        calls.append(kwargs)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(preview, "run", run)
    return calls


def test_the_preview_shows_the_difference(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_preview(
        monkeypatch,
        preview.parse_answer(
            json.dumps(
                {
                    "ok": True,
                    "current": "a: 1\n",
                    "preview": "a: 1\nb: 2\n",
                    "changed": True,
                }
            )
        ),
    )
    body = (
        _admin(client)
        .post(
            f"{_BASE}/overlay/librechat/preview", json={"content": "b: 2\n"}, headers=_CSRF_HEADER
        )
        .json()
    )
    assert (body["ok"], body["available"], body["added"], body["removed"]) == (True, True, 1, 0)
    assert calls[0]["target_path"] == "ai/librechat/librechat.yaml"
    assert calls[0]["draft"] == "b: 2\n"


def test_a_draft_with_a_syntax_error_is_not_sent_to_the_core(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_preview(monkeypatch, preview.Preview(ok=True))
    body = (
        _admin(client)
        .post(
            f"{_BASE}/overlay/librechat/preview", json={"content": "b: [\n"}, headers=_CSRF_HEADER
        )
        .json()
    )
    assert body["ok"] is False and body["problems"]
    assert calls == []


def test_a_core_that_cannot_preview_is_said_so(
    client: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (workspace / "papaia" / "tools" / "lib" / "cli.py").write_text("# old\n", encoding="utf-8")
    body = (
        _admin(client)
        .post(
            f"{_BASE}/overlay/librechat/preview", json={"content": "a: 1\n"}, headers=_CSRF_HEADER
        )
        .json()
    )
    assert (body["ok"], body["available"]) == (False, False)
    assert "can't preview" in body["error"]


def test_a_draft_the_core_cannot_merge_comes_back_with_its_position(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_preview(monkeypatch, preview.Preview(ok=False, error="bad merge", line=3, column=1))
    body = (
        _admin(client)
        .post(
            f"{_BASE}/overlay/librechat/preview", json={"content": "a: 1\n"}, headers=_CSRF_HEADER
        )
        .json()
    )
    assert body["problems"] == [{"message": "bad merge", "line": 3, "column": 1}]


def test_a_core_that_fails_is_an_error_message_not_a_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_preview(monkeypatch, preview.PreviewError("The core didn't answer in time."))
    response = _admin(client).post(
        f"{_BASE}/overlay/librechat/preview", json={"content": "a: 1\n"}, headers=_CSRF_HEADER
    )
    assert response.status_code == 200
    assert response.json()["error"] == "The core didn't answer in time."


# -- overlay: apply, discard, roll back ---------------------------------------------------------


def test_apply_queues_a_job_and_writes_nothing_itself(
    client: TestClient, queue: JobQueue, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_preview(monkeypatch, preview.Preview(ok=True))
    _seed(config_dir)
    revision = _file_revision(client)
    response = _admin(client).put(
        f"{_BASE}/overlay/librechat",
        json={"content": _DRAFT, "revision": revision},
        headers=_CSRF_HEADER,
    )
    assert response.status_code == 202
    [job] = queue.list_jobs()
    assert (job.action, job.target) == (service.APPLY_ACTION, "overlay:librechat")
    # The file is the job's to write; the request only checked and queued.
    assert overlay.read(str(config_dir), "librechat").raw == b"a: 1\n"
    assert _SECRET not in json.dumps(job.params)


def test_apply_from_a_stale_page_is_a_409_with_a_clear_message(
    client: TestClient, queue: JobQueue, config_dir: Path
) -> None:
    _seed(config_dir)
    stale = _file_revision(client)
    overlay.path_of(str(config_dir), "librechat").write_bytes(b"host: edit\n")
    response = _admin(client).put(
        f"{_BASE}/overlay/librechat",
        json={"content": _DRAFT, "revision": stale},
        headers=_CSRF_HEADER,
    )
    assert response.status_code == 409
    assert "changed after this page loaded it" in response.json()["detail"]
    assert queue.list_jobs() == []
    assert overlay.read(str(config_dir), "librechat").raw == b"host: edit\n"


def test_apply_creating_a_file_that_appeared_meanwhile_is_a_409(
    client: TestClient, config_dir: Path
) -> None:
    revision = _file_revision(client)  # "": no file yet
    _seed(config_dir)
    response = _admin(client).put(
        f"{_BASE}/overlay/librechat",
        json={"content": "a: 2\n", "revision": revision},
        headers=_CSRF_HEADER,
    )
    assert response.status_code == 409


def test_a_draft_that_does_not_parse_is_refused_with_the_position(
    client: TestClient, queue: JobQueue
) -> None:
    response = _admin(client).put(
        f"{_BASE}/overlay/librechat",
        json={"content": "a: [\n", "revision": ""} | {"content": "a: [\n"},
        headers=_CSRF_HEADER,
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["message"].startswith("The overlay can't be saved")
    assert detail["problems"][0]["line"] >= 1
    assert queue.list_jobs() == []


def test_a_draft_the_core_cannot_merge_is_refused_before_a_job(
    client: TestClient, queue: JobQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_preview(monkeypatch, preview.Preview(ok=False, error="cannot merge", line=2, column=1))
    response = _admin(client).put(
        f"{_BASE}/overlay/librechat",
        json={"content": "a: 1\n", "revision": ""},
        headers=_CSRF_HEADER,
    )
    assert response.status_code == 422
    assert "The core can't merge" in response.json()["detail"]["message"]
    assert queue.list_jobs() == []


def test_a_core_that_cannot_be_asked_is_not_a_reason_to_refuse(
    client: TestClient, queue: JobQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_preview(monkeypatch, preview.PreviewError("timed out"))
    response = _admin(client).put(
        f"{_BASE}/overlay/librechat",
        json={"content": "a: 1\n", "revision": ""},
        headers=_CSRF_HEADER,
    )
    assert response.status_code == 202
    assert len(queue.list_jobs()) == 1


def test_discard_queues_a_job_for_an_existing_file(
    client: TestClient, queue: JobQueue, config_dir: Path
) -> None:
    _seed(config_dir)
    response = _admin(client).delete(
        f"{_BASE}/overlay/librechat?revision={_file_revision(client)}", headers=_CSRF_HEADER
    )
    assert response.status_code == 202
    assert [j.action for j in queue.list_jobs()] == [service.DISCARD_ACTION]
    assert overlay.read(str(config_dir), "librechat").exists


def test_there_is_nothing_to_discard_when_there_is_no_file(client: TestClient) -> None:
    response = _admin(client).delete(f"{_BASE}/overlay/librechat?revision=", headers=_CSRF_HEADER)
    assert response.status_code == 409
    assert "no overlay file" in response.json()["detail"]


def test_discard_from_a_stale_page_is_a_409(client: TestClient, config_dir: Path) -> None:
    _seed(config_dir)
    response = _admin(client).delete(
        f"{_BASE}/overlay/librechat?revision=old", headers=_CSRF_HEADER
    )
    assert response.status_code == 409


def test_a_running_stack_action_refuses_an_overlay_change_too(
    client: TestClient, queue: JobQueue, runners: _Runners, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_preview(monkeypatch, preview.Preview(ok=True))
    runners.running.add("restore")
    response = _admin(client).put(
        f"{_BASE}/overlay/librechat",
        json={"content": "a: 1\n", "revision": ""},
        headers=_CSRF_HEADER,
    )
    assert response.status_code == 409
    assert queue.list_jobs() == []


# -- versions ----------------------------------------------------------------------------------


def test_a_saved_version_comes_with_its_difference_to_the_file_now(
    client: TestClient, config_dir: Path
) -> None:
    _seed(config_dir, b"a: 2\n")
    version = history.save(str(config_dir), "librechat", b"a: 1\n", "apply")
    body = _admin(client).get(f"{_BASE}/overlay/librechat/versions/{version}").json()
    assert body["content"] == "a: 1\n"
    kinds = {(d["kind"], d["text"]) for d in body["diff"]}
    assert ("del", "a: 2") in kinds and ("add", "a: 1") in kinds


def test_a_version_that_is_not_there_or_not_an_id_is_a_404(client: TestClient) -> None:
    base = f"{_BASE}/overlay/librechat/versions"
    assert _admin(client).get(f"{base}/20261018T142530Z-apply").status_code == 404
    assert _admin(client).get(f"{base}/..%2F..%2Fx").status_code == 404
    assert _admin(client).get(f"{base}/evil").status_code == 404


def test_rolling_back_queues_a_job_with_the_old_content(
    client: TestClient, queue: JobQueue, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_preview(monkeypatch, preview.Preview(ok=True))
    _seed(config_dir, b"a: 2\n")
    version = history.save(str(config_dir), "librechat", b"a: 1\n", "apply")
    response = _admin(client).post(
        f"{_BASE}/overlay/librechat/rollback",
        json={"version": version, "revision": _file_revision(client)},
        headers=_CSRF_HEADER,
    )
    assert response.status_code == 202
    assert [j.action for j in queue.list_jobs()] == [service.ROLLBACK_ACTION]


def test_rolling_back_to_a_version_that_does_not_exist_is_a_404(
    client: TestClient, queue: JobQueue
) -> None:
    response = _admin(client).post(
        f"{_BASE}/overlay/librechat/rollback",
        json={"version": "20261018T142530Z-apply", "revision": ""},
        headers=_CSRF_HEADER,
    )
    assert response.status_code == 404
    assert queue.list_jobs() == []


def test_rolling_back_from_a_stale_page_is_a_409(client: TestClient, config_dir: Path) -> None:
    _seed(config_dir)
    version = history.save(str(config_dir), "librechat", b"a: 0\n", "apply")
    response = _admin(client).post(
        f"{_BASE}/overlay/librechat/rollback",
        json={"version": version, "revision": "old"},
        headers=_CSRF_HEADER,
    )
    assert response.status_code == 409


# -- the pages ---------------------------------------------------------------------------------


def test_settings_has_three_tabs_and_the_menu_has_no_new_entry(client: TestClient) -> None:
    for path, active in (
        ("/settings", "Manager"),
        ("/settings/modules", "Modules"),
        ("/settings/overlay", "Overlay"),
    ):
        html = _admin(client).get(path).text
        assert 'aria-label="Settings"' in html
        assert f'tab-active font-semibold">{active}</a>' in html
    sidebar = _admin(client).get("/").text
    assert 'href="/settings"' in sidebar
    assert 'aria-label="Configuration"' not in sidebar


def test_the_modules_page_has_a_switch_for_the_four_optional_modules(client: TestClient) -> None:
    html = _admin(client).get("/settings/modules").text
    assert html.count('onchange="moduleToggled(this)"') == 4
    assert "Optional modules" in html
    assert 'data-module="manager"' in html and 'data-module="rag"' in html
    # The manager is shown and locked.
    assert "disabled>" in html.split('data-module="manager"')[1][:200]


def test_the_page_links_the_mail_settings_and_hides_the_rag_link_while_it_is_off(
    client: TestClient, config_dir: Path
) -> None:
    html = _admin(client).get("/settings/modules").text
    assert "set it up first" in html  # no mail server yet
    assert ">Open the RAG pages</a>" not in html
    _mail_server(config_dir)
    _on(config_dir, "rag")
    html = _admin(client).get("/settings/modules").text
    assert "set it up first" not in html
    assert ">Mail server settings</a>" in html
    assert ">Open the RAG pages</a>" in html


def test_the_dialog_offers_the_three_states_and_asks_for_the_name(client: TestClient) -> None:
    html = _admin(client).get("/settings/modules").text
    assert 'id="module-off-dialog"' in html
    assert "Keep data and settings" in html
    assert "Delete the data, keep the settings" in html
    assert "Delete the data and the settings" in html
    assert "to confirm the deletion" in html


def test_the_overlay_page_carries_no_overlay_content(client: TestClient, config_dir: Path) -> None:
    _seed(config_dir, _DRAFT.encode())
    html = _admin(client).get("/settings/overlay").text
    assert "overlay-init" in html
    assert _SECRET not in html


def test_the_overlay_page_opens_the_target_it_was_asked_for(client: TestClient) -> None:
    html = _admin(client).get("/settings/overlay?target=searxng").text
    assert '"selected": "searxng"' in html
    assert '"selected": "librechat"' in _admin(client).get("/settings/overlay?target=nope").text


def test_the_job_strip_is_empty_without_a_job(client: TestClient) -> None:
    html = _admin(client).get("/partials/config/job-status").text
    assert 'id="config-job-status"' in html
    assert "papaia-config-job" in html
