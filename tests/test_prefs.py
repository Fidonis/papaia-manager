"""Appearance and language from the platform's preference service `prefs` (T-UI-02, PLT-28).

With Mein Bereich in the platform catalog, `prefs` is the one source of the language and
the appearance (interface spec 3.3, architecture D-02): the manager reads the values at sign-in
with the person's token (exchanged for the audience `papaia-prefs`), keeps them in the
session, reloads them before a page renders when the shared cookie `pk_ui` names a new etag,
and writes the footer and account-menu choices with `PUT /api/v1/preferences/me`. Neither the
token claims nor the manager's own language file is a source then.

The token endpoint and `prefs` are stand-ins that behave like the real ones: the issuer checks
the exchange request and issues a token for exactly `papaia-prefs`, `prefs` refuses any other
token and answers a stale `If-Match` with 412.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import sqlite3
import tempfile
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

_CONFIG_DIR = tempfile.mkdtemp(prefix="papaia-prefs-config-")
_WORKSPACE_DIR = tempfile.mkdtemp(prefix="papaia-prefs-workspace-")

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
from papaia_kit._resources import contract_dir  # noqa: E402
from papaia_kit.audit import validate_event  # noqa: E402
from papaia_kit.exchange import ACCESS_TOKEN_TYPE, GRANT_TYPE  # noqa: E402

from app.auth import user_token  # noqa: E402
from app.auth.oidc import OIDCClaims, OIDCClient, TokenSet, get_oidc_client  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.core import kit_audit, module_catalog, prefs, user_prefs  # noqa: E402
from app.core.audit import audit_path  # noqa: E402
from app.main import create_app  # noqa: E402

SHELL = contract_dir() / "shell"
SUB = "6f1c2a9e-0000-4000-8000-0000000000a1"
SID = "sid-anna-1"
_CSRF = "prefs-test-csrf-token-0001"
_CID = "0b8f5a52-6c1e-4c11-9b3a-0d6f8c2e7a41"


# -- stand-ins -----------------------------------------------------------------------------


def _segment(data: Mapping[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def jwt(claims: Mapping[str, Any]) -> str:
    """A compact JWS shape; neither the exchange client nor these stand-ins check signatures."""
    return f"{_segment({'alg': 'RS256', 'typ': 'JWT'})}.{_segment(claims)}.c2ln"


def claims_of(token: str) -> dict[str, Any]:
    payload = token.split(".")[1]
    data: dict[str, Any] = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    return data


def person_token(*, sub: str = SUB, azp: str = "papaia-manager") -> str:
    now = int(time.time())
    return jwt(
        {
            "sub": sub,
            "azp": azp,
            "aud": "account",
            "sid": SID,
            "auth_time": now - 60,
            "exp": now + 300,
            "iss": "https://kc.test/realms/papaia",
        }
    )


class Issuer:
    """The realm's token endpoint for RFC 8693 token exchange, as the manager's client sees it."""

    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []
        self.refuse = False

    def post_form(
        self, url: str, form: Mapping[str, str], headers: Mapping[str, str], timeout: float
    ) -> tuple[int, bytes]:
        self.calls.append(dict(form))
        expected = "Basic " + base64.b64encode(b"papaia-manager:client-secret").decode()
        if url != "https://kc.test/token" or headers.get("Authorization") != expected:
            return 401, b'{"error":"invalid_client"}'
        if self.refuse:
            return 400, b'{"error":"invalid_grant"}'
        if (
            form.get("grant_type") != GRANT_TYPE
            or form.get("audience") != "papaia-prefs"
            or form.get("scope") != "aud-papaia-prefs"
            or form.get("subject_token_type") != ACCESS_TOKEN_TYPE
        ):
            return 400, b'{"error":"invalid_request"}'
        subject = claims_of(form["subject_token"])
        issued = {
            "sub": subject["sub"],
            "sid": subject["sid"],
            "auth_time": subject["auth_time"],
            "azp": "papaia-manager",
            "aud": "papaia-prefs",
            "exp": int(time.time()) + 300,
        }
        body = {
            "access_token": jwt(issued),
            "token_type": "Bearer",
            "expires_in": 300,
            "issued_token_type": ACCESS_TOKEN_TYPE,
        }
        return 200, json.dumps(body).encode()


class Prefs:
    """`prefs` with one record per person: GET and PUT `/api/v1/preferences/me`."""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, dict[str, str], dict[str, Any] | None]] = []
        self.down = False
        self._serial = 0

    def record(self, sub: str = SUB) -> dict[str, Any]:
        if sub not in self.records:
            self.records[sub] = {
                "sub": sub,
                "locale": "browser",
                "appearance": "system",
                "time_zone": "Europe/Berlin",
                "welcome_seen": {},
                "preferred_channel": "chat_window",
                "fallback_channel": "none",
                "fallback_after": 10,
                "quiet_hours": {"enabled": False, "start": "22:00", "end": "07:00", "days": []},
                "etag": self._etag(),
                "updated_at": "2026-11-23T08:00:00Z",
            }
        return self.records[sub]

    def _etag(self) -> str:
        self._serial += 1
        return f"e{self._serial}"

    def change_elsewhere(self, sub: str = SUB, **values: Any) -> str:
        """Another module (Studio, Flow, Mein Bereich) wrote the record; returns `pk_ui`."""
        record = self.record(sub)
        record.update(values, etag=self._etag())
        return f"{record['appearance']}.{record['locale']}.{record['etag']}"

    @property
    def gets(self) -> int:
        return sum(1 for method, _, _ in self.requests if method == "GET")

    @property
    def puts(self) -> list[dict[str, Any]]:
        return [body for method, _, body in self.requests if method == "PUT" and body]

    def send(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> tuple[int, bytes]:
        if self.down:
            raise OSError("connection refused")
        decoded = json.loads(body) if body else None
        self.requests.append((method, dict(headers), decoded))
        if url != "http://prefs:8080/api/v1/preferences/me":
            return 404, b"{}"
        auth = headers.get("Authorization", "")
        claims = claims_of(auth.removeprefix("Bearer ")) if auth.startswith("Bearer ") else {}
        if claims.get("aud") != "papaia-prefs":
            return 401, b"{}"
        record = self.record(claims["sub"])
        if method == "PUT":
            if headers.get("If-Match") != f'"{record["etag"]}"':
                return 412, b"{}"
            assert decoded is not None
            record.update(decoded, etag=self._etag())
        return 200, json.dumps(record).encode()


# -- fixtures ------------------------------------------------------------------------------


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    path = tmp_path / "config"
    path.mkdir()
    return path


@pytest.fixture
def issuer() -> Issuer:
    return Issuer()


@pytest.fixture
def store() -> Prefs:
    return Prefs()


@pytest.fixture
def keycloak(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The refresh grant answers with a person token for the manager's client."""

    async def refresh(self: OIDCClient, *, refresh_token: str) -> TokenSet:
        claims = OIDCClaims(
            sub=SUB, preferred_username="anna", roles=["admin"], exp=int(time.time()) + 300
        )
        return TokenSet(
            claims=claims,
            refresh_token=refresh_token,
            access_token=person_token(),
            access_expires_at=time.time() + 300,
        )

    get_oidc_client.cache_clear()
    user_token.clear()
    monkeypatch.setattr(OIDCClient, "refresh", refresh)
    yield
    user_token.clear()
    get_oidc_client.cache_clear()


