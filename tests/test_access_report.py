"""The permission report for auditors (OPS-19, PLT-60): page `/access-report` and download.

papaia-audit builds the report for the auditor's own token, exchanged for exactly the
audience `papaia-audit`; the manager shows the answer and hands out its bytes unchanged.
Without `papaia-auditor` nothing is exchanged and nothing is asked. The stand-ins are the
realm's token endpoint and papaia-audit's build route (tests/fake_audit.py).
"""
from __future__ import annotations

import base64
import json
import logging
import os
import sqlite3
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

for _key, _value in {
    "OIDC_ISSUER_KC_AUTH": "https://kc.test/auth",
    "OIDC_ISSUER_KC_TOKEN": "https://kc.test/realms/papaia/protocol/openid-connect/token",
    "OIDC_ISSUER_KC_CERTS": "https://kc.test/certs",
    "MANAGER_ADMIN_ROLE": "admin",
    "MANAGER_USER_ROLE": "user",
    "MANAGER_HOST": "http://localhost:8120",
    "MANAGER_OIDC_CLIENT_SECRET": "client-secret",
    "MANAGER_SESSION_SECRET": "test-session-secret-value",
    "PAPAIA_CONFIG_DIR": tempfile.mkdtemp(prefix="papaia-access-report-config-"),
    "PAPAIA_WORKSPACE_DIR": tempfile.mkdtemp(prefix="papaia-access-report-workspace-"),
}.items():
    os.environ.setdefault(_key, _value)

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fake_audit  # noqa: E402
import jsonschema  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402
from fastapi import Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from itsdangerous import TimestampSigner  # noqa: E402
from papaia_kit.audit import validate_event  # noqa: E402
from papaia_kit.exchange import ClientSecret, ExchangeClient  # noqa: E402

from app.auth.user_token import UserTokenUnavailable  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.core import access_report, kit_audit  # noqa: E402
from app.main import create_app  # noqa: E402
from app.routers import ui_access_report  # noqa: E402

_CSRF = "test-csrf-token-value"
_PAGE = "/access-report"
_DOWNLOAD = "/access-report/download"


class World:
    """The stand-ins of one test and what the page asked of them."""

    def __init__(self) -> None:
        self.issuer = fake_audit.FakeIssuer()
        self.audit = fake_audit.FakeAudit()
        self.subject_calls = 0
        self.no_session = False
        settings = get_settings()
        self.exchange = ExchangeClient(
            token_endpoint=settings.oidc_issuer_kc_token,
            client_id=fake_audit.CLIENT_ID,
            auth=ClientSecret("client-secret"),
            transport=self.issuer,
        )
        self.service = access_report.ReportService(
            exchange=self.exchange, post=self.audit.post, base_url="https://papaia-audit:8443"
        )

    async def subject(self, request: Request) -> str:
        self.subject_calls += 1
        if self.no_session:
            raise UserTokenUnavailable("no refresh token")
        return fake_audit.session_token(fake_audit.DORA)

    def asked(self) -> bool:
        return bool(self.subject_calls or self.issuer.forms or self.audit.calls)


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "config"
    (directory / "manager").mkdir(parents=True)
    return directory


@pytest.fixture
def world() -> World:
    return World()


