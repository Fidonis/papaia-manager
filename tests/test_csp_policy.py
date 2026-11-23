"""The content security policy of the manager, enforced (T-UI-03, oberfl 3.3, P4).

Every HTML response carries the policy as `Content-Security-Policy`: `script-src 'self'` and
no `unsafe-inline` or `unsafe-eval` in any directive, `frame-ancestors` only the manager's own
host. No report-only header is left. A response with a policy of its own keeps it. Every page
loads the CSP build of Alpine and no other build; the standard build is not in the image.
htmx evaluates nothing, injects no style and runs no script tag of a swapped fragment. The
pages of ui/pages.yaml, the list the browser check opens, render every full page of the
template folder.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

_WORLD = Path(tempfile.mkdtemp(prefix="papaia-csp-policy-"))
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests"))

import csp_scan  # noqa: E402
import design_world  # noqa: E402

_CONFIG, _WORKSPACE = _WORLD / "config", _WORLD / "workspace"
_CONFIG.mkdir()
_WORKSPACE.mkdir()
for _key, _value in design_world.environment(
    _CONFIG, _WORKSPACE, host="http://localhost:8120", secret="test-session-secret-value"
).items():
    os.environ.setdefault(_key, _value)

import yaml  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import csp  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.core import module_catalog  # noqa: E402
from app.main import create_app  # noqa: E402
from app.templating import templates as csp_templates  # noqa: E402

HEADER = "content-security-policy"
REPORT_ONLY = "content-security-policy-report-only"
# The target policy of T-UI-03, spelled out here rather than imported: the test is the
# second reading of the specification.
POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; "
    "frame-ancestors 'self'"
)
TEMPLATES = REPO / "src" / "app" / "templates"


def _page_rows() -> list[tuple[str, str]]:
    """(path, person) of every page with a path: the test person who sees the page."""
    doc = yaml.safe_load((REPO / "ui" / "pages.yaml").read_text(encoding="utf-8"))
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


def _directives(policy: str) -> dict[str, str]:
    pairs = (part.strip().split(" ", 1) for part in policy.split(";") if part.strip())
    return {name: value for name, value in pairs}


# -- the header ----------------------------------------------------------------------------


def test_the_policy_is_exactly_the_target_policy() -> None:
    assert csp.POLICY == POLICY
    directives = _directives(csp.POLICY)
    assert directives["script-src"] == "'self'"
    assert directives["frame-ancestors"] == "'self'"
    for name, value in directives.items():
        assert "unsafe-inline" not in value, name
        assert "unsafe-eval" not in value, name
        assert "unsafe-hashes" not in value, name
    # No exception for style attributes either: no third-party library needs one.
    assert "style-src-attr" not in directives


def _sign_in(client: TestClient, person: str) -> None:
    client.cookies.set(
        design_world.SESSION_COOKIE,
        design_world.session_cookie(
            get_settings().manager_session_secret,
            roles=design_world.PERSONS[person],
            person=person,
        ),
    )


@pytest.mark.parametrize(("pattern", "person"), _page_rows())
def test_every_page_carries_the_enforced_policy(
    client: TestClient, pattern: str, person: str
) -> None:
    _sign_in(client, person)
    response = client.get(design_world.sample_path(pattern))
    assert response.status_code == 200, pattern
    assert response.headers.get_list(HEADER) == [POLICY]
    assert REPORT_ONLY not in response.headers


def test_the_pages_of_the_check_list_render_every_full_page_template(client: TestClient) -> None:
    """The browser check opens the pages of ui/pages.yaml; together they render every full
    page of the template folder, so enforcing cannot break a page the check never opens."""
    rendered: set[str] = set()
    for pattern, person in _page_rows():
        _sign_in(client, person)
        response = client.get(design_world.sample_path(pattern))
        assert response.status_code == 200, pattern
        rendered.add(response.template.name)  # type: ignore[attr-defined]
    _sign_in(client, "anna")
    full = {path.name for path in TEMPLATES.glob("*.html")} - {"base.html", "error.html"}
    assert full - rendered == set()
    assert rendered <= full | {"error.html"}


def test_the_no_access_page_carries_the_policy(client: TestClient) -> None:
    client.cookies.set(
        design_world.SESSION_COOKIE,
        design_world.session_cookie(get_settings().manager_session_secret, roles=("user",)),
    )
    refused = client.get("/settings")
    assert refused.status_code == 403
    assert refused.template.name == "error.html"  # type: ignore[attr-defined]
    assert refused.headers.get_list(HEADER) == [POLICY]
    assert REPORT_ONLY not in refused.headers


def test_partials_loaded_into_a_page_carry_the_policy(client: TestClient) -> None:
    response = client.get("/partials/service-status")
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers.get_list(HEADER) == [POLICY]
    assert REPORT_ONLY not in response.headers


def test_json_responses_get_no_page_policy(client: TestClient) -> None:
    response = client.get("/health")
    assert response.headers["content-type"].startswith("application/json")
    assert HEADER not in response.headers
    assert REPORT_ONLY not in response.headers


def test_the_report_only_middleware_is_gone() -> None:
    assert not hasattr(csp, "ReportOnlyPolicyMiddleware")
    source = (REPO / "src" / "app").rglob("*.py")
    for path in source:
        assert "report-only" not in path.read_text(encoding="utf-8").lower(), path


def _bare_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(csp.PolicyMiddleware)

    @app.get("/own")
    async def own() -> HTMLResponse:
        return HTMLResponse("<p>x</p>", headers={"Content-Security-Policy": "default-src 'none'"})

    @app.get("/own-report")
    async def own_report() -> HTMLResponse:
        return HTMLResponse("<p>x</p>", headers={REPORT_ONLY: "default-src 'none'"})

    @app.get("/page")
    async def page() -> HTMLResponse:
        return HTMLResponse("<p>x</p>")

    @app.get("/upper")
    async def upper() -> HTMLResponse:
        return HTMLResponse("<p>x</p>", media_type="TEXT/HTML")

    @app.get("/data")
    async def data() -> JSONResponse:
        return JSONResponse({"x": 1})

    return app


def test_a_response_with_its_own_policy_keeps_it() -> None:
    client = TestClient(_bare_app())
    own = client.get("/own")
    assert own.headers.get_list(HEADER) == ["default-src 'none'"]
    # A response that only reports brings no enforced policy: it gets the page policy.
    report = client.get("/own-report")
    assert report.headers.get_list(HEADER) == [POLICY]
    assert client.get("/page").headers.get_list(HEADER) == [POLICY]
    assert client.get("/upper").headers.get_list(HEADER) == [POLICY]
    assert HEADER not in client.get("/data").headers


# -- the Alpine build of a page ------------------------------------------------------------


def _alpine_scripts(body: str) -> list[str]:
    return re.findall(r'<script[^>]+src="(/static/alpine[^"?]*)', body)


@pytest.mark.parametrize(("pattern", "person"), _page_rows())
def test_every_page_loads_the_csp_build_only(client: TestClient, pattern: str, person: str) -> None:
    _sign_in(client, person)
    body = client.get(design_world.sample_path(pattern)).text
    assert _alpine_scripts(body) == ["/static/alpine-csp.min.js"], pattern
    assert body.count('<meta name="papaia-csp" content="strict">') == 1, pattern


def test_the_standard_build_is_not_part_of_the_image() -> None:
    dockerfile = (REPO / "docker" / "Dockerfile").read_text(encoding="utf-8")
    assert "alpine.min.js" not in dockerfile
    assert "alpinejs@" not in dockerfile
    assert "csp_baseline" not in dockerfile
    assert "alpine.min.js" not in (REPO / "scripts" / "design-tools.sh").read_text("utf-8")
    package = json.loads((REPO / "scripts" / "design" / "package.json").read_text("utf-8"))
    assert "alpinejs" not in package["dependencies"]
    assert not hasattr(csp, "converted_templates")
    assert not hasattr(csp, "strict_page")


def test_the_ratchet_is_replaced_by_the_zero_rule() -> None:
    assert not (REPO / "tests" / "csp_baseline.json").exists()
    assert not (REPO / "tests" / "test_csp_ratchet.py").exists()
    assert (REPO / "tests" / "test_csp_zero.py").is_file()


# -- htmx without eval and without script tags ------------------------------------------


def test_htmx_runs_without_eval_and_without_injected_styles(client: TestClient) -> None:
    body = client.get("/").text
    match = re.search(r'<meta name="htmx-config" content="([^"]+)">', body)
    assert match is not None
    config = json.loads(match.group(1).replace("&#34;", '"').replace("&quot;", '"'))
    assert config == {
        "allowEval": False,
        "allowScriptTags": False,
        "includeIndicatorStyles": False,
    }
    # The meta element precedes htmx, which reads it when it starts.
    assert body.index('name="htmx-config"') < body.index("htmx.min.js")


# htmx drops every script element of a fragment it swaps in (allowScriptTags: false), data
# islands included, so a partial a route renders for htmx carries its server values in a
# template island (partials/_csp.html, swapped_island), which htmx keeps.
_ROUTED_PARTIAL = re.compile(r'"(partials/[\w./-]+\.html)"')
_INCLUDE = re.compile(r'\{%-?\s*include\s+"([^"]+)"')
# `{% from "x.html" import a, b as c %}` and `{% import "x.html" as y %}`: the macros a partial
# pulls in are part of what htmx swaps in, so the check follows them like an include.
_FROM_IMPORT = re.compile(r'\{%-?\s*from\s+"([^"]+)"\s+import\s+(.+?)\s*-?%\}', re.S)
_MODULE_IMPORT = re.compile(r'\{%-?\s*import\s+"([^"]+)"\s+as\s+(\w+)')
_MACRO = re.compile(
    r"\{%-?\s*macro\s+(\w+)\s*\(.*?%\}(.*?)\{%-?\s*endmacro\s*-?%\}", re.S
)


def _routed_partials() -> list[str]:
    found: set[str] = set()
    for path in (REPO / "src" / "app").rglob("*.py"):
        found.update(_ROUTED_PARTIAL.findall(path.read_text(encoding="utf-8")))
    return sorted(name for name in found if (TEMPLATES / name).is_file())


def _imported_names(clause: str) -> dict[str, str]:
    """The local name of each macro of an import clause (`a, b as c` -> {a: a, c: b})."""
    names: dict[str, str] = {}
    for item in clause.replace("with context", "").replace("without context", "").split(","):
        parts = item.split()
        if parts:
            names[parts[-1]] = parts[0]
    return names


def _called(text: str, name: str) -> bool:
    return re.search(rf"(?<![\w.]){re.escape(name)}\s*\(", text) is not None


def _swapped_parts(
    name: str, read: Callable[[str], str] | None = None
) -> list[tuple[str, str | None, str]]:
    """Every piece of template a routed partial puts into its fragment: the partial, what it
    includes, and the body of every macro it calls from another template (`from ... import`,
    `import ... as`), followed through the macros those call in turn. The kit (pk/) is the
    kit's own and checked there. Items are (template, macro or None, source)."""
    read = read or (lambda part: (TEMPLATES / part).read_text(encoding="utf-8"))
    seen: list[tuple[str, str | None, str]] = []
    done: set[tuple[str, str | None]] = set()
    todo: list[tuple[str, str | None]] = [(name, None)]
    while todo:
        unit = todo.pop()
        template, macro = unit
        if unit in done or template.startswith("pk/"):
            continue
        done.add(unit)
        whole = read(template)
        macros = {m.group(1): m.group(2) for m in _MACRO.finditer(whole)}
        text = whole if macro is None else macros.get(macro, "")
        seen.append((template, macro, text))
        todo.extend((included, None) for included in _INCLUDE.findall(text))
        if macro is not None:
            todo.extend(
                (template, other)
                for other in macros
                if other != macro and _called(text, other)
            )
        for source, clause in _FROM_IMPORT.findall(whole):
            for local, original in _imported_names(clause).items():
                if _called(text, local):
                    todo.append((source, original))
        for source, alias in _MODULE_IMPORT.findall(whole):
            for called in re.findall(rf"(?<![\w.]){re.escape(alias)}\.(\w+)\s*\(", text):
                todo.append((source, called))
    return seen


