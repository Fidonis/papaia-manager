"""The page "Requests": confirm requests for information and erasure without an account.

OPS-08 (PLT-66-A1..A3), security 1.14 no. 2b: only a person with the confirmation role decides,
a confirmation needs a fresh sign-in, the person who filed a request cannot confirm it, the
passphrase of an export is shown once, and every decision leaves one audit entry and one
`papaia.audit/1` row. papaia-audit is simulated here (`FakeAudit`) with the shapes of its
read API (`GET /v1/person-requests`, `POST /v1/person-requests/<id>/confirm|reject`).
"""
from __future__ import annotations

import base64
import json
import os
import sqlite3
import tempfile
import time
from collections.abc import Iterator, Mapping
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
    "PAPAIA_CONFIG_DIR": tempfile.mkdtemp(prefix="papaia-person-requests-config-"),
    "PAPAIA_WORKSPACE_DIR": tempfile.mkdtemp(prefix="papaia-person-requests-workspace-"),
}.items():
    os.environ.setdefault(_key, _value)

from fastapi.testclient import TestClient  # noqa: E402
from itsdangerous import TimestampSigner  # noqa: E402
from papaia_kit.audit import validate_event  # noqa: E402
from papaia_kit.correlation import CORRELATION_HEADER  # noqa: E402

from app import main as app_main  # noqa: E402
from app.auth import oidc  # noqa: E402
from app.auth.roles import has_manager_access, is_confirmer  # noqa: E402
from app.config import Settings, get_settings  # noqa: E402
from app.core import kit_audit  # noqa: E402
from app.core import person_requests as pr  # noqa: E402
from app.main import create_app  # noqa: E402
from app.routers import ui_person_requests  # noqa: E402

_CSRF = "test-csrf-token-value"
_BASE = "https://papaia-audit.test:8443"
_EXPORT = "0b5c4a1e-1111-4aaa-8bbb-000000000001"
_ERASE = "0b5c4a1e-2222-4aaa-9bbb-000000000002"
_OWN = "0b5c4a1e-3333-4aaa-abbb-000000000003"
_DONE = "0b5c4a1e-4444-4aaa-bbbb-000000000004"
# A passphrase of papaia-audit's shape: 8 groups of 5 characters of Crockford's base32.
_PASSPHRASE = "7K2QH-M4XWZ-0PRC9-TB3VD-N8YAE-6F1GJ-QS5HK-2MW7X"


def _view(request_id: str, operation: str, state: str = "open", **over: Any) -> dict[str, Any]:
    view: dict[str, Any] = {
        "id": request_id,
        "operation": operation,
        "state": state,
        "requested_at": "2026-11-23T08:15:00.000Z",
        "decided_at": None,
        "person": "P-ANNAAAAA",
        "requested_by": "P-DORAAAAA",
        "confirmed_by": "P-ERIKAAAA",
        "transfer": False,
        "own": False,
        "can_confirm": state == "open",
    }
    view.update(over)
    return view


class FakeAudit:
    """papaia-audit's person request routes, for the person `P-ERIKAAAA` signed in."""

    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {
            _EXPORT: _view(_EXPORT, "export"),
            _ERASE: _view(_ERASE, "erase", transfer=True),
            _OWN: _view(
                _OWN, "export", requested_by="P-ERIKAAAA", confirmed_by="P-DORAAAAA",
                own=True, can_confirm=False,
            ),
            _DONE: _view(_DONE, "erase", state="done", decided_at="2026-11-22T10:00:00.000Z"),
        }  # fmt: skip
        self.calls: list[tuple[str, str, dict[str, str]]] = []
        self.refuse: tuple[int, dict[str, Any]] | None = None
        self.list_body: bytes | None = None

    def send(self, method: str, url: str, headers: Mapping[str, str]) -> tuple[int, bytes]:
        self.calls.append((method, url, dict(headers)))
        assert url.startswith(_BASE)
        path = url[len(_BASE):]
        if method == "GET" and path == "/v1/person-requests":
            if self.list_body is not None:
                return 200, self.list_body
            return 200, json.dumps({"requests": list(self.items.values())}).encode()
        assert method == "POST"
        _, _, _, request_id, decision = path.split("/")
        if self.refuse is not None:
            status, body = self.refuse
            return status, json.dumps(body).encode()
        item = self.items.get(request_id)
        if item is None:
            return 404, json.dumps({"code": "AUDIT-READ-404"}).encode()
        if item["state"] != "open":
            return 409, json.dumps({"code": "AUDIT-PERSON-REQ-409"}).encode()
        if decision == "confirm" and item["own"]:
            return 403, json.dumps({"code": "AUDIT-PERSON-REQ-SELF"}).encode()
        state = "confirmed" if decision == "confirm" else "rejected"
        item.update(state=state, decided_at="2026-11-23T09:00:00.000Z", can_confirm=False)
        answer = dict(item)
        if decision == "confirm" and item["operation"] == "export":
            answer["passphrase"] = _PASSPHRASE
        return 200, json.dumps(answer).encode()

    def decisions(self) -> list[str]:
        return [url for method, url, _ in self.calls if method == "POST"]


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "config"
    (directory / "manager").mkdir(parents=True)
    (directory / ".env").write_text("PAPAIA_HOST=https://papaia.test\n", encoding="utf-8")
    return directory