@pytest.fixture
def client(config_dir: Path, world: World) -> Iterator[TestClient]:
    get_settings.cache_clear()
    settings = get_settings().model_copy(
        update={"papaia_config_dir": str(config_dir), "manager_auditor_role": "papaia-auditor"}
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[ui_access_report.get_report_service] = lambda: world.service
    app.dependency_overrides[ui_access_report.get_subject_token] = lambda: world.subject
    yield TestClient(app, follow_redirects=False)
    get_settings.cache_clear()


def _as(client: TestClient, *roles: str, language: str = "de") -> TestClient:
    session: dict[str, Any] = {
        "user": {
            "sub": fake_audit.DORA,
            "preferred_username": "dora",
            "roles": list(roles),
            "exp": int(time.time()) + 3600,
        },
        "_csrf_token": _CSRF,
    }
    payload = base64.b64encode(json.dumps(session).encode())
    signed = TimestampSigner(get_settings().manager_session_secret).sign(payload).decode()
    client.cookies.clear()
    client.cookies.set("papaia_manager_session", signed)
    client.headers["Accept-Language"] = language
    return client


def _auditor(client: TestClient, language: str = "de") -> TestClient:
    return _as(client, "papaia-auditor", language=language)


def _outbox(config_dir: Path) -> list[dict[str, Any]]:
    path = kit_audit.outbox_path(str(config_dir))
    if not path.exists():
        return []
    with sqlite3.connect(path) as conn:
        found = conn.execute("SELECT body FROM audit_outbox ORDER BY source_seq").fetchall()
    return [json.loads(body) for (body,) in found]


def _rows(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    return [str(tr["data-key"]) for tr in soup.select("tr[data-key]")]


# -- rights --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "roles", [("papaia-admin",), ("papaia-admin", "admin"), ("admin",), ("user",), ()]
)
@pytest.mark.parametrize(
    "path", [_PAGE, f"{_PAGE}?by=system", f"{_DOWNLOAD}?by=person&format=csv",
             f"{_DOWNLOAD}?by=system&format=json"]
)  # fmt: skip
def test_without_the_auditor_role_nothing_is_exchanged_or_asked(
    client: TestClient, world: World, roles: tuple[str, ...], path: str
) -> None:
    response = _as(client, *roles).get(path)
    assert response.status_code == 403
    assert not world.asked()


def test_a_refusal_by_the_manager_is_one_audit_event(
    client: TestClient, world: World, config_dir: Path
) -> None:
    assert _as(client, "papaia-admin").get(f"{_DOWNLOAD}?by=person&format=csv").status_code == 403
    assert _as(client, "papaia-admin").get(_PAGE).status_code == 403
    rows = _outbox(config_dir)
    assert len(rows) == 2
    for row in rows:
        validate_event(row)
        assert row["event"] == "inventory_exported"
        assert row["result"] == "denied"
        assert row["sub"] == fake_audit.DORA
        assert row["correlation_id"]


def test_without_a_session_the_page_asks_for_sign_in(client: TestClient) -> None:
    client.cookies.clear()
    response = client.get(_PAGE)
    assert response.status_code == 307
    assert response.headers["location"].startswith("/auth/login")


def test_an_ended_sign_in_asks_for_sign_in_and_exchanges_nothing(
    client: TestClient, world: World
) -> None:
    world.no_session = True
    response = _auditor(client).get(_PAGE)
    assert response.status_code == 307
    assert response.headers["location"].startswith("/auth/login")
    assert world.issuer.forms == [] and world.audit.calls == []


def test_the_auditor_sees_the_entry_in_the_review_group(client: TestClient) -> None:
    page = _auditor(client).get("/")
    assert 'href="/access-report"' in page.text
    assert 'href="/access-report"' not in _as(client, "admin", "papaia-admin").get("/").text


# -- exchange ------------------------------------------------------------------------------


def test_the_exchange_asks_for_exactly_the_audience_papaia_audit(
    client: TestClient, world: World
) -> None:
    assert _auditor(client).get(_PAGE).status_code == 200
    [form] = world.issuer.forms
    assert form["grant_type"] == "urn:ietf:params:oauth:grant-type:token-exchange"
    assert form["audience"] == "papaia-audit"
    assert form["scope"] == "aud-papaia-audit"
    assert [k for k in form if "audience" in k or k == "resource"] == ["audience"]
    subject = json.loads(base64.urlsafe_b64decode(form["subject_token"].split(".")[1] + "=="))
    assert subject["sub"] == fake_audit.DORA and subject["azp"] == "papaia-manager"
    [call] = world.audit.calls
    assert call["url"] == "https://papaia-audit:8443/v1/access-reports/build"
    assert call["body"] == {"by": "person", "format": "json"}
    assert call["headers"]["Authorization"] == f"Bearer {world.issuer.issued[0]}"


def test_the_correlation_id_goes_along(client: TestClient, world: World) -> None:
    cid = "3f1d2c4b-5a69-4e7d-8c1b-2a3f4e5d6c7b"
    response = _auditor(client).get(_PAGE, headers={"X-Papaia-Correlation-Id": cid})
    assert response.status_code == 200
    assert world.audit.calls[0]["headers"]["X-Papaia-Correlation-Id"] == cid
    assert world.issuer.forms and response.headers.get("X-Papaia-Correlation-Id") == cid


def test_the_exchanged_token_lives_for_one_request_only(client: TestClient, world: World) -> None:
    _auditor(client)
    assert client.get(_PAGE).status_code == 200
    assert client.get(_PAGE).status_code == 200
    # Nothing kept the first token: the second request exchanged again.
    assert len(world.issuer.forms) == 2
    assert world.issuer.issued[0] != world.issuer.issued[1]
    tokens = [c["headers"]["Authorization"] for c in world.audit.calls]
    assert tokens == [f"Bearer {t}" for t in world.issuer.issued]


def test_no_token_value_reaches_log_page_or_cookie(
    client: TestClient, world: World, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    _auditor(client)
    responses = [
        client.get(_PAGE),
        client.get(f"{_PAGE}?by=system&due=1"),
        client.get(f"{_DOWNLOAD}?by=person&format=json"),
    ]
    world.audit.mode = "refuse"
    responses.append(client.get(_PAGE))
    world.audit.mode = "down"
    responses.append(client.get(_PAGE))
    secrets = [*world.issuer.issued, fake_audit.session_token(fake_audit.DORA)]
    assert len(world.issuer.issued) == 5
    shown = "\n".join(
        [r.text for r in responses]
        + [v for r in responses for v in r.headers.get_list("set-cookie")]
        + [record.getMessage() for record in caplog.records]
    )
    for token in secrets:
        assert token not in shown
        # Not even one part of it (header, claims, filler signature).
        for part in token.split(".")[1:]:
            assert part not in shown
    assert "exchanged-token-value" not in shown
    assert "subject-token-value" not in shown


# -- the page ------------------------------------------------------------------------------


def test_rows_by_person_are_shown_with_marks(client: TestClient, world: World) -> None:
    response = _auditor(client).get(_PAGE)
    assert response.status_code == 200
    doc = world.audit.docs["person"]
    assert _rows(response.text) == [row["key"] for row in doc["rows"]]
    soup = BeautifulSoup(response.text, "html.parser")
    anna = soup.select_one(f'tr[data-key="person:{fake_audit.ANNA}"]').get_text(" ")
    assert "Funktionstrennung" in anna and "papaia-admin, papaia-auditor" in anna
    assert "svc-flow-paperless" in anna and "leitung" in anna
    glass = soup.select_one(f'tr[data-key="person:{fake_audit.BREAKGLASS}"]').get_text(" ")
    assert "Notfallkonto" in glass and "Deaktiviert" in glass
    ben = soup.select_one(f'tr[data-key="person:{fake_audit.BEN}"]').get_text(" ")
    assert "flow-engine" in ben and "papaia-studio" in ben and "builder" in ben
    assert "Vertretung" in ben
    assert doc["checksum"] in response.text
    # One retrieval, the JSON form of the kind shown.
    assert [c["body"] for c in world.audit.calls] == [{"by": "person", "format": "json"}]


def test_rows_by_system_are_shown(client: TestClient, world: World) -> None:
    response = _auditor(client).get(f"{_PAGE}?by=system")
    assert response.status_code == 200
    doc = world.audit.docs["system"]
    assert _rows(response.text) == ["system:mcp-paperless", "system:papaia-audit"]
    soup = BeautifulSoup(response.text, "html.parser")
    row = soup.select_one('tr[data-key="system:mcp-paperless"]').get_text(" ")
    assert "flow-engine" in row and "lesen" in row and "schreiben" in row
    assert "ops-anna" in row and "ops-dora" in row
    assert "svc-flow-paperless" in row
    assert "im Zielsystem geführt" in row
    assert doc["checksum"] in response.text
    assert world.audit.calls[0]["body"] == {"by": "system", "format": "json"}


def test_every_row_shows_its_confirmation_and_due_rows_are_marked(
    client: TestClient,
) -> None:
    soup = BeautifulSoup(_auditor(client).get(_PAGE).text, "html.parser")
    dora = soup.select_one(f'tr[data-key="person:{fake_audit.DORA}"]').get_text(" ")
    assert "2026-11-02T09:30:00Z" in dora and "Bestätigung fällig" not in dora
    ben = soup.select_one(f'tr[data-key="person:{fake_audit.BEN}"]').get_text(" ")
    assert "2025-01-10T08:00:00Z" in ben and "Bestätigung fällig" in ben
    anna = soup.select_one(f'tr[data-key="person:{fake_audit.ANNA}"]').get_text(" ")
    assert "nie" in anna and "Bestätigung fällig" in anna


@pytest.mark.parametrize("by", ["person", "system"])
def test_the_due_filter_shows_due_rows_only(client: TestClient, world: World, by: str) -> None:
    response = _auditor(client).get(f"{_PAGE}?by={by}&due=1")
    assert response.status_code == 200
    due = [r["key"] for r in world.audit.docs[by]["rows"] if r["confirmation_due"]]
    assert due and _rows(response.text) == due
    assert len(due) < len(world.audit.docs[by]["rows"])
    # The switch keeps the filter; the filter keeps the kind.
    soup = BeautifulSoup(response.text, "html.parser")
    assert soup.select_one('input[name="by"]')["value"] == by
    assert soup.select_one('input[name="due"]').has_attr("checked")


def test_no_due_row_is_an_empty_state(client: TestClient, world: World) -> None:
    rows = [r for r in fake_audit.person_rows() if r["sub"] == fake_audit.DORA]
    world.audit.docs["person"] = fake_audit.report("person", rows)
    response = _auditor(client).get(f"{_PAGE}?due=1")
    assert response.status_code == 200
    assert 'id="access-report-no-match"' in response.text and _rows(response.text) == []


@pytest.mark.parametrize(
    "query",
    ["by=group", "by=person&by=system", "due=yes", "format=csv", "by=person&extra=1"],
)
def test_unknown_or_invalid_parameters_are_refused(
    client: TestClient, world: World, query: str
) -> None:
    assert _auditor(client).get(f"{_PAGE}?{query}").status_code == 400
    assert world.audit.calls == []


def test_the_page_uses_the_kit_and_no_inline_script_or_style(client: TestClient) -> None:
    soup = BeautifulSoup(_auditor(client).get(_PAGE).text, "html.parser")
    main = soup.find("main", id="pk-main")
    assert main is not None
    assert main.select_one("table.pk-table") is not None
    # The frame's modules come from files and its data islands are JSON: no inline script.
    inline = [
        s for s in main.find_all("script")
        if not s.get("src") and s.get("type") != "application/json"
    ]  # fmt: skip
    assert inline == []
    for element in main.find_all(True):
        assert "style" not in element.attrs
        assert not [a for a in element.attrs if a.startswith("on")]


def test_the_page_speaks_english_too(client: TestClient) -> None:
    text = _auditor(client, language="en").get(_PAGE).text
    assert "Permission report" in text and "Confirmation due" in text


# -- download ------------------------------------------------------------------------------


@pytest.mark.parametrize("by", ["person", "system"])
@pytest.mark.parametrize("fmt", ["csv", "json"])
def test_the_download_is_the_answer_byte_for_byte(
    client: TestClient, world: World, config_dir: Path, by: str, fmt: str
) -> None:
    response = _auditor(client).get(f"{_DOWNLOAD}?by={by}&format={fmt}")
    assert response.status_code == 200
    assert response.content == world.audit.answer(by, fmt)
    assert response.headers["content-type"].startswith(access_report.MEDIA_TYPES[fmt])
    assert response.headers["cache-control"] == "no-store"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith(f'attachment; filename="access-report-{by}-')
    assert disposition.endswith(f'.{fmt}"')
    assert world.audit.calls[-1]["body"] == {"by": by, "format": fmt}
    # papaia-audit records the retrieval; the manager writes no second row.
    assert _outbox(config_dir) == []


def test_the_page_shows_the_checksum_of_the_downloaded_report(
    client: TestClient, world: World
) -> None:
    _auditor(client)
    page = client.get(_PAGE)
    data = json.loads(client.get(f"{_DOWNLOAD}?by=person&format=json").content)
    soup = BeautifulSoup(page.text, "html.parser")
    assert data["checksum"] in soup.select_one("#access-report-checksum").get_text(" ")
    # Two retrievals of an unchanged realm: the same checksum (PLT-60-A3).
    again = BeautifulSoup(client.get(_PAGE).text, "html.parser")
    assert data["checksum"] in again.select_one("#access-report-checksum").get_text(" ")


def test_the_page_links_both_downloads_of_the_kind_shown(client: TestClient) -> None:
    text = _auditor(client).get(f"{_PAGE}?by=system").text
    assert 'href="/access-report/download?by=system&amp;format=csv"' in text
    assert 'href="/access-report/download?by=system&amp;format=json"' in text


@pytest.mark.parametrize(
    "query", ["by=person", "format=csv", "by=person&format=xml", "by=x&format=csv",
              "by=person&format=csv&format=json", "by=person&format=csv&due=1"]
)  # fmt: skip
def test_the_download_refuses_invalid_parameters(
    client: TestClient, world: World, query: str
) -> None:
    assert _auditor(client).get(f"{_DOWNLOAD}?{query}").status_code == 400
    assert world.audit.calls == []


# -- refusals and failures -----------------------------------------------------------------


def test_a_refusal_of_papaia_audit_shows_its_message_and_hint(
    client: TestClient, world: World
) -> None:
    world.audit.mode = "refuse"
    response = _auditor(client).get(_PAGE)
    assert response.status_code == 403
    assert fake_audit.FakeAudit.PROBLEM_403["message"] in response.text
    assert "Bitten Sie die Verwaltung um die Rolle" in response.text
    assert 'id="access-report-refused"' in response.text
    assert _rows(response.text) == [] and "access-report-download" not in response.text
    download = client.get(f"{_DOWNLOAD}?by=person&format=csv")
    assert download.status_code == 403
    assert fake_audit.FakeAudit.PROBLEM_403["message"] in download.text


@pytest.mark.parametrize("mode", ["exchange", "down", "broken", "unavailable"])
def test_a_failure_shows_the_error_state_without_partial_data(
    client: TestClient, world: World, mode: str
) -> None:
    if mode == "exchange":
        world.issuer.mode = "refuse"
    else:
        world.audit.mode = mode
    response = _auditor(client).get(_PAGE)
    assert response.status_code == 502
    assert 'id="access-report-failed"' in response.text
    assert _rows(response.text) == []
    assert "access-report-download" not in response.text
    for row in world.audit.docs["person"]["rows"]:
        assert row["sub"] not in response.text
    assert world.audit.docs["person"]["checksum"] not in response.text
    if mode == "exchange":
        assert world.audit.calls == []
    if mode == "unavailable":
        assert "papaia-ctl realm sync" in response.text
    download = client.get(f"{_DOWNLOAD}?by=person&format=json")
    assert download.status_code == 502


def test_an_unreachable_issuer_is_the_error_state(client: TestClient, world: World) -> None:
    world.issuer.mode = "down"
    response = _auditor(client).get(_PAGE)
    assert response.status_code == 502
    assert 'id="access-report-failed"' in response.text
    assert world.audit.calls == []


# -- the answer at the boundary ------------------------------------------------------------


def _service(answer: tuple[int, str, bytes]) -> access_report.ReportService:
    world = World()
    return access_report.ReportService(
        exchange=world.exchange, post=lambda *_: answer, base_url="https://papaia-audit:8443"
    )


def _build(answer: tuple[int, str, bytes], by: str = "person", fmt: str = "json") -> Any:
    subject = fake_audit.session_token(fake_audit.DORA)
    return _service(answer).build(subject, by, fmt, "3f1d2c4b-5a69-4e7d-8c1b-2a3f4e5d6c7b")


def test_the_stand_in_reports_follow_the_published_schema() -> None:
    for by in ("person", "system"):
        jsonschema.validate(fake_audit.report(by), access_report.schema())


def _tampered(change: Any) -> bytes:
    doc = fake_audit.report("person")
    change(doc)
    return fake_audit.render(doc, "json")


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        fake_audit.render(fake_audit.report("system"), "json"),  # the other kind
        _tampered(lambda d: d.update(checksum="sha256:" + "1" * 64)),
        _tampered(lambda d: d["rows"][0].update(username="mallory")),  # digest no longer fits
        _tampered(lambda d: d["rows"].reverse()),
        _tampered(lambda d: d.update(extra=1)),
        _tampered(lambda d: d["rows"][0].update(password="x")),
        _tampered(lambda d: d["rows"][0].update(last_confirmed_at="yesterday")),
    ],
)
def test_an_answer_that_is_not_the_report_asked_for_is_refused(body: bytes) -> None:
    with pytest.raises(access_report.Unavailable) as failed:
        _build((200, "application/json", body))
    assert failed.value.reason == "answer"


