"""The job editor and the credentials page without inline script (T-UI-03).

Both pages run on the CSP build of Alpine. Their script is static/js/pages/ingest-editor.js
with the components of static/js/pages/ingest-editor-components.js; the server hands it the
editor's start values and the sentences only through the data island `ingest-editor`, buttons
of the page frame name their verb in `data-ingest-editor-action`, and a sentence with markup is
rendered here with its value filled in by `x-text`, never by `x-html`. Rendered through the
real application against the fake ingester of the page tests.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

from tests import test_ingest_pages as pages
from tests.test_ingest_pages import Deployment, _admin, _job

# The fixtures of the page tests: the application against the fake ingester.
client = pages.client
deployment = pages.deployment

REPO = Path(__file__).resolve().parents[1]
PAGES_JS = REPO / "src" / "app" / "static" / "js" / "pages"
_ISLAND = re.compile(
    r'<script type="application/json" id="ingest-editor">(.*?)</script>', re.DOTALL
)
_PAGES = {
    "/ingest/new": "editor",
    "/ingest/new?duplicate=handbook": "editor",
    "/ingest/jobs/handbook/edit": "editor",
    "/ingest/secrets": "secrets",
}


def _body(client: TestClient, path: str) -> str:
    response = _admin(client).get(path)
    assert response.status_code == 200, (path, response.status_code)
    return response.text


def _island(body: str) -> dict[str, object]:
    found = _ISLAND.findall(body)
    assert len(found) == 1, "one data island per page"
    value: dict[str, object] = json.loads(found[0])
    return value


def test_each_page_loads_its_module_before_alpine_on_the_csp_build(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.write_catalog(_job())
    for path, page in _PAGES.items():
        body = _body(client, path)
        assert '<meta name="papaia-csp" content="strict">' in body, path
        module = body.index('<script type="module" src="/static/js/pages/ingest-editor.js')
        assert module < body.index("alpine-csp.min.js"), path
        assert "alpine.min.js" not in body, path
        assert _island(body)["page"] == page, path
        # No script the browser would execute is written into the page.
        for tag in re.findall(r"<script\b[^>]*>", body):
            assert "src=" in tag or 'type="application/json"' in tag, (path, tag)
        assert not re.search(r"\son[a-z]+=", body), path
        assert "x-html" not in body, path
        assert 'style="' not in body, path


def test_the_credentials_list_carries_no_script_handler_or_markup_binding(
    client: TestClient, deployment: Deployment
) -> None:
    from app.core.ingest.secrets import SecretsRepository

    SecretsRepository(deployment.config_dir, "s").set("QI_SECRET_DAV", "value")

    body = _body(client, "/partials/ingest/secrets")

    for tag in re.findall(r"<script\b[^>]*>", body):
        assert 'type="application/json"' in tag, tag
    assert not re.search(r"\son[a-z]+=", body)
    assert "x-html" not in body and 'style="' not in body
    title = 'Delete <span class="font-mono"><span x-text="name"></span></span>?'
    assert title in html.unescape(body)


def test_the_island_carries_every_sentence_the_components_read(
    client: TestClient, deployment: Deployment
) -> None:
    texts = _island(_body(client, "/ingest/new"))["texts"]
    assert isinstance(texts, dict)
    used: set[str] = set()
    for name in ("ingest-editor-components.js", "ingest-editor.js"):
        source = (PAGES_JS / name).read_text(encoding="utf-8")
        used |= set(re.findall(r"\btexts\.(\w+)", source))
    # ingestOutcome() of the lists reads its four sentences from the same texts.
    lists = (PAGES_JS / "ingest-components.js").read_text(encoding="utf-8")
    outcome = lists[lists.index("export function ingestOutcome") :]
    used |= set(re.findall(r"\btexts\.(\w+)", outcome[: outcome.index("\n}\n")]))
    assert used and used <= set(texts), sorted(used - set(texts))
    # A counted noun is [one, other]; a sentence keeps its placeholders.
    assert texts["problemsToFix"] == ["{count} problem to fix", "{count} problems to fix"]
    assert texts["jobSaved"] == "Job <b>{id}</b> saved"
    assert texts["credentialStored"] == "Credential <b>{name}</b> stored"
    assert [mode["value"] for mode in texts["scheduleModes"]] == [  # type: ignore[index]
        "manual", "interval", "hourly", "daily", "weekly", "monthly", "cron",
    ]  # fmt: skip
    secrets = _island(_body(client, "/ingest/secrets"))
    assert secrets["values"] == {}
    assert secrets["texts"] == texts


def test_the_start_values_of_the_editor_reach_its_module_through_the_island(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.write_catalog(_job(description="Docs"))

    body = _body(client, "/ingest/jobs/handbook/edit")

    boot = _island(body)["values"]
    assert isinstance(boot, dict)
    assert boot["isNew"] is False and boot["originalId"] == "handbook" and boot["etag"]
    assert boot["state"]["description"] == "Docs"
    assert 'x-data="jobEditor()"' in body
    assert "jobEditor(" not in body.replace('x-data="jobEditor()"', "")


def test_a_value_from_the_catalog_never_becomes_markup_or_script(
    client: TestClient, deployment: Deployment
) -> None:
    nasty = "</script><script>alert(1)</script>"
    deployment.write_catalog(_job(description=nasty))

    body = _body(client, "/ingest/jobs/handbook/edit")

    assert "<script>alert(1)" not in body
    assert _island(body)["values"]["state"]["description"] == nasty  # type: ignore[index]


def test_the_buttons_of_the_frame_name_their_verbs(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.write_catalog(_job())

    editor = _body(client, "/ingest/jobs/handbook/edit")
    assert 'data-ingest-editor-action="save"' in editor

    secrets = _body(client, "/ingest/secrets")
    assert 'data-ingest-editor-action="refresh"' in secrets
    assert 'data-ingest-editor-action="credential-new"' in secrets
    # The list reloads from the module when a credential was stored, not from an attribute.
    assert "reloadSecrets" not in secrets
    assert (
        '<template id="ingest-failure" data-fallback="The credentials could not be loaded.">'
        in secrets
    )
    assert '<p class="text-lg font-semibold">Credentials are not available</p>' in secrets


def test_the_editor_needs_no_global_of_the_page_script(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.write_catalog(_job())
    for path in ("/ingest/jobs/handbook/edit", "/ingest/secrets"):
        body = _body(client, path)
        for name in ("fmtPlural(", "fmt(", "fmtHtml(", "_ragRequest", "_ragEscape", "showToast"):
            assert name not in body, (path, name)
    module = (PAGES_JS / "ingest-editor-components.js").read_text(encoding="utf-8")
    # The helpers the editor shared with the lists are imported, not copied.
    assert 'from "./ingest-components.js"' in module
    assert "const _ING" not in module and "function ingestOutcome" not in module