@pytest.fixture
def audit() -> Iterator[FakeAudit]:
    fake = FakeAudit()
    pr.use(pr.Wiring(base_url=_BASE, send=fake.send, exchange=lambda token, cid: f"aud-{token}"))
    yield fake
    pr.use(None)


@pytest.fixture
def client(config_dir: Path, audit: FakeAudit) -> Iterator[TestClient]:
    get_settings.cache_clear()
    settings = get_settings().model_copy(update={"papaia_config_dir": str(config_dir)})
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[ui_person_requests.person_token] = lambda: "person-token"
    yield TestClient(app, follow_redirects=False)
    get_settings.cache_clear()
    app_main._job_queue = None  # noqa: SLF001


def _session(
    client: TestClient,
    *,
    roles: tuple[str, ...] = ("user", "papaia-confirm"),
    fresh: bool = False,
) -> TestClient:
    session: dict[str, Any] = {
        "user": {
            "sub": "u-erika",
            "preferred_username": "erika",
            "roles": list(roles),
            "exp": int(time.time()) + 3600,
        },
        "_csrf_token": _CSRF,
    }
    if fresh:
        session["step_up_at"] = int(time.time())
    payload = base64.b64encode(json.dumps(session).encode())
    signed = TimestampSigner(get_settings().manager_session_secret).sign(payload).decode()
    client.cookies.clear()
    client.cookies.set("papaia_manager_session", signed)
    return client


def _form(**extra: str) -> dict[str, str]:
    return {"pk_csrf": _CSRF, **extra}


def _entries(config_dir: Path) -> list[dict[str, Any]]:
    path = config_dir / "manager" / "audit.log"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _rows(config_dir: Path) -> list[dict[str, Any]]:
    path = kit_audit.outbox_path(str(config_dir))
    if not path.exists():
        return []
    with sqlite3.connect(path) as conn:
        found = conn.execute("SELECT body FROM audit_outbox ORDER BY source_seq").fetchall()
    return [json.loads(body) for (body,) in found]


def _decisions(config_dir: Path) -> list[dict[str, Any]]:
    return [e for e in _entries(config_dir) if e["action"].startswith("person_request.")]


# -- the list --------------------------------------------------------------------------------


def test_the_page_lists_request_id_requester_and_scope(
    client: TestClient, audit: FakeAudit, config_dir: Path
) -> None:
    response = _session(client).get("/person-requests")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.text
    for request_id in (_EXPORT, _ERASE, _OWN, _DONE):
        assert request_id in body
    assert "P-DORAAAAA" in body and "P-ANNAAAAA" in body
    assert "Information" in body and "Erasure" in body
    assert "with hand-over of owned objects" in body
    # Reading writes nothing.
    assert _decisions(config_dir) == []
    method, url, headers = audit.calls[0]
    assert (method, url) == ("GET", f"{_BASE}/v1/person-requests")
    # The person's own token, exchanged for papaia-audit, and the request's correlation id.
    assert headers["Authorization"] == "Bearer aud-person-token"
    assert headers[CORRELATION_HEADER]


def test_without_a_fresh_sign_in_the_page_offers_it_and_no_confirm_button(
    client: TestClient,
) -> None:
    body = _session(client).get("/person-requests").text
    assert "/auth/step-up?next=/person-requests" in body
    assert f'action="/person-requests/{_EXPORT}/confirm"' not in body
    assert f'action="/person-requests/{_EXPORT}/reject"' in body


def test_after_a_fresh_sign_in_the_page_offers_confirm_but_not_for_an_own_request(
    client: TestClient,
) -> None:
    body = _session(client, fresh=True).get("/person-requests").text
    assert f'action="/person-requests/{_EXPORT}/confirm"' in body
    assert f'action="/person-requests/{_OWN}/confirm"' not in body
    assert "Waiting for the second person" in body
    assert f'action="/person-requests/{_DONE}/reject"' not in body


