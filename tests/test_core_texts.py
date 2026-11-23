"""The shared page script of base.html (T-UI-04, oberfl 2.1, UXB-P07; PLT-61-A10, PLT-62-A5).

base.html carries no executable inline script: its helpers are the modules of
src/app/static/js/core/, its texts a data island (`mgr-core`) in the language of the page,
with the kit's three-part texts per error class. Messages go to the frame's live regions
`#pk-toasts`, whose `role="status"` and `role="alert"` regions exist before the first message;
the toasts are the kit's, one <template> per tone. A refused request of a page is logged with
the correlation id the page shows as the reference.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import tempfile
from collections.abc import Iterator
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest

_WORLD = Path(tempfile.mkdtemp(prefix="papaia-core-texts-"))
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests"))

import design_world  # noqa: E402

_CONFIG, _WORKSPACE = _WORLD / "config", _WORLD / "workspace"
_CONFIG.mkdir()
_WORKSPACE.mkdir()
for _key, _value in design_world.environment(
    _CONFIG, _WORKSPACE, host="http://localhost:8120", secret="test-session-secret-value"
).items():
    os.environ.setdefault(_key, _value)

from fastapi.testclient import TestClient  # noqa: E402
from papaia_kit.errors import (  # noqa: E402
    ErrorClass,
    display_text,
    find_forbidden_patterns,
    load_catalog,
)

from app.client_texts import error_texts  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.core import module_catalog  # noqa: E402
from app.main import create_app  # noqa: E402

FIXTURE = REPO / "tests" / "js" / "fixtures" / "error-texts.en.json"
TEMPLATES = REPO / "src" / "app" / "templates"
TONES = ("success", "info", "warning", "error")
# The four cases of PLT-62-A5 and the class each is told as.
CASES = {
    "connection stopped": "upstream_unavailable",
    "right missing": "forbidden_or_not_found",
    "input invalid": "invalid_input",
    "session expired": "unauthenticated",
}


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


class _Page(HTMLParser):
    """Scripts, the toast regions and the toast templates of a rendered page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: list[dict[str, str | None]] = []
        self.islands: dict[str, str] = {}
        self._island: str | None = None
        self.regions: list[tuple[str | None, int]] = []
        self._depth = 0
        self._toasts_depth: int | None = None
        self._region_open: int | None = None
        self.region_children: dict[str, int] = {}
        self.templates: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        self._depth += 1
        if tag == "script":
            self.scripts.append(a)
            if a.get("type") == "application/json" and a.get("id"):
                self._island = str(a["id"])
                self.islands[self._island] = ""
        if tag == "template" and a.get("id"):
            self.templates.append(str(a["id"]))
        if a.get("id") == "pk-toasts":
            self._toasts_depth = self._depth
        elif self._toasts_depth is not None and self._depth == self._toasts_depth + 1:
            role = a.get("role")
            self.regions.append((role, self._depth))
            self.region_children[str(role)] = 0
            self._region_open = self._depth
        elif self._region_open is not None and self._depth == self._region_open + 1:
            self.region_children[self.regions[-1][0] or ""] += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._island = None
        if self._region_open is not None and self._depth == self._region_open:
            self._region_open = None
        if self._toasts_depth is not None and self._depth == self._toasts_depth:
            self._toasts_depth = None
        self._depth -= 1

    def handle_data(self, data: str) -> None:
        if self._island is not None:
            self.islands[self._island] += data


def _parse(html: str) -> _Page:
    page = _Page()
    page.feed(html)
    page.close()
    return page


def _island(html: str) -> dict[str, Any]:
    value: dict[str, Any] = json.loads(_parse(html).islands["mgr-core"])
    return value


# -- base.html ------------------------------------------------------------------------------


