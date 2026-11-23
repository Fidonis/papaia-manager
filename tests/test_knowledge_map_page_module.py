"""The knowledge map runs on the CSP build of Alpine (T-UI-03, oberfl 3.3).

Its script is the page module static/js/pages/knowledge-map.js with the component in
static/js/pages/knowledge-map/component.js; the texts come from the data island `km-i18n`,
the graph's styles from the stylesheet. The template keeps no executable inline script, no
`on*` attribute, no `<style>` element and no Alpine expression the CSP build refuses, and
everything its expressions call is a method of the component. The rendered page is checked
in a browser by tests/test_knowledge_map_browser.py.
"""

from __future__ import annotations

import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests"))

import csp_scan  # noqa: E402
from test_knowledge_map import _as, client, config_dir, queue, server  # noqa: E402, F401

TEMPLATE = REPO / "src" / "app" / "templates" / "knowledge_map.html"
JS = REPO / "src" / "app" / "static" / "js"
ENTRY = JS / "pages" / "knowledge-map.js"
COMPONENT = JS / "pages" / "knowledge-map" / "component.js"
STYLESHEET = REPO / "docker" / "tailwind.app.css"
BASELINE = REPO / "tests" / "csp_baseline.json"


def _page(client) -> str:  # noqa: ANN001, F811 -- the fixture of test_knowledge_map
    response = _as(client, "admin").get("/knowledge-map")
    assert response.status_code == 200
    return response.text