def _carries_script(text: str) -> bool:
    markup = csp_scan.strip_jinja(text)
    return "<script" in markup.lower() or re.search(r"(?<![\w.])data_island\(", text) is not None


def test_routes_render_partials_for_htmx() -> None:
    partials = _routed_partials()
    for name in (
        "partials/audit_list.html",
        "partials/connection_list.html",
        "partials/embedding_body.html",
        "partials/tile_editor.html",
        "partials/upgrade_status.html",
    ):
        assert name in partials, name


@pytest.mark.parametrize("name", _routed_partials())
def test_a_partial_htmx_swaps_in_carries_no_script_element(name: str) -> None:
    for template, macro, text in _swapped_parts(name):
        assert not _carries_script(text), (name, template, macro)


_SEEDED = {
    "partials/routed.html": (
        '{% from "partials/helpers.html" import clean, writes as w %}\n'
        '{% import "partials/module.html" as mod %}\n'
        "<div>{{ clean() }}{{ w() }}{{ mod.deep() }}</div>"
    ),
    "partials/helpers.html": (
        '{% from "partials/_csp.html" import data_island %}\n'
        "{% macro clean() %}<p>fine</p>{% endmacro %}\n"
        "{% macro writes() %}{{ data_island('x', 1) }}{% endmacro %}\n"
        "{% macro unused() %}<script>alert(1)</script>{% endmacro %}"
    ),
    "partials/module.html": (
        "{% macro deep() %}{{ inner() }}{% endmacro %}\n"
        "{% macro inner() %}<script>alert(2)</script>{% endmacro %}"
    ),
}