def test_a_page_without_scripts_of_its_own_has_no_executable_inline_script(
    client: TestClient,
) -> None:
    # The frame, base.html and the status row add none (their rows in the ratchet are gone).
    for path in ("/jobs", "/me", "/help"):
        page = _parse(client.get(path).text)
        for script in page.scripts:
            kind = (script.get("type") or "").lower()
            assert script.get("src") or kind == "application/json", (path, script)


def test_the_core_modules_load_before_htmx_and_alpine(client: TestClient) -> None:
    sources = [str(s.get("src")) for s in _parse(client.get("/jobs").text).scripts if s.get("src")]
    main = next(i for i, s in enumerate(sources) if "/js/core/main.js" in s)
    htmx = next(i for i, s in enumerate(sources) if "htmx.min.js" in s)
    alpine = next(i for i, s in enumerate(sources) if "alpine" in s)
    assert main < htmx < alpine
    scripts = _parse(client.get("/jobs").text).scripts
    assert next(s for s in scripts if "/js/core/main.js" in str(s.get("src")))["type"] == "module"


def test_the_live_regions_are_there_before_the_first_message(client: TestClient) -> None:
    body = client.get("/").text
    page = _parse(body)
    assert [role for role, _ in page.regions] == ["status", "alert"]
    assert page.region_children == {"status": 0, "alert": 0}
    assert 'id="toasts"' not in body
    assert body.count('id="pk-toasts"') == 1


def test_one_kit_toast_template_per_tone(client: TestClient) -> None:
    body = client.get("/").text
    page = _parse(body)
    assert [t for t in page.templates if t.startswith("mgr-toast-")] == [
        f"mgr-toast-{tone}" for tone in TONES
    ]
    for tone in TONES:
        block = body[body.index(f'<template id="mgr-toast-{tone}">') :]
        block = block[: block.index("</template>")]
        assert 'class="pk-toast"' in block
        assert f'data-pk-tone="{tone}"' in block
        assert 'class="pk-message-text"' in block
        # In a region the toast has no role of its own; the region announces it.
        assert "role=" not in block
        # The close button is labelled; the kit's shell.js closes the message.
        assert re.search(r'<button [^>]*data-pk-dismiss[^>]*aria-label="Close message"', block)


def test_the_island_carries_the_texts_of_the_shared_actions(client: TestClient) -> None:
    texts = _island(client.get("/").text)["texts"]
    assert texts["stackActionFinished"] == "The stack action finished."
    assert texts["justNow"] == "just now"
    assert "{href}" in texts["maintenanceActionFailed"]


_I18N_KEY = re.compile(r"(?<![\w$])I18N\.(\w+)|(?<![\w$])I18N\[\s*[\"'](\w+)[\"']\s*\]")
_PAGE_TEXTS = re.compile(r"\b\w*_I18N\s*=\s*\{(?P<body>.*?)\};", re.DOTALL)


def _keys_used(source: str) -> set[str]:
    return {a or b for a, b in _I18N_KEY.findall(source)}


def _keys_merged_by_the_page(source: str) -> set[str]:
    """Texts a page script merges into I18N itself (`Object.assign(I18N, <NAME>_I18N)`)."""
    keys: set[str] = set()
    for block in _PAGE_TEXTS.finditer(source):
        name = source[block.start() : block.end()].split("=", 1)[0].strip().split()[-1]
        if f"Object.assign(I18N, {name})" in source:
            keys |= set(re.findall(r"^\s*(\w+)\s*:", block.group("body"), re.MULTILINE))
    return keys


def test_every_text_a_page_reads_from_i18n_is_in_the_island(client: TestClient) -> None:
    """A key the island lacks reads as `undefined` in the message (PLT-62-A5)."""
    texts = set(_island(client.get("/").text)["texts"])
    js = REPO / "src" / "app" / "static" / "js"
    sources = [*TEMPLATES.rglob("*.html"), *js.rglob("*.js")]
    missing = {}
    for path in sources:
        source = path.read_text(encoding="utf-8")
        lacking = _keys_used(source) - texts - _keys_merged_by_the_page(source)
        if lacking:
            missing[str(path.relative_to(REPO))] = sorted(lacking)
    assert missing == {}


