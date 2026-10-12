"""The Mail page and API: who may call what, what is asked of the core, and what is recorded.

The jobs themselves are pinned in `test_mail_service.py`, the rules in `test_mail_settings.py` and
the conversations in `test_mail_probe.py`. This is the HTTP surface around them: the role that
gates it, the status codes, that nothing is changed without the CSRF token, that every change
leaves an audit entry, and that the password is nowhere in a response, a log line or the audit log.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
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
    "PAPAIA_CONFIG_DIR": tempfile.mkdtemp(prefix="papaia-mail-api-config-"),
    "PAPAIA_WORKSPACE_DIR": tempfile.mkdtemp(prefix="papaia-mail-api-workspace-"),
}.items():
    os.environ.setdefault(_key, _value)

from fastapi.testclient import TestClient  # noqa: E402
from itsdangerous import TimestampSigner  # noqa: E402

import app.main as app_main  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.core import runner  # noqa: E402
from app.core.audit import audit_path  # noqa: E402
from app.core.jobs import JobQueue  # noqa: E402
from app.core.mail import status as mail_status  # noqa: E402
from app.core.mail.probe import ProbeResult, SendResult  # noqa: E402
from app.main import create_app  # noqa: E402
from app.routers import api_mail  # noqa: E402

_CSRF = "test-csrf-token-value"
_CSRF_HEADER = {"X-CSRF-Token": _CSRF}
_BASE = "/api/v1/mail"
_SECRET = "s3cret-pass-word-value"

_MODULE = (
    "SMTP_RELAY_UPSTREAM_HOST=smtp.example.org\n"
    "SMTP_RELAY_UPSTREAM_PORT=587\n"
    "SMTP_RELAY_UPSTREAM_SECURITY=starttls\n"
    "SMTP_RELAY_UPSTREAM_USER=mailer\n"
    "SMTP_RELAY_FROM_ADDRESS=noreply@example.org\n"
    "SMTP_RELAY_FROM_NAME=Our Mail\n"
    "SMTP_RELAY_PASSWORD_REV=1\n"
)
_REGISTRY = '    ("disk_space", check_disk_space),\n    ("smtp", check_smtp),\n'


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    lib = tmp_path / "workspace" / "papaia" / "tools" / "lib"
    lib.mkdir(parents=True)
    (lib / "smtp.py").write_text("", encoding="utf-8")
    (lib / "doctor.py").write_text(_REGISTRY, encoding="utf-8")
    return tmp_path / "workspace"


def _configure(config_dir: Path, *, on: bool, password: str | None = _SECRET) -> None:
    (config_dir / "manager").mkdir(parents=True, exist_ok=True)
    (config_dir / ".env").write_text(
        "PAPAIA_HOST=https://papaia.test\nCOMPOSE_PROFILES=nginx" + (",smtp" if on else "") + "\n",
        encoding="utf-8",
    )
    module = config_dir / "infra" / "smtp-relay"
    module.mkdir(parents=True, exist_ok=True)
    (module / ".env").write_text(_MODULE, encoding="utf-8")
    if password is not None:
        (module / "upstream-password").write_text(password, encoding="utf-8")


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "config"
    _configure(directory, on=True)
    return directory


@pytest.fixture
def queue(config_dir: Path) -> Iterator[JobQueue]:
    # Not started: a job is enqueued and stays there, which is all these tests look at.
    app_main._job_queue = JobQueue(config_dir=str(config_dir))  # noqa: SLF001
    yield app_main._job_queue  # noqa: SLF001
    app_main._job_queue = None  # noqa: SLF001


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No Docker, no doctor, no rate limit carried over from another test."""
    running: dict[str, Any] = {}

    async def find_runner(kind: Any = None) -> Any:
        return running.get(getattr(kind, "label", "restore"))

    monkeypatch.setattr(runner, "find_runner", find_runner)
    monkeypatch.setattr(api_mail, "_last_test_message", 0.0)

    async def doctor(**_: Any) -> tuple[int, str, str]:
        document = {
            "schema_version": 1,
            "checks": [
                {
                    "name": "smtp",
                    "status": "pass",
                    "summary": "relay running; queue empty",
                    "details": {"relay": "running", "queued": 0},
                }
            ],
        }
        return 0, json.dumps(document), ""

    monkeypatch.setattr(mail_status, "run_py_cli", doctor)
    mail_status.reset_cache()
    yield
    mail_status.reset_cache()


