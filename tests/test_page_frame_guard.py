"""Guard over every full page: the skip link comes first and the title names the page.

T-UI-07 and the shell contract of papaia-kit: on every page of ui/pages.yaml and on the state
page "Kein Zugang", the first element a keyboard reaches is the skip link to `#pk-main`, and
`<title>` reads `<page> – Manager – <installation>`, where `<page>` is the page's one `h1`.
A new page or a page that renders its own frame fails here.
"""

from __future__ import annotations

import html
import os
import sys
import tempfile
from collections.abc import Iterator
from html.parser import HTMLParser
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests"))

import design_world  # noqa: E402

_WORLD = Path(tempfile.mkdtemp(prefix="papaia-page-guard-"))
_CONFIG, _WORKSPACE = _WORLD / "config", _WORLD / "workspace"
_CONFIG.mkdir()
_WORKSPACE.mkdir()
for _key, _value in design_world.environment(
    _CONFIG, _WORKSPACE, host="http://localhost:8120", secret="test-session-secret-value"
).items():
    os.environ.setdefault(_key, _value)

import yaml  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.core import module_catalog, user_prefs  # noqa: E402
from app.core.settings_store import (  # noqa: E402
    BrandingSettings,
    ManagerSettings,
    save_settings,
)
from app.main import create_app  # noqa: E402

PAGES_FILE = REPO / "ui" / "pages.yaml"
SEP = " – "
DEFAULT_INSTALLATION = "papAIa manager"
_FOCUSABLE = frozenset({"button", "select", "textarea", "summary", "iframe", "embed"})


def _rows() -> list[dict[str, str]]:
    doc = yaml.safe_load(PAGES_FILE.read_text(encoding="utf-8"))
    return [row for row in doc["pages"] if "path" in row]


def page_paths() -> list[str]:
    return [row["path"] for row in _rows()]


def _sign_in_as_the_person_of(client: TestClient, pattern: str) -> None:
    """Sign in as the test person of the page (column `person`), e.g. Emil for the inventory."""
    person = next((row.get("person") for row in _rows() if row["path"] == pattern), None)
    person = person or "anna"
    client.cookies.set(
        design_world.SESSION_COOKIE,
        design_world.session_cookie(
            get_settings().manager_session_secret,
            roles=design_world.PERSONS[person],
            person=person,
        ),
    )


