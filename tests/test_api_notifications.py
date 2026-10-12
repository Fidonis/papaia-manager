"""The Notifications tab and its API: who may call what, what is stored, and what is shown.

The rules are pinned in `test_notifications_store.py`, the detection in
`test_notifications_detect.py` and the delivery in `test_notifications_delivery.py`. This is the
HTTP surface around them: the role that gates it, the status codes, that nothing is changed
without the CSRF token or on a stale revision, that every change leaves an audit entry, and that
a webhook URL is in no response, no page and no log.
"""

from __future__ import annotations

import base64
import html
import json
import os
import tempfile
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
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
    "PAPAIA_CONFIG_DIR": tempfile.mkdtemp(prefix="papaia-notify-api-config-"),
    "PAPAIA_WORKSPACE_DIR": tempfile.mkdtemp(prefix="papaia-notify-api-workspace-"),
}.items():
    os.environ.setdefault(_key, _value)

from fastapi.testclient import TestClient  # noqa: E402
from itsdangerous import TimestampSigner  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.core.audit import audit_path  # noqa: E402
from app.core.notifications import service as notify_service  # noqa: E402
from app.core.notifications import store  # noqa: E402
from app.main import create_app  # noqa: E402
from app.routers import api_notifications, ui_notifications  # noqa: E402

_CSRF = "test-csrf-token-value"
_CSRF_HEADER = {"X-CSRF-Token": _CSRF}
_BASE = "/api/v1/notifications"
_HOOK = "https://hooks.example.com/services/T000/B000/s3cr3t-token"
_ADDRESS = "ops@customer.example"

_MODULE = (
    "SMTP_RELAY_UPSTREAM_HOST=smtp.example.org\n"
    "SMTP_RELAY_FROM_ADDRESS=noreply@example.org\n"
    "SMTP_RELAY_FROM_NAME=Our Mail\n"
)


def _relay(config_dir: Path, *, on: bool) -> None:
    (config_dir / ".env").write_text(
        "PAPAIA_HOST=https://papaia.test\nCOMPOSE_PROFILES=nginx" + (",smtp" if on else "") + "\n",
        encoding="utf-8",
    )
    module = config_dir / "infra" / "smtp-relay"
    module.mkdir(parents=True, exist_ok=True)
    (module / ".env").write_text(_MODULE, encoding="utf-8")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    lib = tmp_path / "workspace" / "papaia" / "tools" / "lib"
    lib.mkdir(parents=True)
    (lib / "smtp.py").write_text("", encoding="utf-8")
    (lib / "doctor.py").write_text('    ("smtp", check_smtp),\n', encoding="utf-8")
    return tmp_path / "workspace"


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "config"
    directory.mkdir()
    _relay(directory, on=True)
    return directory


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_notifications, "_last_test", {})