def test_an_answer_of_another_media_type_is_refused() -> None:
    body = fake_audit.render(fake_audit.report("person"), "json")
    with pytest.raises(access_report.Unavailable):
        _build((200, "text/html", body))
    with pytest.raises(access_report.Unavailable):
        _build((200, "application/json", body), fmt="csv")


def test_a_csv_with_the_header_of_the_other_kind_is_refused() -> None:
    body = fake_audit.render(fake_audit.report("system"), "csv")
    with pytest.raises(access_report.Unavailable):
        _build((200, "text/csv", body), fmt="csv")


@pytest.mark.parametrize(
    "problem",
    [b"not json", b'{"message": "x"}', json.dumps({"message": "a" * 2000, "hint": "b"}).encode(),
     json.dumps({"message": "a\nb", "hint": "c"}).encode()],
)  # fmt: skip
def test_a_403_without_a_well_formed_problem_is_the_error_state(problem: bytes) -> None:
    with pytest.raises(access_report.Unavailable):
        _build((403, "application/problem+json", problem))


def test_a_403_carries_message_hint_and_code() -> None:
    body = json.dumps(fake_audit.FakeAudit.PROBLEM_403).encode()
    with pytest.raises(access_report.Refused) as refused:
        _build((403, "application/problem+json", body))
    assert refused.value.code == "AUDIT-REPORT-403"
    assert refused.value.message.startswith("Der Berechtigungsbericht")
    assert refused.value.hint.startswith("Bitten Sie")