def test_the_page_carries_no_inline_script_or_style(client: TestClient) -> None:
    body = _session(client, fresh=True).get("/person-requests").text
    main = body.split("<main", 1)[1]
    assert " onclick=" not in main and " onsubmit=" not in main
    assert " style=" not in main


def test_an_empty_list_says_so(client: TestClient, audit: FakeAudit) -> None:
    audit.items.clear()
    body = _session(client).get("/person-requests").text
    assert "No request for information or erasure names you." in body


def test_an_answer_of_another_shape_shows_the_service_as_unavailable(
    client: TestClient, audit: FakeAudit
) -> None:
    audit.list_body = json.dumps(
        {"requests": [{**_view(_EXPORT, "export"), "name": "Anna Berger"}]}
    ).encode()
    response = _session(client).get("/person-requests")
    assert response.status_code == 503
    assert "Anna Berger" not in response.text
    assert "The service that keeps the requests does not answer." in response.text


# -- who may ---------------------------------------------------------------------------------


@pytest.mark.parametrize("roles", [("user",), ("user", "papaia-auditor"), ("admin",)])
def test_without_the_confirmation_role_nothing_is_shown_or_decided(
    client: TestClient, audit: FakeAudit, roles: tuple[str, ...]
) -> None:
    session = _session(client, roles=roles, fresh=True)
    assert session.get("/person-requests").status_code == 403
    assert session.post(f"/person-requests/{_EXPORT}/confirm", data=_form()).status_code == 403
    assert session.post(f"/person-requests/{_EXPORT}/reject", data=_form()).status_code == 403
    assert audit.calls == []


def test_the_platform_administration_role_may_decide(client: TestClient) -> None:
    assert _session(client, roles=("papaia-admin",)).get("/person-requests").status_code == 200


def test_the_confirmation_role_signs_in_and_sees_the_navigation_row(
    client: TestClient,
) -> None:
    settings = get_settings()
    claims = oidc.OIDCClaims(sub="u", preferred_username="u", roles=["papaia-confirm"], exp=0)
    assert is_confirmer(claims, settings) and has_manager_access(claims, settings)
    body = _session(client, roles=("papaia-confirm",)).get("/person-requests").text
    assert 'href="/person-requests"' in body
    other = _session(client, roles=("user",)).get("/").text
    assert 'href="/person-requests"' not in other


def test_a_confirming_person_may_ask_for_a_fresh_sign_in(client: TestClient) -> None:
    response = _session(client, roles=("papaia-confirm",)).get(
        "/auth/step-up?next=/person-requests"
    )
    assert response.status_code == 302
    assert "prompt=login" in response.headers["location"]
    plain = _session(client, roles=("user",)).get("/auth/step-up?next=/person-requests")
    assert plain.status_code == 403


# -- confirming ------------------------------------------------------------------------------


def test_the_requester_cannot_confirm(
    client: TestClient, audit: FakeAudit, config_dir: Path
) -> None:
    response = _session(client, fresh=True).post(
        f"/person-requests/{_OWN}/confirm", data=_form()
    )
    assert response.status_code == 403
    assert "You filed this request yourself and cannot confirm it." in response.text
    # Refused from papaia-audit's own answer: no decision was sent.
    assert audit.decisions() == []
    assert audit.items[_OWN]["state"] == "open"
    entries = _decisions(config_dir)
    assert [(e["action"], e["target"], e["result"]) for e in entries] == [
        ("person_request.confirm", _OWN, "denied")
    ]
    rows = [r for r in _rows(config_dir) if r["operation"] == "person_request.confirm"]
    assert [(r["result"], r["object_ref"]) for r in rows] == [
        ("denied", {"type": "person_request", "id": _OWN})
    ]


def test_papaia_audit_refusing_an_own_request_is_shown_and_recorded(
    client: TestClient, audit: FakeAudit, config_dir: Path
) -> None:
    audit.refuse = (403, {"code": "AUDIT-PERSON-REQ-SELF", "message": "x"})
    response = _session(client, fresh=True).post(
        f"/person-requests/{_EXPORT}/confirm", data=_form()
    )
    assert response.status_code == 403
    assert "You filed this request yourself" in response.text
    [entry] = _decisions(config_dir)
    assert entry["result"] == "denied"
    assert entry["params"] == {"operation": "export", "code": "AUDIT-PERSON-REQ-SELF"}