def _make_client(config_dir: Path, workspace: Path) -> TestClient:
    get_settings.cache_clear()
    settings = get_settings().model_copy(
        update={"papaia_config_dir": str(config_dir), "papaia_workspace_dir": str(workspace)}
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def client(config_dir: Path, workspace: Path) -> Iterator[TestClient]:
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
    return str(_admin(client).get(_BASE).json()["revision"])


def _create(client: TestClient, **changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "revision": _revision(client),
        "name": "Ops team",
        "kind": "email",
        "target": _ADDRESS,
        "enabled": True,
        "events": ["job_failed"],
    }
    body.update(changes)
    response = _admin(client).post(f"{_BASE}/recipients", json=body, headers=_CSRF_HEADER)
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


# -- who may call it -----------------------------------------------------------------

_READS = [_BASE, "/mail", "/partials/notifications/status", "/partials/notifications/log"]
_WRITES = [
    (
        "post",
        f"{_BASE}/recipients",
        {"revision": "x", "name": "n", "kind": "email", "target": "a@b.co"},
    ),
    ("put", f"{_BASE}/recipients/abc", {"revision": "x"}),
    ("put", f"{_BASE}/recipients/abc/events", {"revision": "x", "events": []}),
    ("delete", f"{_BASE}/recipients/abc", {"revision": "x"}),
    (
        "put",
        f"{_BASE}/settings",
        {
            "revision": "x",
            "check_interval_minutes": 5,
            "debounce_minutes": 30,
            "cert_lead_days": 14,
        },
    ),
    ("post", f"{_BASE}/recipients/abc/test", {}),
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
    client: TestClient, config_dir: Path, method: str, path: str, body: Any
) -> None:
    response = _admin(client).request(method, path, json=body)
    assert response.status_code == 403
    assert not store.config_path(str(config_dir)).exists()
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
    expected = {
        (method.upper(), path.replace("/abc", "/{recipient_id}")) for method, path, _ in _WRITES
    }
    assert changing == expected


# -- reading -------------------------------------------------------------------------


def test_an_empty_installation_has_the_catalogue_the_defaults_and_a_ready_mail_server(
    client: TestClient,
) -> None:
    body = _admin(client).get(_BASE).json()
    assert body["recipients"] == [] and body["error"] is None
    assert body["settings"] == {
        "check_interval_minutes": 5,
        "debounce_minutes": 30,
        "cert_lead_days": 14,
    }
    assert [e["id"] for e in body["events"]] == [
        "job_failed",
        "backup_overdue",
        "service_unhealthy",
        "update_available",
        "cert_expiring",
    ]
    assert body["mail"] == {"supported": True, "ready": True}


def test_a_mail_server_that_is_off_is_not_ready(client: TestClient, config_dir: Path) -> None:
    _relay(config_dir, on=False)
    assert _admin(client).get(_BASE).json()["mail"] == {"supported": True, "ready": False}


def test_a_core_without_the_relay_cannot_send_email(config_dir: Path, tmp_path: Path) -> None:
    old = tmp_path / "old"
    (old / "papaia" / "tools" / "lib").mkdir(parents=True)
    client = _make_client(config_dir, old)
    assert _admin(client).get(_BASE).json()["mail"] == {"supported": False, "ready": False}
    refused = _admin(client).post(
        f"{_BASE}/recipients",
        json={"revision": "", "name": "n", "kind": "email", "target": _ADDRESS, "events": []},
        headers=_CSRF_HEADER,
    )
    assert refused.status_code == 409 and "no mail relay" in refused.json()["detail"]
    # A webhook does not need it.
    created = _admin(client).post(
        f"{_BASE}/recipients",
        json={"revision": "", "name": "n", "kind": "webhook", "target": _HOOK, "events": []},
        headers=_CSRF_HEADER,
    )
    assert created.status_code == 201


# -- creating, changing, deleting ----------------------------------------------------


def test_a_recipient_is_created_and_audited_without_its_address(
    client: TestClient, config_dir: Path
) -> None:
    created = _create(client, events=["job_failed", "backup_overdue"])
    assert created["recipient"]["name"] == "Ops team"
    assert created["recipient"]["target"] == _ADDRESS
    assert created["recipient"]["events"] == ["job_failed", "backup_overdue"]
    assert created["recipient"]["test"]["state"] == "untested"
    (entry,) = _audit(config_dir)
    assert entry["action"] == "notifications.recipient.create" and entry["target"] == "Ops team"
    assert entry["params"] == {
        "kind": "email",
        "enabled": True,
        "events": ["job_failed", "backup_overdue"],
    }
    assert _ADDRESS not in json.dumps(entry)


def test_a_webhook_url_is_in_no_response_no_log_and_no_page(
    client: TestClient, config_dir: Path
) -> None:
    created = _create(client, name="Chat", kind="webhook", target=_HOOK)
    assert created["recipient"]["target"] == "https://hooks.example.com/…"
    seen = [
        json.dumps(created),
        json.dumps(_admin(client).get(_BASE).json()),
        _admin(client).get("/mail").text,
        json.dumps(_audit(config_dir)),
    ]
    for text in seen:
        assert "s3cr3t" not in text and "T000" not in text
    # ... while the file keeps it, for the delivery.
    assert store.load_config(str(config_dir)).config.recipients[0].target == _HOOK


def test_a_refusal_repeats_nothing_it_was_given(client: TestClient, config_dir: Path) -> None:
    revision = _revision(client)
    for body in (
        {"revision": revision, "name": "x", "kind": "webhook", "target": "ftp://s3cr3t.example/x"},
        {
            "revision": revision,
            "name": "x",
            "kind": "webhook",
            "target": "https://u:s3cr3t@h.example/",
        },
        {"revision": revision, "name": "x", "kind": "email", "target": "s3cr3t at nowhere"},
        {"revision": revision, "kind": "webhook", "target": "https://s3cr3t.example/x"},  # no name
        {"revision": revision, "name": "x", "kind": "carrier-pigeon", "target": "s3cr3t"},
    ):
        response = _admin(client).post(f"{_BASE}/recipients", json=body, headers=_CSRF_HEADER)
        assert response.status_code == 422
        assert "s3cr3t" not in response.text
    assert not store.config_path(str(config_dir)).exists()
    assert _audit(config_dir) == []


def test_a_body_that_is_not_json_is_a_refusal(client: TestClient) -> None:
    response = _admin(client).post(
        f"{_BASE}/recipients",
        content=b"not json",
        headers={**_CSRF_HEADER, "Content-Type": "application/json"},
    )
    assert response.status_code == 422


def test_a_change_on_a_stale_revision_is_a_conflict_and_changes_nothing(
    client: TestClient, config_dir: Path
) -> None:
    created = _create(client)
    rid = created["recipient"]["id"]
    before = store.config_path(str(config_dir)).read_bytes()
    stale = _admin(client).put(
        f"{_BASE}/recipients/{rid}/events",
        json={"revision": "old", "events": ["cert_expiring"]},
        headers=_CSRF_HEADER,
    )
    assert stale.status_code == 409 and "Reload" in stale.json()["detail"]
    assert store.config_path(str(config_dir)).read_bytes() == before
    deleted = _admin(client).request(
        "DELETE", f"{_BASE}/recipients/{rid}", json={"revision": "old"}, headers=_CSRF_HEADER
    )
    assert deleted.status_code == 409


def test_ticking_in_the_matrix_replaces_the_events_of_one_recipient(
    client: TestClient, config_dir: Path
) -> None:
    first = _create(client, name="One")
    second = _create(client, name="Two", target="two@customer.example", events=["update_available"])
    changed = _admin(client).put(
        f"{_BASE}/recipients/{first['recipient']['id']}/events",
        json={
            "revision": second["revision"],
            "events": ["cert_expiring", "job_failed", "job_failed"],
        },
        headers=_CSRF_HEADER,
    )
    assert changed.status_code == 200
    assert changed.json()["events"] == ["job_failed", "cert_expiring"]
    again = {r["name"]: r["events"] for r in _admin(client).get(_BASE).json()["recipients"]}
    assert again == {"One": ["job_failed", "cert_expiring"], "Two": ["update_available"]}
    assert _audit(config_dir)[-1]["action"] == "notifications.recipient.update"


def test_a_recipient_is_edited_and_a_missing_target_keeps_the_stored_one(
    client: TestClient, config_dir: Path
) -> None:
    created = _create(client, name="Chat", kind="webhook", target=_HOOK)
    rid = created["recipient"]["id"]
    changed = _admin(client).put(
        f"{_BASE}/recipients/{rid}",
        json={"revision": created["revision"], "name": "On-call chat", "enabled": False},
        headers=_CSRF_HEADER,
    )
    assert changed.status_code == 200
    assert changed.json()["recipient"]["name"] == "On-call chat"
    assert changed.json()["recipient"]["enabled"] is False
    assert store.load_config(str(config_dir)).config.recipients[0].target == _HOOK
    entry = _audit(config_dir)[-1]
    assert entry["params"]["target_changed"] is False
    assert _HOOK not in json.dumps(_audit(config_dir))


def test_a_recipient_that_is_not_there_is_a_404(client: TestClient) -> None:
    revision = _revision(client)
    for method, suffix, body in (
        ("put", "", {"revision": revision, "name": "x"}),
        ("put", "/events", {"revision": revision, "events": []}),
        ("delete", "", {"revision": revision}),
    ):
        response = _admin(client).request(
            method, f"{_BASE}/recipients/nope{suffix}", json=body, headers=_CSRF_HEADER
        )
        assert response.status_code == 404


def test_deleting_a_recipient_leaves_nothing_of_it(client: TestClient, config_dir: Path) -> None:
    created = _create(client, events=["job_failed", "cert_expiring"])
    rid = created["recipient"]["id"]
    deleted = _admin(client).request(
        "DELETE",
        f"{_BASE}/recipients/{rid}",
        json={"revision": created["revision"]},
        headers=_CSRF_HEADER,
    )
    assert deleted.status_code == 200
    assert _admin(client).get(_BASE).json()["recipients"] == []
    assert "Ops team" not in store.config_path(str(config_dir)).read_text(encoding="utf-8")
    assert _audit(config_dir)[-1]["action"] == "notifications.recipient.delete"


def test_the_settings_are_validated_stored_and_audited(
    client: TestClient, config_dir: Path
) -> None:
    ok = _admin(client).put(
        f"{_BASE}/settings",
        json={
            "revision": _revision(client),
            "check_interval_minutes": 10,
            "debounce_minutes": 45,
            "cert_lead_days": 21,
        },
        headers=_CSRF_HEADER,
    )
    assert ok.status_code == 200
    assert _admin(client).get(_BASE).json()["settings"] == {
        "check_interval_minutes": 10,
        "debounce_minutes": 45,
        "cert_lead_days": 21,
    }
    assert _audit(config_dir)[-1]["action"] == "notifications.settings"
    bad = _admin(client).put(
        f"{_BASE}/settings",
        json={
            "revision": ok.json()["revision"],
            "check_interval_minutes": 0,
            "debounce_minutes": 45,
            "cert_lead_days": 21,
        },
        headers=_CSRF_HEADER,
    )
    assert bad.status_code == 422 and "check_interval_minutes" in bad.json()["detail"]


def test_a_configuration_file_nobody_can_read_is_shown_and_refuses_changes(
    client: TestClient, config_dir: Path
) -> None:
    path = store.config_path(str(config_dir))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("surprise: 1\n", encoding="utf-8")
    body = _admin(client).get(_BASE).json()
    assert body["error"] and body["recipients"] == []
    refused = _admin(client).post(
        f"{_BASE}/recipients",
        json={
            "revision": body["revision"],
            "name": "n",
            "kind": "email",
            "target": _ADDRESS,
            "events": [],
        },
        headers=_CSRF_HEADER,
    )
    assert refused.status_code == 409 and "Repair or remove" in refused.json()["detail"]
    assert path.read_text(encoding="utf-8") == "surprise: 1\n"
    page = _admin(client).get("/mail")
    assert page.status_code == 200 and "cannot be used" in page.text
    assert "Add recipient" not in page.text


# -- the test button -----------------------------------------------------------------


def _hook_transport(status: int) -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(status))