def _make_client(config_dir: Path, workspace: Path) -> TestClient:
    get_settings.cache_clear()
    settings = get_settings().model_copy(
        update={
            "papaia_config_dir": str(config_dir),
            "papaia_workspace_dir": str(workspace),
        }
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


def _audit(config_dir: Path) -> list[dict[str, Any]]:
    path = audit_path(str(config_dir))
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _revision(client: TestClient) -> str:
    return str(_admin(client).get(_BASE).json()["settings"]["revision"])


def _put_body(client: TestClient, **changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "revision": _revision(client),
        "host": "smtp.example.org",
        "port": 587,
        "security": "starttls",
        "user": "mailer",
        "from_address": "noreply@example.org",
        "from_name": "Our Mail",
    }
    body.update(changes)
    return body


# -- who may call it -----------------------------------------------------------------

_READS = [_BASE, "/mail/server", "/partials/mail/status", "/partials/mail/job-status"]
_WRITES = [
    ("put", _BASE, {"revision": "x", "host": "h", "from_address": "a@example.org"}),
    ("post", f"{_BASE}/disable", {"revision": "x"}),
    ("post", f"{_BASE}/probe", {}),
    ("post", f"{_BASE}/test-message", {"to": "a@example.org"}),
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
    assert (
        _as(client, *roles).request(method, path, json=body, headers=_CSRF_HEADER).status_code
        == 403
    )


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
    client: TestClient, config_dir: Path, queue: JobQueue, method: str, path: str, body: Any
) -> None:
    response = _admin(client).request(method, path, json=body)
    assert response.status_code == 403
    assert queue.list_jobs() == []
    assert _audit(config_dir) == []


def test_every_route_that_changes_or_tries_something_is_one_this_module_covers(
    client: TestClient,
) -> None:
    """A new mutating route has to be added to `_WRITES` -- and so to the tests above."""
    paths: dict[str, dict[str, Any]] = client.app.openapi()["paths"]  # type: ignore[attr-defined]
    changing = {
        (method.upper(), path)
        for path, operations in paths.items()
        if path.startswith(_BASE)
        for method in operations
        if method.upper() in {"POST", "PUT", "PATCH", "DELETE"}
    }
    assert changing == {(method.upper(), path) for method, path, _ in _WRITES}


# -- reading -------------------------------------------------------------------------


def test_the_document_has_the_settings_and_the_status_and_never_the_password(
    client: TestClient,
) -> None:
    response = _admin(client).get(_BASE)
    assert response.status_code == 200
    body = response.json()
    assert body["supported"] is True
    assert body["settings"]["enabled"] is True
    assert body["settings"]["host"] == "smtp.example.org"
    assert body["settings"]["has_password"] is True
    assert body["status"]["available"] is True
    assert body["status"]["relay"] == "running"
    assert _SECRET not in response.text


def test_a_relay_that_is_off_has_no_status_and_the_doctor_is_not_asked(
    config_dir: Path, workspace: Path, queue: JobQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(config_dir, on=False)

    async def fail(**_: Any) -> tuple[int, str, str]:  # pragma: no cover - must not run
        raise AssertionError("doctor must not be run for a relay that is off")

    monkeypatch.setattr(mail_status, "run_py_cli", fail)
    body = _admin(_make_client(config_dir, workspace)).get(_BASE).json()
    assert body["settings"]["enabled"] is False
    assert body["status"] is None


def test_a_core_without_the_relay_is_reported_not_failed(
    config_dir: Path, tmp_path: Path, queue: JobQueue
) -> None:
    old = tmp_path / "old-core"
    (old / "papaia" / "tools" / "lib").mkdir(parents=True)
    client = _make_client(config_dir, old)
    body = _admin(client).get(_BASE).json()
    assert body["supported"] is False
    assert body["status"] is None
    page = _admin(client).get("/mail/server")
    assert page.status_code == 200
    assert "no mail relay" in page.text
    assert 'id="mail-form"' not in page.text
    # ... and nothing can be changed on it.
    response = _admin(client).put(_BASE, json={}, headers=_CSRF_HEADER)
    assert response.status_code in (409, 422)


# -- switching on and changing -------------------------------------------------------


def test_a_valid_request_queues_the_apply_job_and_records_nothing_yet(
    client: TestClient, config_dir: Path, queue: JobQueue
) -> None:
    body = _put_body(client, password="a-new-password-value")
    response = _admin(client).put(_BASE, json=body, headers=_CSRF_HEADER)
    assert response.status_code == 202
    (job,) = queue.list_jobs()
    assert response.json() == {"job_id": job.id, "status": "queued"}
    assert (job.action, job.target, job.user) == ("mail-apply", "smtp-relay", "admin")
    # The entry is written when the job succeeds, not when it is queued.
    assert _audit(config_dir) == []
    assert "a-new-password-value" not in json.dumps(job.params)
    assert "a-new-password-value" not in response.text


def test_the_stored_password_is_kept_when_the_field_is_left_empty(
    client: TestClient, queue: JobQueue
) -> None:
    response = _admin(client).put(_BASE, json=_put_body(client), headers=_CSRF_HEADER)
    assert response.status_code == 202


@pytest.mark.parametrize(
    "change",
    [{"host": "other.example.org"}, {"port": 465}, {"user": "someone-else"}],
)
def test_the_stored_password_does_not_follow_a_change_of_server_port_or_login(
    client: TestClient, queue: JobQueue, change: dict[str, Any]
) -> None:
    response = _admin(client).put(_BASE, json=_put_body(client, **change), headers=_CSRF_HEADER)
    assert response.status_code == 422
    assert "enter the password again" in response.json()["detail"]
    assert queue.list_jobs() == []


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        ({"host": "smtp.example.org:587"}, "not a valid mail server host name"),
        ({"security": "ssl"}, "not a valid connection security"),
        ({"port": 70000}, "not a valid port"),
        ({"from_address": "nobody"}, "not a valid sender address"),
        ({"user": "", "password": "x"}, "needs a login"),
        ({"security": "none", "port": 25, "password": "x"}, "unencrypted"),
    ],
)
def test_a_value_the_rules_refuse_is_a_422_with_the_reason(
    client: TestClient, queue: JobQueue, change: dict[str, Any], fragment: str
) -> None:
    response = _admin(client).put(_BASE, json=_put_body(client, **change), headers=_CSRF_HEADER)
    assert response.status_code == 422
    assert fragment in response.json()["detail"]
    assert queue.list_jobs() == []