def test_confirming_needs_a_fresh_sign_in(
    client: TestClient, audit: FakeAudit, config_dir: Path
) -> None:
    response = _session(client).post(f"/person-requests/{_EXPORT}/confirm", data=_form())
    assert response.status_code == 403
    assert "/auth/step-up?next=/person-requests" in response.text
    assert audit.calls == []
    [entry] = _decisions(config_dir)
    assert (entry["action"], entry["result"]) == ("person_request.confirm", "refused")


def test_a_sign_in_papaia_audit_finds_too_old_is_shown(
    client: TestClient, audit: FakeAudit, config_dir: Path
) -> None:
    audit.refuse = (401, {"code": "AUDIT-AUTH-FRESH"})
    response = _session(client, fresh=True).post(
        f"/person-requests/{_EXPORT}/confirm", data=_form()
    )
    assert response.status_code == 403
    assert "at most five minutes ago" in response.text
    assert _decisions(config_dir)[0]["result"] == "refused"


def test_the_passphrase_is_shown_once(
    client: TestClient, audit: FakeAudit, config_dir: Path
) -> None:
    session = _session(client, fresh=True)
    response = session.post(f"/person-requests/{_EXPORT}/confirm", data=_form())
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.text.count(_PASSPHRASE) == 1
    assert "cannot be shown again" in response.text
    assert audit.decisions() == [f"{_BASE}/v1/person-requests/{_EXPORT}/confirm"]
    # Not on the next page, and the same form again is refused: the request is decided.
    assert _PASSPHRASE not in session.get("/person-requests").text
    again = session.post(f"/person-requests/{_EXPORT}/confirm", data=_form())
    assert again.status_code == 409
    assert _PASSPHRASE not in again.text
    # Never written anywhere.
    log = (config_dir / "manager" / "audit.log").read_text(encoding="utf-8")
    assert _PASSPHRASE not in log
    assert _PASSPHRASE.encode() not in kit_audit.outbox_path(str(config_dir)).read_bytes()


def test_one_audit_row_per_confirmation(
    client: TestClient, audit: FakeAudit, config_dir: Path
) -> None:
    response = _session(client, fresh=True).post(
        f"/person-requests/{_EXPORT}/confirm", data=_form()
    )
    assert response.status_code == 200
    entries = _decisions(config_dir)
    assert [(e["action"], e["target"], e["result"], e["user"]) for e in entries] == [
        ("person_request.confirm", _EXPORT, "ok", "erika")
    ]
    assert entries[0]["params"] == {"operation": "export"}
    rows = [r for r in _rows(config_dir) if r["operation"] == "person_request.confirm"]
    assert len(rows) == 1
    row = rows[0]
    validate_event(row)
    assert row["result"] == "allowed"
    assert row["sub"] == "u-erika"
    assert row["object_ref"] == {"type": "person_request", "id": _EXPORT}
    # papaia-audit's own event and this row share the correlation id.
    decision_headers = [h for m, _, h in audit.calls if m == "POST"][0]
    assert row["correlation_id"] == decision_headers[CORRELATION_HEADER]


def test_confirming_an_erasure_shows_no_passphrase(
    client: TestClient, audit: FakeAudit, config_dir: Path
) -> None:
    response = _session(client, fresh=True).post(
        f"/person-requests/{_ERASE}/confirm", data=_form()
    )
    assert response.status_code == 200
    assert "Passphrase of the archive" not in response.text
    assert "is confirmed" in response.text
    assert audit.items[_ERASE]["state"] == "confirmed"
    assert len(_decisions(config_dir)) == 1


def test_a_passphrase_where_none_belongs_is_not_shown(
    client: TestClient, audit: FakeAudit
) -> None:
    def send(method: str, url: str, headers: Mapping[str, str]) -> tuple[int, bytes]:
        status, body = audit.send(method, url, headers)
        if method == "POST" and status == 200:
            answer = json.loads(body)
            answer["passphrase"] = _PASSPHRASE
            body = json.dumps(answer).encode()
        return status, body

    pr.use(pr.Wiring(base_url=_BASE, send=send, exchange=lambda token, cid: token))
    response = _session(client, fresh=True).post(
        f"/person-requests/{_ERASE}/confirm", data=_form()
    )
    assert response.status_code == 503
    assert _PASSPHRASE not in response.text


# -- rejecting -------------------------------------------------------------------------------


def test_rejecting_needs_no_fresh_sign_in_and_is_recorded(
    client: TestClient, audit: FakeAudit, config_dir: Path
) -> None:
    response = _session(client).post(f"/person-requests/{_OWN}/reject", data=_form())
    assert response.status_code == 200
    assert "is rejected" in response.text
    assert audit.items[_OWN]["state"] == "rejected"
    [entry] = _decisions(config_dir)
    assert (entry["action"], entry["target"], entry["result"]) == (
        "person_request.reject", _OWN, "ok"
    )