def _app(config_dir: Path, tmp_path: Path, **update: Any) -> TestClient:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    settings = get_settings().model_copy(
        update={
            "papaia_config_dir": str(config_dir),
            "papaia_workspace_dir": str(workspace),
            **update,
        }
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def wired(issuer: Issuer, store: Prefs, keycloak: None) -> Iterator[None]:
    get_settings.cache_clear()
    module_catalog.reset_readers()
    prefs.reset()
    prefs.use(
        prefs.Wiring.from_settings(
            get_settings(), issuer_transport=issuer, prefs_transport=store
        )
    )
    yield
    prefs.reset()
    get_settings.cache_clear()
    module_catalog.reset_readers()


@pytest.fixture
def client(config_dir: Path, tmp_path: Path, wired: None) -> TestClient:
    _install_catalog(config_dir)
    return _app(config_dir, tmp_path)


def _install_catalog(config_dir: Path, name: str = "rs-anna-de-light") -> None:
    """The reference catalog: a platform with Mein Bereich (`me`), so `prefs` runs."""
    doc = json.loads((SHELL / "inputs" / f"{name}.json").read_text(encoding="utf-8"))
    platform = config_dir / "platform"
    platform.mkdir(exist_ok=True)
    (platform / "services.json").write_text(json.dumps(doc["catalog"]), encoding="utf-8")
    shutil.copy(SHELL / "inputs" / "branding" / "logo.svg", platform / "brand-logo")


def _signed(session: Mapping[str, Any]) -> str:
    payload = base64.b64encode(json.dumps(dict(session)).encode())
    return TimestampSigner(get_settings().manager_session_secret).sign(payload).decode()


def _as(client: TestClient, *roles: str, **extra: Any) -> TestClient:
    session: dict[str, Any] = {
        "user": {
            "sub": SUB,
            "preferred_username": "anna",
            "roles": list(roles) or ["admin"],
            "exp": int(time.time()) + 3600,
        },
        "oidc": {"refresh_token": "refresh-anna"},
        "_csrf_token": _CSRF,
        **extra,
    }
    client.cookies.clear()
    # The domain the test client files the server's own session cookie under, so an answer
    # that renews the session replaces this one.
    client.cookies.set("papaia_manager_session", _signed(session), domain="testserver.local")
    return client


def _session(client: TestClient) -> dict[str, Any]:
    raw = client.cookies.get("papaia_manager_session")
    assert raw is not None
    payload = TimestampSigner(get_settings().manager_session_secret).unsign(raw.encode())
    data: dict[str, Any] = json.loads(base64.b64decode(payload))
    return data


def _html(body: str) -> str:
    return body[body.index("<html") : body.index(">", body.index("<html")) + 1]


def rows(config_dir: Path) -> list[dict[str, Any]]:
    path = kit_audit.outbox_path(str(config_dir))
    if not path.exists():
        return []
    with sqlite3.connect(path) as conn:
        found = conn.execute("SELECT body FROM audit_outbox ORDER BY source_seq").fetchall()
    return [json.loads(body) for (body,) in found]


def _log(config_dir: Path) -> list[dict[str, Any]]:
    path = audit_path(str(config_dir))
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _cookie(response: Any, name: str) -> str:
    found = [v for v in response.headers.get_list("set-cookie") if v.startswith(f"{name}=")]
    assert len(found) == 1, response.headers.get_list("set-cookie")
    return str(found[0])


# -- the ticket's test ---------------------------------------------------------------------


def test_dark_preference_set_in_another_module_is_shown_without_re_login(
    client: TestClient, store: Prefs, issuer: Issuer
) -> None:
    """PLT-28-A2 with Ä-34: dark in Studio, the manager's next page is dark, no new sign-in.

    The other module writes `prefs` and sets the shared session cookie `pk_ui`; the manager
    sees a new etag, reloads from `prefs` before it renders, and the first byte of the page
    already carries `data-theme="dark"`.
    """
    store.record()["locale"] = "de"
    browser = _as(client)
    first = browser.get("/")
    assert first.status_code == 200
    assert _html(first.text) == '<html lang="de">'
    reads = store.gets

    browser.cookies.set("pk_ui", store.change_elsewhere(appearance="dark"))
    page = browser.get("/")
    assert page.status_code == 200
    assert page.text.lstrip().startswith('<!doctype html>\n<html lang="de" data-theme="dark">')
    assert store.gets == reads + 1
    assert store.records[SUB]["appearance"] == "dark"
    # Still the same session: nobody was sent to the sign-in page.
    assert _session(browser)["user"]["sub"] == SUB
    assert _session(browser)["pk_prefs"]["appearance"] == "dark"
    # The same etag again needs no second read.
    browser.get("/")
    assert store.gets == reads + 1
    # The token sent to prefs was exchanged for exactly that audience.
    assert {call["audience"] for call in issuer.calls} == {"papaia-prefs"}


# -- reading -------------------------------------------------------------------------------


def test_the_first_page_reads_prefs_when_the_session_has_no_values(
    client: TestClient, store: Prefs
) -> None:
    store.record().update(locale="en", appearance="dark")
    page = _as(client).get("/", headers={"accept-language": "de-DE,de;q=0.9"})
    assert _html(page.text) == '<html lang="en" data-theme="dark">'
    assert store.gets == 1
    assert _session(client)["pk_prefs"]["etag"] == store.records[SUB]["etag"]


def test_browser_language_follows_accept_language(client: TestClient, store: Prefs) -> None:
    store.record()["locale"] = "browser"
    english = _as(client).get("/", headers={"accept-language": "en-GB,en;q=0.9,de;q=0.5"})
    assert _html(english.text) == '<html lang="en">'
    german = client.get("/", headers={"accept-language": "fr-FR,de;q=0.7"})
    assert _html(german.text) == '<html lang="de">'
    # Nothing the platform knows: German, as in every module (kit mapping).
    other = client.get("/", headers={"accept-language": "fr-FR"})
    assert _html(other.text) == '<html lang="de">'
    marked = other.text[other.text.index('class="pk-language"') :]
    assert 'value="browser" aria-current="true"' in marked[: marked.index("</details>")]


def test_neither_the_old_session_value_nor_the_local_language_file_is_a_source(
    client: TestClient, store: Prefs, config_dir: Path
) -> None:
    store.record().update(locale="en", appearance="light")
    user_prefs.set_locale(str(config_dir), SUB, "de")
    browser = _as(client, pk_appearance="dark")
    page = browser.get("/", headers={"accept-language": "de"})
    assert _html(page.text) == '<html lang="en" data-theme="light">'
    assert page.headers["content-language"] == "en"


def test_values_in_the_session_are_used_without_a_call(client: TestClient, store: Prefs) -> None:
    record = store.record()
    record.update(locale="de", appearance="dark")
    cached = {k: record[k] for k in ("locale", "appearance", "time_zone", "welcome_seen", "etag")}
    browser = _as(client, pk_prefs=cached)
    browser.cookies.set("pk_ui", f"dark.de.{record['etag']}")
    assert _html(browser.get("/").text) == '<html lang="de" data-theme="dark">'
    assert store.gets == 0


@pytest.mark.parametrize(
    "cookie", ["dark.de", "dark.fr.e9", "blue.de.e9", "dark.de." + "x" * 200, "dark.de.e 9"]
)
def test_an_unreadable_pk_ui_is_ignored(client: TestClient, store: Prefs, cookie: str) -> None:
    record = store.record()
    cached = {k: record[k] for k in ("locale", "appearance", "time_zone", "welcome_seen", "etag")}
    browser = _as(client, pk_prefs=cached)
    browser.cookies.set("pk_ui", cookie)
    assert browser.get("/").status_code == 200
    assert store.gets == 0


def test_a_tampered_session_entry_is_read_again(client: TestClient, store: Prefs) -> None:
    store.record()["appearance"] = "dark"
    broken = {"locale": "xx", "appearance": "dark", "time_zone": "Mars/Base", "etag": "e1"}
    page = _as(client, pk_prefs=broken).get("/")
    assert _html(page.text) == '<html lang="de" data-theme="dark">'
    assert store.gets == 1


def test_an_unreachable_prefs_still_renders_and_is_not_asked_on_every_page(
    client: TestClient, store: Prefs, caplog: pytest.LogCaptureFixture
) -> None:
    store.down = True
    browser = _as(client)
    page = browser.get("/", headers={"accept-language": "en"})
    assert page.status_code == 200
    assert _html(page.text) == '<html lang="en">'
    assert "preferences not read" in caplog.text
    store.down = False
    browser.get("/")
    assert store.gets == 0  # held back for a while after the failure


def test_a_refused_exchange_renders_with_the_defaults(
    client: TestClient, issuer: Issuer, store: Prefs
) -> None:
    issuer.refuse = True
    page = _as(client).get("/", headers={"accept-language": "en"})
    assert page.status_code == 200
    assert _html(page.text) == '<html lang="en">'
    assert store.gets == 0


def test_static_files_and_kit_assets_never_ask_prefs(client: TestClient, store: Prefs) -> None:
    browser = _as(client)
    browser.cookies.set("pk_ui", "dark.de.e77")
    browser.get("/health")
    browser.get("/_kit/brand/logo")
    assert store.gets == 0


def test_sign_in_reads_the_values_into_the_session(
    client: TestClient, store: Prefs, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.routers import auth

    store.record().update(locale="en", appearance="dark")

    class _Oidc:
        def build_auth_url(self, state: str, challenge: str, *, step_up: bool = False) -> str:
            return f"https://kc.test/auth?state={state}"

        async def exchange_code(self, *, code: str, code_verifier: str) -> TokenSet:
            claims = OIDCClaims(
                sub=SUB, preferred_username="anna", roles=["admin"], exp=int(time.time()) + 300
            )
            return TokenSet(
                claims=claims,
                refresh_token="refresh-anna",
                access_token=person_token(),
                access_expires_at=time.time() + 300,
            )

    monkeypatch.setattr(auth, "get_oidc_client", lambda: _Oidc())
    client.cookies.clear()
    sent = client.get("/auth/login")
    state = parse_qs(urlsplit(sent.headers["location"]).query)["state"][0]
    back = client.get(f"/auth/callback?code=c&state={state}")
    assert back.status_code == 302
    assert store.gets == 1
    assert _session(client)["pk_prefs"]["appearance"] == "dark"
    assert _html(client.get("/").text) == '<html lang="en" data-theme="dark">'
    assert store.gets == 1


def test_sign_in_goes_on_when_prefs_is_down(
    client: TestClient, store: Prefs, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.routers import auth

    class _Oidc:
        def build_auth_url(self, state: str, challenge: str, *, step_up: bool = False) -> str:
            return f"https://kc.test/auth?state={state}"

        async def exchange_code(self, *, code: str, code_verifier: str) -> TokenSet:
            claims = OIDCClaims(
                sub=SUB, preferred_username="anna", roles=["admin"], exp=int(time.time()) + 300
            )
            return TokenSet(claims=claims, refresh_token="r", access_token=person_token())

    store.down = True
    monkeypatch.setattr(auth, "get_oidc_client", lambda: _Oidc())
    client.cookies.clear()
    state = parse_qs(urlsplit(client.get("/auth/login").headers["location"]).query)["state"][0]
    back = client.get(f"/auth/callback?code=c&state={state}")
    assert back.status_code == 302
    assert "pk_prefs" not in _session(client)


# -- writing -------------------------------------------------------------------------------


def test_the_appearance_form_writes_prefs(
    client: TestClient, store: Prefs, config_dir: Path
) -> None:
    store.record()["locale"] = "de"
    browser = _as(client)
    browser.get("/")
    etag = store.records[SUB]["etag"]
    response = browser.post(
        "/_kit/appearance",
        data={"appearance": "dark", "pk_csrf": _CSRF},
        headers={"referer": "http://testserver/host", "X-Papaia-Correlation-Id": _CID},
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/host"
    assert store.puts == [{"appearance": "dark"}]
    put_headers = next(h for m, h, _ in store.requests if m == "PUT")
    assert put_headers["If-Match"] == f'"{etag}"'
    assert put_headers["X-Papaia-Correlation-Id"] == _CID
    new = store.records[SUB]
    cookie = _cookie(response, "pk_ui")
    assert cookie == f"pk_ui=dark.de.{new['etag']}; Path=/; Secure; SameSite=Lax"
    assert "pk_appearance" not in _session(browser)
    assert _html(browser.get("/").text) == '<html lang="de" data-theme="dark">'
    # One row in papaia.audit/1: the field name, never the value.
    [row] = rows(config_dir)
    validate_event(row)
    assert (row["catalog_id"], row["operation"], row["effect"]) == ("prefs", "write_own", "write")
    assert (row["result"], row["correlation_id"], row["sub"]) == ("allowed", _CID, SUB)
    assert (row["writer"], row["azp"], row["aud"]) == (
        "papaia-manager",
        "papaia-manager",
        "papaia-prefs",
    )
    assert row["detail"] == {"objects": ["appearance"]}
    assert "dark" not in json.dumps(row)
    assert [(e["action"], e["result"]) for e in _log(config_dir)] == [("kit.appearance", "ok")]


def test_the_language_form_writes_prefs(
    client: TestClient, store: Prefs, config_dir: Path
) -> None:
    browser = _as(client)
    response = browser.post("/_kit/language", data={"language": "en", "pk_csrf": _CSRF})
    assert response.status_code == 303
    assert store.puts == [{"locale": "en"}]
    assert _html(browser.get("/", headers={"accept-language": "de"}).text) == '<html lang="en">'
    browser.post("/_kit/language", data={"language": "browser", "pk_csrf": _CSRF})
    assert store.records[SUB]["locale"] == "browser"
    assert _html(browser.get("/", headers={"accept-language": "de"}).text) == '<html lang="de">'
    # The manager's own language file is not written any more.
    assert user_prefs.stored_locale(str(config_dir), SUB) is None
    assert [r["result"] for r in rows(config_dir)] == ["allowed", "allowed"]
    assert [r["detail"] for r in rows(config_dir)] == [{"objects": ["locale"]}] * 2


def test_the_account_language_route_writes_prefs(
    client: TestClient, store: Prefs, config_dir: Path
) -> None:
    browser = _as(client)
    response = browser.post("/account/language", data={"locale": "de", "csrf_token": _CSRF})
    assert response.status_code == 303
    browser.post("/account/language", data={"locale": "auto", "csrf_token": _CSRF})
    assert store.puts == [{"locale": "de"}, {"locale": "browser"}]
    assert user_prefs.stored_locale(str(config_dir), SUB) is None
    assert [r["catalog_id"] for r in rows(config_dir)] == ["prefs", "prefs"]


def test_a_change_from_another_module_in_between_is_retried_once(
    client: TestClient, store: Prefs, config_dir: Path
) -> None:
    browser = _as(client)
    browser.get("/")
    store.change_elsewhere(locale="en")  # the session's etag is stale now
    response = browser.post("/_kit/appearance", data={"appearance": "dark", "pk_csrf": _CSRF})
    assert response.status_code == 303
    record = store.records[SUB]
    assert (record["appearance"], record["locale"]) == ("dark", "en")
    assert _cookie(response, "pk_ui").startswith(f"pk_ui=dark.en.{record['etag']};")
    assert [r["result"] for r in rows(config_dir)] == ["allowed"]


def test_the_cookie_is_shared_below_the_configured_domain(
    config_dir: Path, tmp_path: Path, wired: None, store: Prefs
) -> None:
    _install_catalog(config_dir)
    browser = _as(_app(config_dir, tmp_path, prefs_cookie_domain="example.test"))
    response = browser.post("/_kit/appearance", data={"appearance": "light", "pk_csrf": _CSRF})
    etag = store.records[SUB]["etag"]
    cookie = _cookie(response, "pk_ui")
    expected = f"pk_ui=light.browser.{etag}; Domain=example.test; Path=/; Secure; SameSite=Lax"
    assert cookie == expected
    assert "max-age" not in cookie.lower() and "httponly" not in cookie.lower()


def test_a_write_prefs_cannot_take_fails_with_one_row(
    client: TestClient, store: Prefs, config_dir: Path
) -> None:
    browser = _as(client)
    browser.get("/")
    store.down = True
    response = browser.post("/_kit/appearance", data={"appearance": "dark", "pk_csrf": _CSRF})
    assert response.status_code == 503
    assert "pk_ui=" not in response.headers.get("set-cookie", "")
    [row] = rows(config_dir)
    validate_event(row)
    assert (row["result"], row["error_code"]) == ("error", "PK-UPSTREAM-001")
    assert [(e["action"], e["result"]) for e in _log(config_dir)] == [("kit.appearance", "error")]


def test_a_refused_exchange_fails_the_write_with_one_row(
    client: TestClient, issuer: Issuer, store: Prefs, config_dir: Path
) -> None:
    issuer.refuse = True
    response = _as(client).post("/_kit/language", data={"language": "de", "pk_csrf": _CSRF})
    # The realm no longer accepts the session's token: back to the sign-in.
    assert response.status_code == 307
    assert response.headers["location"].startswith("/auth/login")
    assert store.puts == []
    [row] = rows(config_dir)
    validate_event(row)
    assert row["result"] in ("denied", "error")
    assert row["detail"] == {"objects": ["locale"]}


@pytest.mark.parametrize(
    ("path", "data"),
    [
        ("/_kit/appearance", {"appearance": "system"}),
        ("/_kit/appearance", {"appearance": "dark", "locale": "en"}),
        ("/_kit/language", {"language": "fr"}),
        ("/_kit/language", {"language": ["de", "en"]}),
    ],
)
def test_invalid_forms_never_reach_prefs(
    client: TestClient, store: Prefs, config_dir: Path, path: str, data: dict[str, Any]
) -> None:
    response = _as(client).post(path, data={**data, "pk_csrf": _CSRF})
    assert response.status_code == 400
    assert store.puts == []
    assert rows(config_dir) == []


@pytest.mark.parametrize("path", ["/_kit/appearance", "/_kit/language"])
def test_the_forms_still_need_the_csrf_token(
    client: TestClient, store: Prefs, config_dir: Path, path: str
) -> None:
    field = path.rsplit("/", 1)[1]
    value = {"appearance": "dark", "language": "de"}[field]
    response = _as(client).post(path, data={field: value, "pk_csrf": "wrong-token-value-0001"})
    assert response.status_code == 403
    assert store.puts == []
    assert rows(config_dir) == []


# -- without Mein Bereich ------------------------------------------------------------------


def test_without_mein_bereich_the_manager_keeps_its_own_choices(
    config_dir: Path, tmp_path: Path, wired: None, store: Prefs
) -> None:
    """A manager without the platform's `me` profile has no `prefs` to ask."""
    browser = _as(_app(config_dir, tmp_path))
    browser.post("/_kit/appearance", data={"appearance": "dark", "pk_csrf": _CSRF})
    browser.post("/_kit/language", data={"language": "de", "pk_csrf": _CSRF})
    assert _html(browser.get("/").text) == '<html lang="de" data-theme="dark">'
    assert store.requests == []
    assert user_prefs.stored_locale(str(config_dir), SUB) == "de"
