"""The strict content security policy of the manager in report-only mode (T-UI-03, oberfl 3.3).

Every HTML response carries the target policy as `Content-Security-Policy-Report-Only`;
enforcing it is a later step. A response with a policy of its own keeps it. `base.html` loads
exactly one Alpine build per page: the CSP build for the templates tests/csp_baseline.json
lists as converted, the standard build otherwise. htmx runs without `eval`, and the widget
poll of the dashboard pauses through a listener of the core modules instead of an htmx
trigger filter.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

_WORLD = Path(tempfile.mkdtemp(prefix="papaia-csp-policy-"))
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

import yaml  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import csp  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.core import module_catalog  # noqa: E402
from app.main import create_app  # noqa: E402

HEADER = "content-security-policy-report-only"
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
    for name, value in _directives(csp.POLICY).items():
        assert "unsafe-inline" not in value, name
        assert "unsafe-eval" not in value, name


@pytest.mark.parametrize(("pattern", "person"), _page_rows())
def test_every_page_carries_the_report_only_policy(
    client: TestClient, pattern: str, person: str
) -> None:
    client.cookies.set(
        design_world.SESSION_COOKIE,
        design_world.session_cookie(
            get_settings().manager_session_secret,
            roles=design_world.PERSONS[person],
            person=person,
        ),
    )
    response = client.get(design_world.sample_path(pattern))
    assert response.status_code == 200, pattern
    assert response.headers[HEADER] == POLICY
    # Report-only here: nothing is enforced yet.
    assert "content-security-policy" not in response.headers


def test_the_no_access_page_carries_the_policy(client: TestClient) -> None:
    client.cookies.set(
        design_world.SESSION_COOKIE,
        design_world.session_cookie(get_settings().manager_session_secret, roles=("user",)),
    )
    refused = client.get("/settings")
    assert refused.status_code == 403
    assert refused.headers[HEADER] == POLICY


def test_partials_loaded_into_a_page_carry_the_policy(client: TestClient) -> None:
    response = client.get("/partials/service-status")
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers[HEADER] == POLICY


def test_json_responses_get_no_page_policy(client: TestClient) -> None:
    response = client.get("/health")
    assert response.headers["content-type"].startswith("application/json")
    assert HEADER not in response.headers


def _bare_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(csp.ReportOnlyPolicyMiddleware)

    @app.get("/own")
    async def own() -> HTMLResponse:
        return HTMLResponse("<p>x</p>", headers={"Content-Security-Policy": "default-src 'none'"})

    @app.get("/own-report")
    async def own_report() -> HTMLResponse:
        return HTMLResponse(
            "<p>x</p>", headers={"Content-Security-Policy-Report-Only": "default-src 'none'"}
        )

    @app.get("/page")
    async def page() -> HTMLResponse:
        return HTMLResponse("<p>x</p>")

    @app.get("/data")
    async def data() -> JSONResponse:
        return JSONResponse({"x": 1})

    return app


def test_a_response_with_its_own_policy_keeps_it() -> None:
    client = TestClient(_bare_app())
    own = client.get("/own")
    assert own.headers["content-security-policy"] == "default-src 'none'"
    assert HEADER not in own.headers
    report = client.get("/own-report")
    assert report.headers.get_list(HEADER) == ["default-src 'none'"]
    assert client.get("/page").headers[HEADER] == POLICY
    assert HEADER not in client.get("/data").headers


# -- the Alpine build of a page ------------------------------------------------------------


def _alpine_scripts(body: str) -> list[str]:
    return re.findall(r'<script[^>]+src="(/static/alpine[^"?]*)', body)


def test_a_page_not_converted_loads_the_standard_build_only(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(csp, "converted_templates", lambda: frozenset({"jobs.html"}))
    assert _alpine_scripts(client.get("/").text) == ["/static/alpine.min.js"]


def test_a_converted_page_loads_the_csp_build_only(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(csp, "converted_templates", lambda: frozenset({"dashboard.html"}))
    assert _alpine_scripts(client.get("/").text) == ["/static/alpine-csp.min.js"]
    # Another page of the same process stays on the standard build.
    assert _alpine_scripts(client.get("/jobs").text) == ["/static/alpine.min.js"]


def test_the_converted_list_is_read_from_the_baseline() -> None:
    doc = json.loads((REPO / "tests" / "csp_baseline.json").read_text(encoding="utf-8"))
    assert csp.converted_templates() == frozenset(doc["converted"])


# -- htmx without eval ---------------------------------------------------------------------


def test_htmx_runs_without_eval_and_without_injected_styles(client: TestClient) -> None:
    body = client.get("/").text
    match = re.search(r'<meta name="htmx-config" content="([^"]+)">', body)
    assert match is not None
    config = json.loads(match.group(1).replace("&#34;", '"').replace("&quot;", '"'))
    assert config == {"allowEval": False, "includeIndicatorStyles": False}
    # The meta element precedes htmx, which reads it when it starts.
    assert body.index('name="htmx-config"') < body.index("htmx.min.js")


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