# -- the boundary ----------------------------------------------------------------------------


def test_a_form_with_another_field_is_refused(client: TestClient, audit: FakeAudit) -> None:
    response = _session(client, fresh=True).post(
        f"/person-requests/{_EXPORT}/confirm", data=_form(person="P-ANNAAAAA")
    )
    assert response.status_code == 422
    assert audit.calls == []


def test_a_form_without_the_csrf_token_is_refused(client: TestClient, audit: FakeAudit) -> None:
    session = _session(client, fresh=True)
    assert session.post(f"/person-requests/{_EXPORT}/confirm", data={}).status_code == 403
    wrong = session.post(f"/person-requests/{_EXPORT}/confirm", data={"pk_csrf": "other"})
    assert wrong.status_code == 403
    assert audit.calls == []


@pytest.mark.parametrize("request_id", ["not-a-request", _EXPORT.upper(), "0" * 36])
def test_a_malformed_request_id_is_not_found(
    client: TestClient, audit: FakeAudit, request_id: str
) -> None:
    response = _session(client, fresh=True).post(
        f"/person-requests/{request_id}/confirm", data=_form()
    )
    assert response.status_code == 404
    assert audit.calls == []


def test_a_request_that_does_not_name_the_person_is_not_found(
    client: TestClient, audit: FakeAudit, config_dir: Path
) -> None:
    unknown = "0b5c4a1e-5555-4aaa-8bbb-000000000005"
    response = _session(client, fresh=True).post(
        f"/person-requests/{unknown}/confirm", data=_form()
    )
    assert response.status_code == 404
    assert audit.decisions() == []
    assert _decisions(config_dir)[0]["result"] == "not_found"


def test_an_unreachable_audit_service_decides_nothing(
    client: TestClient, config_dir: Path
) -> None:
    def down(method: str, url: str, headers: Mapping[str, str]) -> tuple[int, bytes]:
        raise OSError("connection refused")

    pr.use(pr.Wiring(base_url=_BASE, send=down, exchange=lambda token, cid: token))
    response = _session(client, fresh=True).post(
        f"/person-requests/{_EXPORT}/confirm", data=_form()
    )
    assert response.status_code == 503
    assert _decisions(config_dir)[0]["result"] == "unavailable"


# -- the client of papaia-audit --------------------------------------------------------------


def test_the_list_accepts_only_its_own_shape() -> None:
    good = json.dumps({"requests": [_view(_EXPORT, "export")]}).encode()
    assert [r.id for r in pr.parse_list(good)] == [_EXPORT]
    for bad in (
        {"requests": [_view(_EXPORT, "export")], "more": 1},
        {"requests": [_view(_EXPORT, "unknown")]},
        {"requests": [_view(_EXPORT, "export", person="anna@example.org")]},
        {"requests": [_view("1", "export")]},
        {"requests": [_view(_EXPORT, "export", own="yes")]},
        {"requests": [_view(_EXPORT, "export")] * (pr.LIST_MAX + 1)},
    ):
        with pytest.raises(pr.PersonRequestError):
            pr.parse_list(json.dumps(bad).encode())


def test_a_decision_answers_a_passphrase_only_for_a_confirmed_export() -> None:
    confirmed = _view(_EXPORT, "export", state="confirmed", can_confirm=False)
    decided = pr.parse_decided(
        json.dumps({**confirmed, "passphrase": _PASSPHRASE}).encode(), "confirm", _EXPORT
    )
    assert decided.passphrase == _PASSPHRASE
    for body, decision, request_id in (
        (confirmed, "confirm", _EXPORT),
        ({**confirmed, "passphrase": "short"}, "confirm", _EXPORT),
        ({**confirmed, "passphrase": _PASSPHRASE}, "confirm", _ERASE),
        ({**confirmed, "passphrase": _PASSPHRASE}, "reject", _EXPORT),
    ):
        with pytest.raises(pr.PersonRequestError):
            pr.parse_decided(json.dumps(body).encode(), decision, request_id)  # type: ignore[arg-type]


def test_the_read_api_is_reached_over_https_only() -> None:
    base = get_settings().model_dump()
    for bad in ("http://papaia-audit:8443", "https://papaia-audit:8443/v1", "https://u@h"):
        with pytest.raises(ValueError, match="AUDIT_READ_URL"):
            Settings(**{**base, "audit_read_url": bad})
    assert Settings(**{**base, "audit_read_url": "https://a:8443/"}).audit_read_url == (
        "https://a:8443"
    )