@pytest.fixture
def delivered(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[httpx.Request]]:
    """Route the webhook post of a test notification to a stand-in receiver."""
    posts: list[httpx.Request] = []
    status = {"code": 200}

    def answer(request: httpx.Request) -> httpx.Response:
        posts.append(request)
        return httpx.Response(status["code"])

    real = notify_service.send_test

    async def send_test(*args: Any, **kwargs: Any) -> Any:
        return await real(*args, transport=httpx.MockTransport(answer), retry_delay=0, **kwargs)

    monkeypatch.setattr(api_notifications, "send_test", send_test)
    yield posts


def test_a_test_reaches_the_recipient_and_the_result_shows_on_the_page(
    client: TestClient, config_dir: Path, delivered: list[httpx.Request]
) -> None:
    created = _create(client, name="Chat", kind="webhook", target=_HOOK)
    rid = created["recipient"]["id"]
    response = _admin(client).post(f"{_BASE}/recipients/{rid}/test", headers=_CSRF_HEADER)
    assert response.status_code == 200 and response.json() == {"ok": True, "reason": ""}
    (post,) = delivered
    assert json.loads(post.content)["event"] == "test"
    recipient = _admin(client).get(_BASE).json()["recipients"][0]
    assert recipient["test"]["state"] == "passed"
    assert "Test passed" in _admin(client).get("/mail").text
    log = _admin(client).get("/partials/notifications/log").text
    assert "Chat" in log and "Sent" in log and "s3cr3t" not in log