class _Frame(HTMLParser):
    """The first focusable element, the `<title>` text and the text of every `h1`."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.first: tuple[str, dict[str, str | None]] | None = None
        self.title = ""
        self.h1: list[str] = []
        self._in: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        names = dict(attrs)
        if self.first is None and _focusable(tag, names):
            self.first = (tag, names)
        if tag == "title":
            self._in = "title"
        elif tag == "h1":
            self._in = "h1"
            self.h1.append("")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("title", "h1"):
            self._in = None

    def handle_data(self, data: str) -> None:
        if self._in == "title":
            self.title += data
        elif self._in == "h1":
            self.h1[-1] += data


def _focusable(tag: str, attrs: dict[str, str | None]) -> bool:
    if "disabled" in attrs:
        return False
    tabindex = attrs.get("tabindex")
    if tabindex is not None and tabindex.strip().startswith("-"):
        return False
    if tabindex is not None or attrs.get("contenteditable") in ("", "true"):
        return True
    if tag == "a":
        return "href" in attrs
    if tag == "input":
        return (attrs.get("type") or "").lower() != "hidden"
    return tag in _FOCUSABLE


def _frame(body: str) -> _Frame:
    parser = _Frame()
    parser.feed(body)
    parser.close()
    return parser


def _text(value: str) -> str:
    return " ".join(html.unescape(value).split())


# -- fixtures ------------------------------------------------------------------------------


@pytest.fixture
def world(tmp_path: Path) -> tuple[Path, Path]:
    return design_world.make_world(tmp_path)


@pytest.fixture
def client(world: tuple[Path, Path]) -> Iterator[TestClient]:
    config, workspace = world
    get_settings.cache_clear()
    module_catalog.reset_readers()
    settings = get_settings().model_copy(
        update={"papaia_config_dir": str(config), "papaia_workspace_dir": str(workspace)}
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    design_world.stand_ins(app)
    test_client = TestClient(app, follow_redirects=False)
    test_client.cookies.set(
        design_world.SESSION_COOKIE,
        design_world.session_cookie(get_settings().manager_session_secret),
    )
    yield test_client
    get_settings.cache_clear()
    module_catalog.reset_readers()


def _assert_frame(body: str, installation: str, where: str) -> None:
    frame = _frame(body)
    assert frame.first is not None, where
    tag, attrs = frame.first
    assert (tag, attrs.get("class"), attrs.get("href")) == ("a", "pk-skip", "#pk-main"), where
    assert body.count('<main id="pk-main"') == 1, where
    assert len(frame.h1) == 1, where
    page = _text(frame.h1[0])
    assert page, where
    assert _text(frame.title) == f"{page}{SEP}Manager{SEP}{installation}", where


# -- every page of the list ----------------------------------------------------------------


def test_the_list_has_pages_to_guard() -> None:
    paths = page_paths()
    assert len(paths) >= 39
    assert "/_kit/specimen" in paths


@pytest.mark.parametrize("pattern", page_paths())
def test_every_page_starts_with_the_skip_link_and_names_itself(
    client: TestClient, pattern: str
) -> None:
    _sign_in_as_the_person_of(client, pattern)
    response = client.get(design_world.sample_path(pattern))
    assert response.status_code == 200, pattern
    _assert_frame(response.text, DEFAULT_INSTALLATION, pattern)


@pytest.mark.parametrize("pattern", page_paths())
def test_the_title_names_the_installation_of_the_branding(
    client: TestClient, world: tuple[Path, Path], pattern: str
) -> None:
    config, _ = world
    save_settings(
        str(config),
        ManagerSettings(branding=BrandingSettings(name="Musterfirma", tagline="Testbetrieb")),
    )
    _sign_in_as_the_person_of(client, pattern)
    response = client.get(design_world.sample_path(pattern))
    assert response.status_code == 200, pattern
    _assert_frame(response.text, "Musterfirma", pattern)


@pytest.mark.parametrize("lang", ["de", "en"])
def test_the_rule_holds_in_both_languages(
    client: TestClient, world: tuple[Path, Path], lang: str
) -> None:
    config, _ = world
    user_prefs.set_locale(str(config), "u-anna", lang)
    for path in ("/", "/help", "/_kit/specimen"):
        response = client.get(path)
        assert response.headers["content-language"] == lang, path
        _assert_frame(response.text, DEFAULT_INSTALLATION, f"{path} ({lang})")


def test_the_no_access_page_follows_the_same_rule(client: TestClient) -> None:
    """The state page "Kein Zugang" of the list: an account without the admin role."""
    client.cookies.set(
        design_world.SESSION_COOKIE,
        design_world.session_cookie(get_settings().manager_session_secret, roles=("user",)),
    )
    response = client.get("/settings")
    assert response.status_code == 403
    _assert_frame(response.text, DEFAULT_INSTALLATION, "no access")


# -- the guard itself ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        # A link before the skip link.
        "<html><head><title>A – Manager – papAIa manager</title></head><body>"
        '<a href="/x">x</a><a class="pk-skip" href="#pk-main">Skip</a>'
        '<main id="pk-main"><h1>A</h1></main></body></html>',
        # A focusable element by tabindex before the skip link.
        "<html><head><title>A – Manager – papAIa manager</title></head><body>"
        '<div tabindex="0"></div><a class="pk-skip" href="#pk-main">Skip</a>'
        '<main id="pk-main"><h1>A</h1></main></body></html>',
        # The title names another page.
        "<html><head><title>B – Manager – papAIa manager</title></head><body>"
        '<a class="pk-skip" href="#pk-main">Skip</a><main id="pk-main"><h1>A</h1></main>'
        "</body></html>",
        # The title leaves out the installation.
        "<html><head><title>A – Manager</title></head><body>"
        '<a class="pk-skip" href="#pk-main">Skip</a><main id="pk-main"><h1>A</h1></main>'
        "</body></html>",
        # Two h1.
        "<html><head><title>A – Manager – papAIa manager</title></head><body>"
        '<a class="pk-skip" href="#pk-main">Skip</a><main id="pk-main"><h1>A</h1><h1>A</h1>'
        "</main></body></html>",
    ],
)
def test_the_guard_refuses_a_broken_frame(body: str) -> None:
    with pytest.raises(AssertionError):
        _assert_frame(body, DEFAULT_INSTALLATION, "seeded")


def test_the_guard_accepts_hidden_and_disabled_controls_before_the_skip_link() -> None:
    body = (
        "<html><head><title>A &amp; B – Manager – papAIa manager</title></head><body>"
        '<input type="hidden" name="x"><button disabled>x</button><a>no href</a>'
        '<a class="pk-skip" href="#pk-main">Skip</a><main id="pk-main"><h1>A &amp; B</h1>'
        "</main></body></html>"
    )
    _assert_frame(body, DEFAULT_INSTALLATION, "seeded")
