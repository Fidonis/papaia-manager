"""The language of a request, end to end: the picker's form, the store and the rendered pages.

Each test gets its own configuration directory, injected through the settings dependency, and
a forged session cookie for an account identified by its subject, the way `test_settings.py`
does it.
"""
from __future__ import annotations

import base64
import json
import os
import tempfile
import time
from collections.abc import Iterator
from functools import cache
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest

_CONFIG_DIR = tempfile.mkdtemp(prefix="papaia-i18n-config-")
_WORKSPACE_DIR = tempfile.mkdtemp(prefix="papaia-i18n-workspace-")

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

from babel.messages.pofile import read_po  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from itsdangerous import TimestampSigner  # noqa: E402

from app.config import Settings, get_settings  # noqa: E402
from app.core import user_prefs  # noqa: E402
from app.i18n import get_catalogs  # noqa: E402
from app.i18n.catalog import LOCALE_DIR  # noqa: E402
from app.main import create_app  # noqa: E402

_CSRF = "test-csrf-token-value"
_ALICE = "11111111-1111-1111-1111-111111111111"
_BOB = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "config"
    (directory / "manager").mkdir(parents=True)
    (directory / ".env").write_text("PAPAIA_HOST=https://papaia.test\n", encoding="utf-8")
    return directory


@pytest.fixture
def app(config_dir: Path) -> Iterator[FastAPI]:
    get_settings.cache_clear()
    settings = get_settings().model_copy(update={"papaia_config_dir": str(config_dir)})
    application = create_app()
    application.dependency_overrides[get_settings] = lambda: settings
    yield application
    get_settings.cache_clear()


@pytest.fixture
def settings(app: FastAPI) -> Settings:
    override = app.dependency_overrides[get_settings]
    found = override()
    assert isinstance(found, Settings)
    return found


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, follow_redirects=False)


def _device(app: FastAPI, sub: str, *roles: str) -> TestClient:
    """A browser on its own computer: a client with a session cookie of its own for `sub`."""
    session: dict[str, Any] = {
        "user": {
            "sub": sub,
            "preferred_username": "tester",
            "roles": list(roles),
            "exp": int(time.time()) + 3600,
        },
        "_csrf_token": _CSRF,
    }
    payload = base64.b64encode(json.dumps(session).encode())
    signed = TimestampSigner(get_settings().manager_session_secret).sign(payload).decode()
    browser = TestClient(app, follow_redirects=False)
    browser.cookies.set("papaia_manager_session", signed)
    return browser


def _choose(browser: TestClient, locale: str, **extra: str) -> Any:
    return browser.post(
        "/account/language", data={"locale": locale, "csrf_token": _CSRF, **extra}
    )


# ---------------------------------------------------------------------------
# The picker's form
# ---------------------------------------------------------------------------


def test_a_choice_is_stored_by_subject_and_the_account_is_sent_back(
    app: FastAPI, settings: Settings
) -> None:
    browser = _device(app, _ALICE, "admin")

    response = _choose(browser, "de", next="/audit?actor=x")

    assert response.status_code == 303
    assert response.headers["location"] == "/audit?actor=x"
    assert user_prefs.stored_locale(settings.papaia_config_dir, _ALICE) == "de"
    assert user_prefs.stored_locale(settings.papaia_config_dir, _BOB) is None


def test_the_page_the_choice_was_made_on_is_found_from_the_referer(app: FastAPI) -> None:
    browser = _device(app, _ALICE, "admin")

    response = browser.post(
        "/account/language",
        data={"locale": "de", "csrf_token": _CSRF},
        headers={"Referer": "http://localhost:8120/audit?actor=x"},
    )

    assert response.headers["location"] == "/audit?actor=x"


@pytest.mark.parametrize(
    "target",
    [
        "//evil.test/x",
        "/\\evil.test",
        "https://evil.test/",
        "javascript:alert(1)",
        "evil.test",
        "/ok\nSet-Cookie: x=1",
    ],
)
def test_nothing_but_a_path_on_this_origin_is_followed_back(app: FastAPI, target: str) -> None:
    browser = _device(app, _ALICE, "admin")

    response = _choose(browser, "de", next=target)

    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_a_foreign_referer_cannot_send_the_account_elsewhere(app: FastAPI) -> None:
    browser = _device(app, _ALICE, "admin")

    response = browser.post(
        "/account/language",
        data={"locale": "en", "csrf_token": _CSRF},
        headers={"Referer": "https://evil.test//phish"},
    )

    assert response.headers["location"] == "/"


def test_the_form_needs_the_csrf_token(app: FastAPI, settings: Settings) -> None:
    browser = _device(app, _ALICE, "admin")

    missing = browser.post("/account/language", data={"locale": "de"})
    wrong = browser.post("/account/language", data={"locale": "de", "csrf_token": "nope"})

    assert missing.status_code == 403
    assert wrong.status_code == 403
    assert user_prefs.stored_locale(settings.papaia_config_dir, _ALICE) is None