def test_a_failed_test_is_an_answer_not_an_error_and_shows_why(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = notify_service.send_test

    async def send_test(*args: Any, **kwargs: Any) -> Any:
        return await real(*args, transport=_hook_transport(404), retry_delay=0, **kwargs)

    monkeypatch.setattr(api_notifications, "send_test", send_test)
    created = _create(client, name="Chat", kind="webhook", target=_HOOK)
    response = _admin(client).post(
        f"{_BASE}/recipients/{created['recipient']['id']}/test", headers=_CSRF_HEADER
    )
    assert response.status_code == 200
    assert response.json() == {"ok": False, "reason": "the webhook answered 404"}
    page = _admin(client).get("/mail").text
    assert "Test failed" in page and "the webhook answered 404" in page
    assert "Failed" in _admin(client).get("/partials/notifications/log").text


def test_a_test_for_a_recipient_that_is_gone_is_a_404(client: TestClient) -> None:
    response = _admin(client).post(f"{_BASE}/recipients/nope/test", headers=_CSRF_HEADER)
    assert response.status_code == 404


def test_a_test_button_held_down_is_slowed_down(
    client: TestClient, delivered: list[httpx.Request]
) -> None:
    created = _create(client, name="Chat", kind="webhook", target=_HOOK)
    url = f"{_BASE}/recipients/{created['recipient']['id']}/test"
    assert _admin(client).post(url, headers=_CSRF_HEADER).status_code == 200
    assert _admin(client).post(url, headers=_CSRF_HEADER).status_code == 429
    assert len(delivered) == 1


# -- the pages -----------------------------------------------------------------------


def test_the_page_has_the_tabs_in_order_the_matrix_and_the_dialogs(client: TestClient) -> None:
    _create(client, events=["job_failed", "cert_expiring"])
    response = _admin(client).get("/mail")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    text = response.text
    assert text.index('href="/mail"') < text.index('href="/mail/server"')
    assert ">Notifications</a>" in text and ">Mail Server</a>" in text
    for element in (
        'id="recipient-modal"',
        'id="recipient-delete-modal"',
        'id="notify-settings"',
        'id="notify-log"',
        "Add recipient",
        "Event settings",
    ):
        assert element in text
    # One checkbox per recipient and event, the two chosen ones ticked.
    assert text.count('data-event="') == 5
    assert text.count("checked") >= 2


def test_the_page_says_who_will_not_hear_anything_until_the_mail_server_is_set_up(
    client: TestClient, config_dir: Path
) -> None:
    _create(client, events=["job_failed"])
    _relay(config_dir, on=False)
    text = _admin(client).get("/mail").text
    assert "The Mail Server isn't set up yet" in text
    assert 'href="/mail/server"' in text
    assert "Waiting for Mail Server" in text


def test_a_webhook_recipient_does_not_wait_for_the_mail_server(
    client: TestClient, config_dir: Path
) -> None:
    _create(client, name="Chat", kind="webhook", target=_HOOK)
    _relay(config_dir, on=False)
    assert "Waiting for Mail Server" not in _admin(client).get("/mail").text


def _strip(client: TestClient) -> str:
    """The strip as it reads: entities decoded, a line break in the markup is a space."""
    return " ".join(
        html.unescape(_admin(client).get("/partials/notifications/status").text).split()
    )


def test_the_strip_says_when_the_background_task_is_not_running(client: TestClient) -> None:
    # A test client that never started up has no background task.
    assert "Notifications are not being sent" in _strip(client)


def test_the_strip_says_when_the_mail_server_is_not_set_up(
    client: TestClient, config_dir: Path
) -> None:
    _relay(config_dir, on=False)
    text = _strip(client)
    assert "The Mail Server isn't set up yet" in text
    assert 'href="/mail/server"' in text and "Webhook recipients work already" in text


def test_the_strip_says_when_the_core_has_no_relay(config_dir: Path, tmp_path: Path) -> None:
    old = tmp_path / "old"
    (old / "papaia" / "tools" / "lib").mkdir(parents=True)
    text = html.unescape(
        _admin(_make_client(config_dir, old)).get("/partials/notifications/status").text
    )
    assert "This papAIa core has no mail relay" in text and "1.6.0" in text


def test_the_strip_says_when_nobody_receives_anything_and_when_somebody_does(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    running = SimpleNamespace(last_pass_at=None)
    monkeypatch.setattr(ui_notifications, "_service", lambda: running)
    assert "Nobody receives notifications yet" in _strip(client)
    _create(client, events=[])  # a recipient that chose nothing is still nobody
    assert "Nobody receives notifications yet" in _strip(client)
    _create(client, name="Other", target="other@customer.example", events=["job_failed"])
    text = _strip(client)
    assert "Notifications are on" in text and "1 recipient" in text
    assert "every 5 minutes" in text and "shortly after the manager started" in text
    running.last_pass_at = datetime.now(tz=UTC)
    assert "last a moment ago" in _strip(client)


def test_the_strip_reports_a_configuration_nobody_can_read(
    client: TestClient, config_dir: Path
) -> None:
    path = store.config_path(str(config_dir))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("surprise: 1\n", encoding="utf-8")
    text = _strip(client)
    assert "cannot be used" in text and "notifications.yaml" in text


def test_an_empty_page_invites_the_first_recipient(client: TestClient) -> None:
    text = _admin(client).get("/mail").text
    assert "No recipients yet" in text
    assert "Nothing sent yet" in _admin(client).get("/partials/notifications/log").text


def test_the_mail_server_page_moved_and_keeps_its_form(client: TestClient) -> None:
    text = _admin(client).get("/mail/server").text
    assert 'id="mail-form"' in text
    assert ">Notifications</a>" in text and ">Mail Server</a>" in text
    assert 'id="recipient-modal"' not in text


def test_the_log_lists_deliveries_and_not_the_edits_an_administrator_made(
    client: TestClient, config_dir: Path
) -> None:
    _create(client)
    assert "Nothing sent yet" in _admin(client).get("/partials/notifications/log").text
    from app.core.audit import write_audit_entry

    write_audit_entry(
        str(config_dir),
        user="notifier",
        action="notify.failed",
        target="Ops team",
        params={"event": "service_unhealthy", "subject": "litellm", "reason": "the relay said no"},
        result="failed",
    )
    text = _admin(client).get("/partials/notifications/log").text
    assert "Service unhealthy" in text and "litellm" in text
    assert "Ops team" in text and "the relay said no" in text and "Failed" in text