class _Scripts(HTMLParser):
    """Every <script> of a page with its attributes and text, in document order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: list[dict[str, str]] = []
        self._open: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self._open = {name: value or "" for name, value in attrs}
            self._open["#text"] = ""
            self.scripts.append(self._open)

    def handle_data(self, data: str) -> None:
        if self._open is not None:
            self._open["#text"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._open = None


def _scripts(markup: str) -> list[dict[str, str]]:
    parser = _Scripts()
    parser.feed(markup)
    return parser.scripts


# -- the template --------------------------------------------------------------------------


def test_the_page_is_converted_and_has_no_row_in_the_baseline() -> None:
    doc = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert "knowledge_map.html" in doc["converted"]
    assert "knowledge_map.html" not in doc["templates"]


def test_the_template_has_nothing_the_strict_policy_refuses() -> None:
    counts = csp_scan.count_markup(TEMPLATE.read_text(encoding="utf-8"))
    assert counts == dict.fromkeys(csp_scan.KINDS, 0)


def test_the_graph_styles_live_in_the_stylesheet() -> None:
    css = STYLESHEET.read_text(encoding="utf-8")
    for selector in (
        ".km-graph text",
        ".km-graph .lane",
        ".km-graph .node.dim",
        ".km-graph .edge.sot",
        ".km-graph .edge.instead",
        ".km-graph .edge.hi",
    ):
        assert selector + " {" in css, selector


def _alpine_values(markup: str) -> list[tuple[str, str]]:
    values: list[tuple[str, str]] = []

    class _Collect(HTMLParser):
        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            for name, value in attrs:
                if csp_scan.is_alpine_attribute(name) and value:
                    values.append((name, value))

    collector = _Collect(convert_charrefs=True)
    collector.feed(csp_scan.strip_jinja(markup))
    return values


def _methods() -> set[str]:
    # The members of the object the component returns: two spaces deeper than its `return {`.
    source = COMPONENT.read_text(encoding="utf-8")
    return set(re.findall(r"^    (?:async )?([A-Za-z_$][\w$]*)\(", source, re.MULTILINE))


def test_every_function_the_expressions_call_is_a_method_of_the_component() -> None:
    # A plain name followed by `(`: on the CSP build it is looked up in the component's data
    # and nowhere else, so a former global would be an "Undefined variable".
    called: set[str] = set()
    for name, value in _alpine_values(TEMPLATE.read_text(encoding="utf-8")):
        if name == "x-data":
            assert value == "knowledgeMap()"
            continue
        code = csp_scan._STRINGS.sub("''", value)  # noqa: SLF001
        called |= set(re.findall(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*\(", code))
    assert called, "the template calls no method"
    assert called <= _methods(), sorted(called - _methods())


def test_no_expression_needs_a_global_of_the_browser() -> None:
    # The forms of csp_scan plus a reach for `Boolean`, which the scanner's list does not
    # carry and the CSP build does not resolve either.
    for name, value in _alpine_values(TEMPLATE.read_text(encoding="utf-8")):
        assert not csp_scan.alpine_rejected(name, value), (name, value)
        assert not re.search(r"(?<![\w$.])Boolean\b", value), (name, value)


# -- the module -----------------------------------------------------------------------------


def test_the_component_reads_no_global_of_the_page_script() -> None:
    # A name of core/legacy.js the module uses is one it imports from the core modules.
    legacy = (JS / "core" / "legacy.js").read_text(encoding="utf-8")
    block = legacy[legacy.index("LEGACY_NAMES = Object.freeze([") :]
    names = re.findall(r'"([^"]+)",', block[: block.index("]);")])
    for path in (COMPONENT, ENTRY):
        source = path.read_text(encoding="utf-8")
        imported = {
            n.strip()
            for group in re.findall(r"^import \{([^}]*)\} from \"[./]+(?:core/)", source, re.M)
            for n in group.split(",")
        }
        for name in names:
            if re.search(r"(?<![\w$.])" + re.escape(name) + r"\b", source):
                assert name in imported, (path.name, name)
        assert "window." not in source or path == ENTRY, path.name


def test_the_entry_registers_the_component_when_alpine_starts() -> None:
    source = ENTRY.read_text(encoding="utf-8")
    assert 'addEventListener("alpine:init"' in source
    assert 'Alpine.data("knowledgeMap"' in source
    assert 'readIsland("km-i18n")' in source


def _texts_read(source: str) -> set[str]:
    return set(re.findall(r"(?<![\w$.])T\.([A-Za-z_]\w*)", source)) | set(
        re.findall(r"(?<![\w$.])T\[\"([A-Za-z_]\w*)\"\]", source)
    )


# -- the rendered page ------------------------------------------------------------------------


def test_the_page_runs_on_the_csp_build_with_the_module_before_alpine(client) -> None:  # noqa: ANN001, F811
    page = _page(client)
    assert page.count('<meta name="papaia-csp" content="strict">') == 1
    sources = [s.get("src", "") for s in _scripts(page) if "src" in s]
    main = next(i for i, s in enumerate(sources) if "js/core/main.js" in s)
    module = next(i for i, s in enumerate(sources) if "js/pages/knowledge-map.js" in s)
    alpine = next(i for i, s in enumerate(sources) if "alpine" in s)
    assert "alpine-csp.min.js" in sources[alpine]
    assert not any(s.endswith("alpine.min.js") or "/alpine.min.js?" in s for s in sources)
    # Deferred scripts and modules run in document order: the component is registered on
    # `alpine:init`, so its module has to run before Alpine starts.
    assert main < module < alpine
    (tag,) = [s for s in _scripts(page) if "js/pages/knowledge-map.js" in s.get("src", "")]
    assert tag.get("type") == "module"


def test_the_rendered_page_has_no_executable_inline_script(client) -> None:  # noqa: ANN001, F811
    page = _page(client)
    for script in _scripts(page):
        if "src" not in script:
            assert script.get("type") == "application/json", script
    counts = csp_scan.count_markup(page)
    assert counts["inline_scripts"] == 0
    assert counts["on_attributes"] == 0
    assert counts["style_blocks"] == 0


def test_the_island_carries_every_text_the_page_reads(client) -> None:  # noqa: ANN001, F811
    page = _page(client)
    (island,) = [s for s in _scripts(page) if s.get("id") == "km-i18n"]
    assert island["type"] == "application/json"
    texts = json.loads(island["#text"])
    read = _texts_read(COMPONENT.read_text(encoding="utf-8"))
    read |= _texts_read(TEMPLATE.read_text(encoding="utf-8"))
    assert read <= set(texts), sorted(read - set(texts))
    # The texts that used to be written into the script by the server.
    assert set(texts["parts"]) == {"all", "development", "holdout"}
    assert len(texts["quality"]) == 9
    assert all(isinstance(v, str) and v for v in texts["quality"].values())
    # Groups the component looks up with lbl(group, value).
    for group in ("learning", "classes", "tiers", "personal_data", "subjects"):
        assert isinstance(texts[group], dict) and texts[group], group


def test_the_page_without_the_module_loads_no_page_module(client, config_dir: Path) -> None:  # noqa: ANN001, F811
    (config_dir / ".env").write_text("COMPOSE_PROFILES=librechat\n", encoding="utf-8")
    page = _page(client)
    assert "js/pages/knowledge-map.js" not in page
    assert 'id="km-i18n"' not in page