def test_the_key_scan_sees_both_spellings() -> None:
    source = 'fmt(I18N.catalogActionFailed, {}); x = I18N["other"]; y = UPGRADE_I18N.notThis;'
    assert _keys_used(source) == {"catalogActionFailed", "other"}
    merged = 'var PAGE_I18N = {\n  a: "x",\n  b: "y",\n};\nObject.assign(I18N, PAGE_I18N);'
    assert _keys_merged_by_the_page(merged) == {"a", "b"}
    assert _keys_merged_by_the_page(merged.replace("Object.assign(I18N, PAGE_I18N);", "")) == set()


@pytest.mark.parametrize(("language", "label"), [("en", "Reference"), ("de", "Kennung")])
def test_the_island_carries_the_kits_error_texts_in_the_language_of_the_page(
    client: TestClient, language: str, label: str
) -> None:
    errors = _island(client.get("/", headers={"Accept-Language": language}).text)["errors"]
    assert errors["reference_label"] == label
    assert set(errors["classes"]) == {c.value for c in ErrorClass}
    codes = load_catalog().class_codes
    for error_class in ErrorClass:
        kit = display_text(
            codes[error_class], correlation_id="00000000-0000-7000-8000-000000000000", lang=language
        )
        shown = errors["classes"][error_class.value]
        assert shown["what"] == kit.what
        assert shown["todo"] == kit.todo
        assert shown["consequence"] == (kit.consequence or "")


@pytest.mark.parametrize("language", ["en", "de"])
@pytest.mark.parametrize("case", sorted(CASES))
def test_the_four_cases_have_three_clean_parts(language: str, case: str) -> None:
    shown = error_texts(language, None)["classes"][CASES[case]]
    assert shown["what"] and shown["todo"]
    for part in ("what", "consequence", "todo"):
        assert find_forbidden_patterns(shown[part], kind="message") == [], (case, part)


def test_the_contact_point_fills_the_todo() -> None:
    shown = error_texts("en", "Service desk")["classes"]["forbidden_or_not_found"]
    assert shown["todo"] == "Ask Service desk if you need access."


def test_the_fixture_of_the_js_tests_is_the_kits_english_text() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert fixture == error_texts("en", None)


# -- the reference leads to the log ---------------------------------------------------------


def test_a_refused_request_is_logged_with_the_reference_the_page_shows(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    sent = "0190f3a2-7c4e-7a51-9b1d-2f6c8e4a1b3d"
    with caplog.at_level(logging.INFO, logger="app.core.kit_audit"):
        response = client.post(
            "/api/v1/catalogs/papers/refresh", headers={"X-Papaia-Correlation-Id": sent}
        )
    assert response.status_code == 403  # no CSRF token: the right check refuses
    assert response.headers["X-Papaia-Correlation-Id"] == sent
    lines = [r.getMessage() for r in caplog.records if "request refused" in r.getMessage()]
    assert lines == [
        f"request refused: POST /api/v1/catalogs/papers/refresh answered 403, correlation id {sent}"
    ]


def test_a_token_shaped_reference_is_replaced_before_it_is_logged(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="app.core.kit_audit"):
        response = client.post(
            "/api/v1/catalogs/papers/refresh", headers={"X-Papaia-Correlation-Id": "x" * 65}
        )
    answered = response.headers["X-Papaia-Correlation-Id"]
    assert answered != "x" * 65
    assert all("x" * 65 not in r.getMessage() for r in caplog.records)
    assert any(answered in r.getMessage() for r in caplog.records)


def test_answered_pages_and_plain_navigations_are_not_logged_as_refused(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="app.core.kit_audit"):
        assert client.get("/jobs").status_code == 200
        assert client.get("/no-such-page").status_code == 404
    assert not [r for r in caplog.records if "request refused" in r.getMessage()]