def test_anonymous_visitors_are_sent_to_the_login(client: TestClient) -> None:
    response = client.post("/account/language", data={"locale": "de", "csrf_token": _CSRF})

    assert response.status_code in (307, 401, 403)
    assert "/auth/login" in response.headers.get("location", "/auth/login")


def test_the_dashboard_only_role_may_choose_its_own_language(
    app: FastAPI, settings: Settings
) -> None:
    browser = _device(app, _BOB, "user")

    assert _choose(browser, "de").status_code == 303
    assert user_prefs.stored_locale(settings.papaia_config_dir, _BOB) == "de"


def test_a_language_that_is_not_shipped_is_refused_and_nothing_is_stored(
    app: FastAPI, settings: Settings
) -> None:
    browser = _device(app, _ALICE, "admin")

    response = _choose(browser, "tlh")

    assert response.status_code == 422
    assert user_prefs.stored_locale(settings.papaia_config_dir, _ALICE) is None


def test_automatic_puts_the_account_back_to_the_browser(
    app: FastAPI, settings: Settings
) -> None:
    browser = _device(app, _ALICE, "admin")
    _choose(browser, "de")

    _choose(browser, "auto")

    assert user_prefs.stored_locale(settings.papaia_config_dir, _ALICE) is None


def test_a_damaged_preferences_file_is_reported_not_overwritten(
    app: FastAPI, settings: Settings
) -> None:
    path = user_prefs.preferences_path(settings.papaia_config_dir)
    path.write_text("users: [broken", encoding="utf-8")
    browser = _device(app, _ALICE, "admin")

    response = _choose(browser, "de")

    assert response.status_code == 503
    assert path.read_text(encoding="utf-8") == "users: [broken"


# ---------------------------------------------------------------------------
# The same account on another computer
# ---------------------------------------------------------------------------


def test_the_choice_follows_the_account_to_another_computer(app: FastAPI) -> None:
    first = _device(app, _ALICE, "admin")
    assert '<html lang="en">' in first.get("/").text
    _choose(first, "de")

    second = _device(app, _ALICE, "admin")
    page = second.get("/")

    assert '<html lang="de">' in page.text
    assert page.headers["content-language"] == "de"
    assert "Abmelden" in page.text
    assert "Sign out" not in page.text


def test_another_account_is_not_affected(app: FastAPI) -> None:
    _choose(_device(app, _ALICE, "admin"), "de")

    page = _device(app, _BOB, "admin").get("/")

    assert '<html lang="en">' in page.text


def test_the_picker_marks_the_choice_and_offers_every_shipped_language(app: FastAPI) -> None:
    browser = _device(app, _ALICE, "admin")
    _choose(browser, "de")

    page = browser.get("/").text

    assert 'name="locale" value="auto"' in page
    codes = get_catalogs().codes()
    assert {"en", "de"} <= set(codes)
    for code in codes:
        assert f'name="locale" value="{code}"' in page
    german = page.index('name="locale" value="de"')
    assert 'aria-current="true"' in page[german : german + 300]
    english = page.index('name="locale" value="en"')
    assert 'aria-current="true"' not in page[english : english + 300]


# ---------------------------------------------------------------------------
# Where the language comes from when nothing is stored
# ---------------------------------------------------------------------------


def test_the_browser_decides_on_the_first_visit(app: FastAPI) -> None:
    browser = _device(app, _ALICE, "admin")

    german = browser.get("/", headers={"Accept-Language": "de-DE,de;q=0.9,en;q=0.5"})
    english = browser.get("/", headers={"Accept-Language": "en-US"})
    unknown = browser.get("/", headers={"Accept-Language": "tlh"})

    assert '<html lang="de">' in german.text
    assert '<html lang="en">' in english.text
    assert '<html lang="en">' in unknown.text


def test_a_stored_choice_beats_the_browser(app: FastAPI) -> None:
    browser = _device(app, _ALICE, "admin")
    _choose(browser, "en")

    page = browser.get("/", headers={"Accept-Language": "de"})

    assert '<html lang="en">' in page.text


def test_the_installation_default_applies_last(app: FastAPI, settings: Settings) -> None:
    app.dependency_overrides[get_settings] = lambda: settings.model_copy(
        update={"manager_default_locale": "de"}
    )
    browser = _device(app, _ALICE, "admin")

    assert '<html lang="de">' in browser.get("/").text
    assert '<html lang="en">' in browser.get("/", headers={"Accept-Language": "en"}).text


def test_a_default_that_is_not_shipped_means_english(app: FastAPI, settings: Settings) -> None:
    app.dependency_overrides[get_settings] = lambda: settings.model_copy(
        update={"manager_default_locale": "tlh"}
    )

    assert '<html lang="en">' in _device(app, _ALICE, "admin").get("/").text