def test_the_partial_check_follows_imported_macros() -> None:
    """A macro pulled in with `from ... import` or `import ... as` that writes a data island or
    a script element is found; a macro the partial does not call is not held against it."""
    def read(part: str) -> str:
        return _SEEDED.get(part) or (TEMPLATES / part).read_text(encoding="utf-8")

    parts = _swapped_parts("partials/routed.html", read=read)
    flagged = sorted((template, macro) for template, macro, text in parts if _carries_script(text))
    assert flagged == [
        ("partials/_csp.html", "data_island"),
        ("partials/helpers.html", "writes"),
        ("partials/module.html", "inner"),
    ]
    assert ("partials/helpers.html", "unused") not in {(t, m) for t, m, _ in parts}


def test_a_template_island_survives_the_swap_and_holds_the_value(client: TestClient) -> None:
    template = csp_templates.env.from_string(
        '{% from "partials/_csp.html" import swapped_island %}{{ swapped_island("x-state", v) }}'
    )
    hostile = "</template><script>alert(1)</script><!-- & ' \""
    html = template.render(v={"name": hostile})
    match = re.fullmatch(
        r'<template id="x-state" data-island="application/json">(.*)</template>', html
    )
    assert match is not None, html
    assert "<" not in match.group(1)
    assert json.loads(match.group(1)) == {"name": hostile}
    with pytest.raises(ValueError, match="island id"):
        csp_templates.env.from_string(
            '{% from "partials/_csp.html" import swapped_island %}{{ swapped_island("a b", 1) }}'
        ).render()


def test_the_indicator_styles_live_in_the_stylesheet() -> None:
    css = (REPO / "docker" / "tailwind.app.css").read_text(encoding="utf-8")
    assert ".htmx-indicator" in css
    assert ".htmx-request .htmx-indicator" in css


def test_no_template_uses_an_htmx_feature_that_needs_eval() -> None:
    for path in TEMPLATES.rglob("*.html"):
        source = path.read_text(encoding="utf-8")
        for trigger in re.findall(r'hx-trigger="([^"]*)"', source):
            # A CSS selector after `from:` may hold brackets; a filter is anything else in them.
            events = re.sub(r"from:\S+", "", trigger)
            assert "[" not in events, f"{path.name}: trigger filter in {trigger!r}"
        assert "hx-on" not in source, path.name
        assert not re.search(r'hx-(vals|vars)=["\']\s*js:', source), path.name


def test_the_widget_poll_pauses_through_the_core_listener(client: TestClient) -> None:
    source = (TEMPLATES / "partials" / "tile_gallery.html").read_text(encoding="utf-8")
    assert "visibilityState" not in source
    assert "data-poll-visible" in source
    body = client.get("/").text
    assert re.search(r'<script type="module" src="/static/js/core/main\.js[^"]*"></script>', body)