def test_an_oversized_answer_is_cut_off() -> None:
    chunks = iter([b"x" * 1024] * 4)
    clock = iter([0.0, 1.0, 2.0, 3.0, 4.0, 5.0])

    class _Response:
        status_code = 200
        headers = {"content-type": "application/json"}

        def iter_bytes(self) -> Iterator[bytes]:
            return chunks

        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    class _Client:
        def stream(self, *args: Any, **kwargs: Any) -> _Response:
            assert kwargs["follow_redirects"] is False
            return _Response()

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    post = access_report.post_with(_Client, deadline_seconds=2.5, clock=lambda: next(clock))
    with pytest.raises(OSError):
        post("https://papaia-audit:8443/v1/access-reports/build", b"{}", {})


# -- settings ------------------------------------------------------------------------------


def test_the_production_service_exchanges_with_the_manager_client() -> None:
    ui_access_report._service = None
    try:
        service = ui_access_report.get_report_service(get_settings())
        assert service is ui_access_report.get_report_service(get_settings())
        exchange = service._exchange
        assert isinstance(exchange, ExchangeClient)
        assert exchange.client_id == "papaia-manager"
        assert exchange.downstream == frozenset()
        # The read API of papaia-audit (AUDIT_READ_URL, the one of the data subject requests).
        assert service._url == "https://papaia-audit:8443/v1/access-reports/build"
    finally:
        ui_access_report._service = None