def test_a_refusal_never_repeats_the_password(client: TestClient, queue: JobQueue) -> None:
    # A Pydantic error would echo the input of a missing field, which is the whole body.
    body = {"revision": "x", "from_address": "noreply@example.org", "password": _SECRET}
    response = _admin(client).put(_BASE, json=body, headers=_CSRF_HEADER)
    assert response.status_code == 422
    assert "host" in response.json()["detail"]
    assert _SECRET not in response.text

    bad = _put_body(client, password=_SECRET, port="not-a-number")
    response = _admin(client).put(_BASE, json=bad, headers=_CSRF_HEADER)
    assert response.status_code == 422
    assert _SECRET not in response.text


def test_a_body_that_is_not_json_is_a_422(client: TestClient) -> None:
    response = _admin(client).put(
        _BASE, content=b"{nope", headers={**_CSRF_HEADER, "Content-Type": "application/json"}
    )
    assert response.status_code == 422


def test_a_form_from_a_stale_tab_is_refused(client: TestClient, queue: JobQueue) -> None:
    body = _put_body(client, revision="from-an-older-page")
    response = _admin(client).put(_BASE, json=body, headers=_CSRF_HEADER)
    assert response.status_code == 409
    assert "changed since" in response.json()["detail"]
    assert queue.list_jobs() == []


@pytest.mark.parametrize("busy", ["restore", "upgrade", "stack"])
def test_a_change_is_refused_while_a_restore_an_upgrade_or_a_stack_action_runs(
    client: TestClient, queue: JobQueue, monkeypatch: pytest.MonkeyPatch, busy: str
) -> None:
    kinds = {
        "restore": runner.RESTORE_KIND,
        "upgrade": runner.UPGRADE_KIND,
        "stack": runner.STACK_KIND,
    }

    async def find_runner(kind: Any = runner.RESTORE_KIND) -> Any:
        return SimpleNamespace(is_running=True, target="x") if kind is kinds[busy] else None

    monkeypatch.setattr(runner, "find_runner", find_runner)
    response = _admin(client).put(_BASE, json=_put_body(client), headers=_CSRF_HEADER)
    assert response.status_code == 409
    assert "still running" in response.json()["detail"]
    assert queue.list_jobs() == []