# ---------------------------------------------------------------------------
# Pages, partials, errors
# ---------------------------------------------------------------------------


def test_an_htmx_partial_follows_the_language_too(app: FastAPI) -> None:
    browser = _device(app, _ALICE, "admin")
    english = browser.get("/partials/service-status", headers={"HX-Request": "true"})
    _choose(browser, "de")

    german = browser.get("/partials/service-status", headers={"HX-Request": "true"})

    assert english.status_code == german.status_code == 200
    assert german.text != english.text
    assert "Stack" in german.text


def test_the_access_denied_page_is_in_the_language_of_the_account(
    app: FastAPI, settings: Settings
) -> None:
    user_prefs.set_locale(settings.papaia_config_dir, _BOB, "de")
    browser = _device(app, _BOB, "offline_access")

    page = browser.get("/")

    assert page.status_code == 403
    assert "Zugriff verweigert" in page.text
    assert "Back to dashboard" not in page.text


def test_an_api_error_is_worded_in_the_language_of_the_account(
    app: FastAPI, settings: Settings
) -> None:
    user_prefs.set_locale(settings.papaia_config_dir, _BOB, "de")
    user = _device(app, _BOB, "user")
    english_user = _device(app, _ALICE, "user")

    german = user.get("/api/v1/audit")
    english = english_user.get("/api/v1/audit")

    assert german.status_code == english.status_code == 403
    assert english.json()["detail"] == "admin role required"
    assert german.json()["detail"] == "Admin-Rolle erforderlich"


def test_the_language_of_one_request_does_not_leak_into_the_next(app: FastAPI) -> None:
    german = _device(app, _ALICE, "admin")
    _choose(german, "de")
    english = _device(app, _BOB, "admin")

    for _ in range(3):
        assert '<html lang="de">' in german.get("/").text
        assert '<html lang="en">' in english.get("/").text


def _get_pages(app: FastAPI) -> list[str]:
    paths = app.openapi()["paths"]
    return sorted(
        path
        for path, operations in paths.items()
        if "get" in operations
        and "{" not in path
        and not path.startswith(("/api/", "/static", "/auth", "/health", "/brand"))
    )


_VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}
)


class _VisibleText(HTMLParser):
    """The text a reader sees, one string per text node: not scripts, styles or code, which
    show values and names that are the same in every language."""

    def __init__(self) -> None:
        super().__init__()
        self.nodes: list[str] = []
        self._open: list[bool] = []  # per open element: does it hide its text?
        self._hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _VOID:
            return
        classes = (dict(attrs).get("class") or "").split()
        hides = tag in ("script", "style", "code", "pre", "kbd") or "font-mono" in classes
        self._open.append(hides)
        self._hidden += hides

    def handle_endtag(self, tag: str) -> None:
        if tag not in _VOID and self._open:
            self._hidden -= self._open.pop()

    def handle_data(self, data: str) -> None:
        text = " ".join(data.split())
        if text and not self._hidden:
            self.nodes.append(text)


@cache
def _english_texts_that_german_changes() -> frozenset[str]:
    """English texts of the catalogue that have a different German text: if one of them is on a
    German page as it is, something printed it without translating it."""
    with (LOCALE_DIR / "de" / "LC_MESSAGES" / "messages.po").open("rb") as handle:
        catalogue = read_po(handle, locale="de")
    return frozenset(
        message.id
        for message in catalogue
        if isinstance(message.id, str)
        and message.string
        and message.string != message.id
        and len(message.id) >= 4
        and "%(" not in message.id
        and "{" not in message.id
    )


def test_every_page_renders_in_german_exactly_when_it_renders_in_english(
    app: FastAPI,
) -> None:
    pages = _get_pages(app)
    assert len(pages) > 20  # the inventory itself must not silently shrink to nothing
    english = _device(app, _ALICE, "admin")
    german = _device(app, _BOB, "admin")
    _choose(german, "de")
    untranslated = _english_texts_that_german_changes()
    assert len(untranslated) > 1000

    leaks: dict[str, list[str]] = {}
    for path in pages:
        in_english = english.get(path)
        in_german = german.get(path)
        assert in_german.status_code == in_english.status_code, path
        assert in_german.status_code < 500, path
        if in_english.status_code == 200 and "text/html" in in_english.headers["content-type"]:
            assert '<html lang="de">' in in_german.text or "<html" not in in_german.text, path
            assert "{%" not in in_german.text and "%(" not in in_german.text, path
            reader = _VisibleText()
            reader.feed(in_german.text)
            found = sorted(set(reader.nodes) & untranslated)
            if found:
                leaks[path] = found

    assert not leaks, f"English texts on a German page: {leaks}"
