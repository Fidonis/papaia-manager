"""Manager pages built from the kit's components (oberfl 6.4 T-UI-01, T-UI-05, 3.5; PLT-24, PLT-75).

The browser run of `pk-conform --design` (scripts/design-check.sh) judges the drawn pages; this
file checks without a browser what the rebuild promises about the markup:

- no design finding waits for the rebuild any more: `ui/design-known.yaml` is empty;
- secondary text uses the kit's muted text colour, never an opacity that drops below the
  contrast the kit's colour pair guarantees;
- every form control of every page of `ui/pages.yaml` has an accessible name, rendered on the
  server, before any script runs (PLT-61-A9);
- the stack status in the frame is a kit badge with a state symbol, and the section tabs keep
  their targets apart (target-gap of the design check).
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from collections.abc import Iterator
from html.parser import HTMLParser
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "tests"))

import check_design  # noqa: E402
import design_world  # noqa: E402

_WORLD = Path(tempfile.mkdtemp(prefix="papaia-pages-kit-"))
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
from app.core import module_catalog  # noqa: E402
from app.main import create_app  # noqa: E402

TEMPLATES = REPO / "src" / "app" / "templates"
KNOWN_FILE = REPO / "ui" / "design-known.yaml"
PAGES_FILE = REPO / "ui" / "pages.yaml"


def page_rows() -> list[tuple[str, str]]:
    doc = yaml.safe_load(PAGES_FILE.read_text(encoding="utf-8"))
    return [(row["path"], row["person"]) for row in doc["pages"] if "path" in row]


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    config, workspace = design_world.make_world(tmp_path)
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


def _as(client: TestClient, person: str) -> TestClient:
    client.cookies.set(
        design_world.SESSION_COOKIE,
        design_world.session_cookie(
            get_settings().manager_session_secret,
            roles=design_world.PERSONS[person],
            person=person,
        ),
    )
    return client


# -- the known list ------------------------------------------------------------------------


def test_no_design_finding_waits_for_the_rebuild() -> None:
    """Acceptance of MGR-013: the CI job `design` is green without a known entry."""
    assert check_design.parse_known(KNOWN_FILE.read_text(encoding="utf-8")) == []


# -- muted text ----------------------------------------------------------------------------

# opacity-0 and opacity-100 are the two ends of a transition; every value between them dims
# text below the contrast of the kit's colour pairs (axe color-contrast on /services,
# /catalogs, /users/roles, ... before the rebuild).
_DIMMED = re.compile(r"(?<![\w-])opacity-(?:[1-9]0|[1-9]5|5)(?![\w-])")


def _sources() -> list[Path]:
    sources = sorted(TEMPLATES.rglob("*.html"))
    sources += sorted((REPO / "src" / "app" / "static" / "js").rglob("*.js"))
    return sources


def test_no_template_dims_text_with_opacity() -> None:
    found = [
        f"{path.relative_to(REPO)}:{number}"
        for path in _sources()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if _DIMMED.search(line)
    ]
    assert found == []


# -- accessible names of form controls -----------------------------------------------------

_NO_NAME_NEEDED = {"hidden", "submit", "button", "reset", "image"}
_NAMING = ("aria-label", ":aria-label", "x-bind:aria-label", "aria-labelledby", "title")


class _Controls(HTMLParser):
    """Form controls of a rendered page and whether markup names them.

    Controls inside `<template>` (Alpine) count as well: they become live without a server
    round trip, so their names have to be in the markup the server sends.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_label = 0
        self.label_for: set[str] = set()
        self.controls: list[tuple[str, dict[str, str], bool]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {k: v or "" for k, v in attrs}
        if tag == "label":
            self.in_label += 1
            for key in ("for", ":for"):
                if values.get(key):
                    self.label_for.add(values[key])
        elif tag in ("input", "select", "textarea"):
            if tag == "input" and values.get("type", "text") in _NO_NAME_NEEDED:
                return
            self.controls.append((tag, values, self.in_label > 0))

    def handle_endtag(self, tag: str) -> None:
        if tag == "label" and self.in_label:
            self.in_label -= 1


def unnamed_controls(body: str) -> list[str]:
    parser = _Controls()
    parser.feed(body)
    unnamed = []
    for tag, values, wrapped in parser.controls:
        if wrapped or any(values.get(key) for key in _NAMING):
            continue
        if values.get("id") in parser.label_for or values.get(":id") in parser.label_for:
            continue
        hook = {k: v for k, v in values.items() if k in ("id", "name", "x-model", "type")}
        unnamed.append(f"{tag} {hook}")
    return unnamed


def test_the_name_check_reads_every_way_of_naming() -> None:
    body = (
        '<label>Name <input name="a"></label>'
        '<label for="b">B</label><input id="b">'
        '<input aria-label="C"><select aria-labelledby="h"></select>'
        '<template><textarea :aria-label="f.label"></textarea></template>'
        '<input type="hidden" name="csrf"><button type="submit">Go</button>'
        '<input name="lost"><template><select x-model="x"></select></template>'
    )
    assert unnamed_controls(body) == ["input {'name': 'lost'}", "select {'x-model': 'x'}"]


@pytest.mark.parametrize(("pattern", "person"), page_rows())
def test_every_form_control_of_a_page_has_a_name(
    client: TestClient, pattern: str, person: str
) -> None:
    response = _as(client, person).get(design_world.sample_path(pattern))
    assert response.status_code == 200, pattern
    assert unnamed_controls(response.text) == [], pattern


# -- frame status and section tabs ----------------------------------------------------------


def test_the_stack_status_is_a_kit_badge_with_a_state_symbol(client: TestClient) -> None:
    body = _as(client, "anna").get("/partials/service-status").text
    badge = re.search(r'<span class="pk-badge" data-pk-tone="([a-z]+)">(.*?)</span>', body, re.S)
    assert badge is not None, body
    assert badge.group(1) in {"neutral", "info", "success", "warning", "error"}
    assert "#pk-icon-" in badge.group(2)
    # The verdict stays in the accessible name of the trigger.
    assert re.search(r'role="button" aria-label="[^"]+"', body)


@pytest.mark.parametrize("path", ["/users", "/mail", "/settings", "/ingest/jobs"])
def test_section_tabs_keep_their_targets_apart(client: TestClient, path: str) -> None:
    body = _as(client, "anna").get(path).text
    navs = re.findall(r'<nav class="([^"]*\btabs\b[^"]*)"', body)
    assert navs, path
    for classes in navs:
        assert "gap-2" in classes.split(), classes