def test_an_unreachable_docker_is_a_503_not_a_trace(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def find_runner(kind: Any = None) -> Any:
        raise runner.RunnerError("cannot reach the Docker socket")

    monkeypatch.setattr(runner, "find_runner", find_runner)
    response = _admin(client).put(_BASE, json=_put_body(client), headers=_CSRF_HEADER)
    assert response.status_code == 503


def test_the_password_is_in_no_log_line(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    body = _put_body(client, password=_SECRET)
    _admin(client).put(_BASE, json=body, headers=_CSRF_HEADER)
    assert _SECRET not in caplog.text


# -- switching off -------------------------------------------------------------------


def test_switching_off_queues_the_disable_job(client: TestClient, queue: JobQueue) -> None:
    response = _admin(client).post(
        f"{_BASE}/disable", json={"revision": _revision(client)}, headers=_CSRF_HEADER
    )
    assert response.status_code == 202
    (job,) = queue.list_jobs()
    assert (job.action, job.target) == ("mail-disable", "smtp-relay")


def test_a_relay_that_is_off_cannot_be_switched_off_again(
    config_dir: Path, workspace: Path, queue: JobQueue
) -> None:
    _configure(config_dir, on=False)
    client = _admin(_make_client(config_dir, workspace))
    revision = client.get(_BASE).json()["settings"]["revision"]
    response = client.post(f"{_BASE}/disable", json={"revision": revision}, headers=_CSRF_HEADER)
    assert response.status_code == 409
    assert queue.list_jobs() == []


def test_switching_off_from_a_stale_tab_is_refused(client: TestClient, queue: JobQueue) -> None:
    response = _admin(client).post(
        f"{_BASE}/disable", json={"revision": "old"}, headers=_CSRF_HEADER
    )
    assert response.status_code == 409
    assert queue.list_jobs() == []


# -- testing the connection ----------------------------------------------------------


@pytest.fixture
def probes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    calls: list[tuple[Any, ...]] = []

    async def probe(host: str, port: int, security: str, user: str, password: str) -> ProbeResult:
        calls.append((host, port, security, user, password))
        return ProbeResult(True, "ok", login="ok" if user else "not needed")

    monkeypatch.setattr(api_mail.mail_probe, "probe", probe)
    return calls


def test_a_probe_without_values_tries_what_is_stored_with_the_stored_password(
    client: TestClient, probes: list[tuple[Any, ...]], config_dir: Path
) -> None:
    response = _admin(client).post(f"{_BASE}/probe", json={}, headers=_CSRF_HEADER)
    assert response.status_code == 200
    assert response.json() == {"ok": True, "stage": "ok", "reason": "", "login": "ok"}
    assert probes == [("smtp.example.org", 587, "starttls", "mailer", _SECRET)]
    assert _SECRET not in response.text
    (entry,) = _audit(config_dir)
    assert entry["action"] == "mail.probe"
    assert entry["result"] == "ok"
    assert _SECRET not in json.dumps(entry)


def test_a_typed_password_is_the_one_that_is_tried(
    client: TestClient, probes: list[tuple[Any, ...]]
) -> None:
    body = {
        "host": "other.example.org",
        "port": 465,
        "security": "tls",
        "user": "x",
        "password": "typed-one",
    }
    assert _admin(client).post(f"{_BASE}/probe", json=body, headers=_CSRF_HEADER).status_code == 200
    assert probes == [("other.example.org", 465, "tls", "x", "typed-one")]


@pytest.mark.parametrize(
    "change",
    [{"host": "other.example.org"}, {"port": 465, "security": "tls"}, {"user": "someone-else"}],
)
def test_the_stored_password_is_never_sent_to_another_address(
    client: TestClient, probes: list[tuple[Any, ...]], change: dict[str, Any]
) -> None:
    response = _admin(client).post(f"{_BASE}/probe", json=change, headers=_CSRF_HEADER)
    assert response.status_code == 422
    assert probes == []
    assert _SECRET not in response.text


def test_a_failed_handshake_is_an_answer_not_an_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, config_dir: Path
) -> None:
    async def probe(*_: Any) -> ProbeResult:
        return ProbeResult(False, "auth", "login refused: 535 credentials rejected")

    monkeypatch.setattr(api_mail.mail_probe, "probe", probe)
    response = _admin(client).post(f"{_BASE}/probe", json={}, headers=_CSRF_HEADER)
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["stage"] == "auth"
    assert _audit(config_dir)[0]["result"] == "failed"


def test_a_login_over_no_encryption_is_not_tried(
    client: TestClient, probes: list[tuple[Any, ...]]
) -> None:
    body = {"security": "none", "port": 25, "user": "mailer", "password": "x"}
    response = _admin(client).post(f"{_BASE}/probe", json=body, headers=_CSRF_HEADER)
    assert response.status_code == 422
    assert probes == []


def test_a_server_without_a_login_is_tried_without_one(
    client: TestClient, probes: list[tuple[Any, ...]]
) -> None:
    body = {"host": "relay.lan", "port": 25, "security": "none", "user": ""}
    assert _admin(client).post(f"{_BASE}/probe", json=body, headers=_CSRF_HEADER).status_code == 200
    assert probes == [("relay.lan", 25, "none", "", "")]


def test_a_password_without_a_login_is_refused_by_the_probe_too(
    client: TestClient, probes: list[tuple[Any, ...]]
) -> None:
    body = {"host": "relay.lan", "port": 25, "security": "none", "user": "", "password": "x"}
    assert _admin(client).post(f"{_BASE}/probe", json=body, headers=_CSRF_HEADER).status_code == 422
    assert probes == []


# -- the test mail -------------------------------------------------------------------


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    calls: list[dict[str, str]] = []

    async def send(*, to: str, from_address: str, from_name: str) -> SendResult:
        calls.append({"to": to, "from": from_address, "name": from_name})
        return SendResult(True)

    monkeypatch.setattr(api_mail.mail_probe, "send", send)
    return calls


def test_a_test_mail_goes_from_the_stored_sender_and_is_audited(
    client: TestClient, sent: list[dict[str, str]], config_dir: Path
) -> None:
    response = _admin(client).post(
        f"{_BASE}/test-message", json={"to": "someone@example.org"}, headers=_CSRF_HEADER
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "reason": ""}
    assert sent == [
        {"to": "someone@example.org", "from": "noreply@example.org", "name": "Our Mail"}
    ]
    (entry,) = _audit(config_dir)
    assert (entry["action"], entry["result"]) == ("mail.test-message", "ok")
    # Whom it went to is the operator's business and a stranger's address; it is not kept.
    assert "someone@example.org" not in json.dumps(entry)


def test_a_test_mail_that_the_relay_refuses_is_an_answer_with_its_words(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, config_dir: Path
) -> None:
    async def send(**_: Any) -> SendResult:
        return SendResult(
            False, "the relay refused the recipient: 550 recipient domain does not resolve"
        )

    monkeypatch.setattr(api_mail.mail_probe, "send", send)
    response = _admin(client).post(
        f"{_BASE}/test-message", json={"to": "someone@nowhere.example"}, headers=_CSRF_HEADER
    )
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert "550" in response.json()["reason"]
    assert _audit(config_dir)[0]["result"] == "failed"


@pytest.mark.parametrize(
    "address", ["", "nobody", "Name <a@example.org>", "a@example.org\nBcc: b@example.org"]
)
def test_a_bad_address_is_a_422_and_sends_nothing(
    client: TestClient, sent: list[dict[str, str]], address: str
) -> None:
    response = _admin(client).post(
        f"{_BASE}/test-message", json={"to": address}, headers=_CSRF_HEADER
    )
    assert response.status_code == 422
    assert sent == []


def test_a_test_mail_needs_a_relay_that_is_on_and_configured(
    config_dir: Path, workspace: Path, queue: JobQueue, sent: list[dict[str, str]]
) -> None:
    _configure(config_dir, on=False)
    client = _admin(_make_client(config_dir, workspace))
    response = client.post(
        f"{_BASE}/test-message", json={"to": "a@example.org"}, headers=_CSRF_HEADER
    )
    assert response.status_code == 409
    assert sent == []


def test_test_mails_are_not_sent_in_a_burst(client: TestClient, sent: list[dict[str, str]]) -> None:
    body = {"to": "someone@example.org"}
    assert (
        _admin(client).post(f"{_BASE}/test-message", json=body, headers=_CSRF_HEADER).status_code
        == 200
    )
    again = _admin(client).post(f"{_BASE}/test-message", json=body, headers=_CSRF_HEADER)
    assert again.status_code == 429
    assert len(sent) == 1


# -- the pages -----------------------------------------------------------------------


def test_the_page_has_the_form_the_test_mail_and_the_off_dialog(client: TestClient) -> None:
    response = _admin(client).get("/mail/server")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    text = response.text
    for element in (
        'id="mail-form"',
        'id="mail-host"',
        'id="mail-security"',
        'id="mail-port"',
        'id="mail-user"',
        'id="mail-password"',
        'id="mail-from"',
        'id="mail-test-to"',
        'id="mail-off-dialog"',
        "Test connection",
        "Save and apply",
        "Switch off",
    ):
        assert element in text
    assert 'value="smtp.example.org"' in text
    assert 'value="noreply@example.org"' in text


def test_the_page_never_contains_the_stored_password(client: TestClient) -> None:
    text = _admin(client).get("/mail/server").text
    assert _SECRET not in text
    assert "stored; leave empty to keep it" in text
    assert 'type="password"' in text


def test_a_relay_that_is_off_offers_to_switch_it_on_and_hides_the_off_button(
    config_dir: Path, workspace: Path, queue: JobQueue
) -> None:
    _configure(config_dir, on=False)
    text = _admin(_make_client(config_dir, workspace)).get("/mail/server").text
    assert "Switch on and apply" in text
    assert "Switch off" not in text
    assert 'id="mail-off-dialog"' not in text
    assert "Switch the relay on to send a test mail." in text


def test_a_relay_never_set_up_starts_with_a_login_and_the_defaults(
    tmp_path: Path, workspace: Path, queue: JobQueue
) -> None:
    empty = tmp_path / "fresh-config"
    (empty / "manager").mkdir(parents=True)
    text = _admin(_make_client(empty, workspace)).get("/mail/server").text
    assert 'id="mail-login" type="checkbox"' in text
    assert "checked" in text.split('id="mail-login"')[1].split(">")[0]
    assert 'value="587"' in text


def test_the_status_strip_says_what_the_doctor_found(client: TestClient) -> None:
    text = _admin(client).get("/partials/mail/status").text
    assert "Mail relay is on" in text
    assert "relay running" in text
    assert "queue empty" in text


def test_the_status_strip_shows_a_waiting_queue(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def doctor(**_: Any) -> tuple[int, str, str]:
        check = {
            "name": "smtp",
            "status": "warn",
            "summary": "2 message(s) waiting in the relay's queue",
            "details": {"relay": "running", "queued": 2},
        }
        return 0, json.dumps({"schema_version": 1, "checks": [check]}), ""

    monkeypatch.setattr(mail_status, "run_py_cli", doctor)
    text = _admin(client).get("/partials/mail/status?fresh=true").text
    assert "2 messages waiting" in text


def test_a_status_that_cannot_be_read_is_said_so(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def doctor(**_: Any) -> tuple[int, str, str]:
        return 2, "", "doctor: cannot reach docker"

    monkeypatch.setattr(mail_status, "run_py_cli", doctor)
    text = _admin(client).get("/partials/mail/status").text
    assert "could not be read" in text
    assert "cannot reach docker" in text


def test_the_status_of_a_relay_that_is_off_is_a_sentence_without_asking_the_doctor(
    config_dir: Path, workspace: Path, queue: JobQueue
) -> None:
    _configure(config_dir, on=False)
    text = _admin(_make_client(config_dir, workspace)).get("/partials/mail/status").text
    assert "Mail relay is off" in text
    assert "settings below are stored" in text


def test_the_job_strip_is_empty_without_a_job_and_shows_a_queued_change(
    client: TestClient, queue: JobQueue
) -> None:
    assert "Applying mail settings" not in _admin(client).get("/partials/mail/job-status").text
    assert (
        _admin(client).put(_BASE, json=_put_body(client), headers=_CSRF_HEADER).status_code == 202
    )
    text = _admin(client).get("/partials/mail/job-status").text
    assert "Applying mail settings: queued" in text
    assert '"papaia-mail-job"' in text


def test_another_jobs_presence_is_named_not_mistaken_for_ours(
    client: TestClient, queue: JobQueue
) -> None:
    import asyncio  # noqa: PLC0415

    async def nothing(ctx: Any) -> None:  # pragma: no cover - never run
        return None

    asyncio.run(queue.enqueue(action="backup", target="core", user="admin", callback=nothing))
    text = _admin(client).get("/partials/mail/job-status").text
    assert "backup queued" in text
    assert "queued behind it" in text


def test_the_sidebar_offers_the_page_to_administrators_only(client: TestClient) -> None:
    assert 'href="/mail"' in _admin(client).get("/audit").text
    assert 'href="/mail"' not in _as(client, "user").get("/").text


def test_the_users_page_points_at_the_mail_page_when_the_realm_has_no_mail_server(
    client: TestClient,
) -> None:
    text = _as(client, "papaia-admin").get("/users").text
    assert "set one up on the Mail page" in text
