"""The ingest lists without inline script (T-UI-03): what the pages hand their module.

Jobs, one job, Runs, one run, Leftovers and the job file run on the CSP build of Alpine. Their
script is static/js/pages/ingest.js; the server hands it values and sentences only through the
data island `ingest-page`, buttons name their verb in `data-ingest-action`, and a sentence with
markup is rendered here with its value filled in by `x-text`, never by `x-html`. Rendered
through the real application against the fake ingester of the page tests.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests import test_ingest_pages as pages
from tests.test_ingest_pages import Deployment, _admin, _job

# The fixtures of the page tests: the application against the fake ingester.
client = pages.client
deployment = pages.deployment

REPO = Path(__file__).resolve().parents[1]
PAGES_JS = REPO / "src" / "app" / "static" / "js" / "pages"
_ISLAND = re.compile(r'<script type="application/json" id="ingest-page">(.*?)</script>', re.DOTALL)
_PAGES = {
    "/ingest/jobs": "jobs",
    "/ingest/jobs/handbook": "job",
    "/ingest/runs": "runs",
    "/ingest/runs/{run_id}": "run",
    "/ingest/orphans": "orphans",
    "/ingest/catalog": "catalog",
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


def _paths(deployment: Deployment) -> dict[str, str]:
    deployment.write_catalog(_job())
    run_id = deployment.ingest.start_run("handbook")
    return {path.replace("{run_id}", run_id): page for path, page in _PAGES.items()}


def test_each_page_loads_its_module_before_alpine_on_the_csp_build(
    client: TestClient, deployment: Deployment
) -> None:
    for path, page in _paths(deployment).items():
        body = _body(client, path)
        assert '<meta name="papaia-csp" content="strict">' in body, path
        module = body.index('<script type="module" src="/static/js/pages/ingest.js')
        assert module < body.index("alpine-csp.min.js"), path
        assert "alpine.min.js" not in body, path
        assert _island(body)["page"] == page, path
        # No script the browser would execute is written into the page.
        for tag in re.findall(r"<script\b[^>]*>", body):
            assert "src=" in tag or 'type="application/json"' in tag, (path, tag)
        assert not re.search(r"\son[a-z]+=", body), path
        assert "x-html" not in body, path


def test_the_island_carries_every_sentence_the_components_read(
    client: TestClient, deployment: Deployment
) -> None:
    texts = _island(_body(client, "/ingest/jobs"))["texts"]
    assert isinstance(texts, dict)
    source = (PAGES_JS / "ingest-components.js").read_text(encoding="utf-8")
    used = set(re.findall(r"\btexts\.(\w+)", source))
    assert used and used <= set(texts), sorted(used - set(texts))
    # A counted noun is [one, other]; a sentence keeps its placeholders.
    assert texts["runCount"] == ["{count} run", "{count} runs"]
    assert texts["jobDeleted"] == "Job <b>{id}</b> deleted"


def test_the_values_of_a_page_reach_its_module_through_the_island(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.write_catalog(_job(), defaults={"embedding": {"model": "bge-m3"}})
    runs = _island(_body(client, "/ingest/runs"))["values"]
    assert runs == {"job_choices": ["handbook"], "tz": "Europe/Berlin"}
    catalog = _island(_body(client, "/ingest/catalog"))["values"]
    assert isinstance(catalog, dict)
    assert catalog["defaults"] == {"embedding": {"model": "bge-m3"}}
    assert catalog["raw"]["writable"] is True and "version: 1" in catalog["raw"]["text"]
    assert _island(_body(client, "/ingest/jobs/nope"))["page"] == "missing"


def test_a_value_from_the_catalog_never_becomes_markup_or_script(
    client: TestClient, deployment: Deployment
) -> None:
    nasty = "</script><script>alert(1)</script>"
    deployment.write_catalog(_job(), defaults={"embedding": {"model": nasty}})

    body = _body(client, "/ingest/catalog")

    assert "<script>alert(1)" not in body
    assert _island(body)["values"]["defaults"]["embedding"]["model"] == nasty  # type: ignore[index]


def test_the_rows_name_their_verbs_and_values(client: TestClient, deployment: Deployment) -> None:
    deployment.write_catalog(_job(), _job("paused", enabled=False))

    rows = _body(client, "/partials/ingest/jobs")

    assert '<a data-ingest-action="disable" data-job-id="handbook">' in rows
    assert '<a data-ingest-action="enable" data-job-id="paused">' in rows
    for chip in ("all", "attention", "active", "disabled"):
        assert f'data-chip="{chip}" data-ingest-action="chip"' in rows
    assert '<p id="ingest-none" hidden class=' in rows
    assert "ingestCloseMenu" not in rows


def test_the_job_page_switches_tabs_through_the_module(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.write_catalog(_job())

    body = _body(client, "/ingest/jobs/handbook")

    for tab in ("overview", "runs", "files", "config"):
        assert f'data-tab="{tab}" data-ingest-action="tab" data-tab-name="{tab}"' in body
    assert '<template id="ingest-failure" data-fallback="This tab could not be loaded.">' in body
    assert '<a data-ingest-action="disable" data-job-id="handbook">' in body


@pytest.mark.parametrize(
    ("sentence", "expression"),
    [
        ('Run <span class="font-mono"><span x-text="job.id"></span></span>', "job.id"),
        ('Delete <span class="font-mono"><span x-text="job.id"></span></span>?', "job.id"),
        ("<b>Its runs are deleted too.</b> <span x-text=\"runsText()\"></span>", "runsText()"),
        ('too: <span class="font-mono"><span x-text="job.siblings.join(\', \')"></span></span>.',
         "job.siblings.join(', ')"),
        (', counted in <span class="font-mono"><span x-text="tz"></span></span>', "tz"),
    ],
)  # fmt: skip
def test_a_sentence_with_markup_is_rendered_with_its_value_as_text(
    client: TestClient, deployment: Deployment, sentence: str, expression: str
) -> None:
    deployment.write_catalog(_job())
    body = html.unescape(_body(client, "/ingest/jobs"))
    assert sentence in body, expression


def test_the_leftovers_dialog_fills_its_values_as_text(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.ingest.orphans = [
        {"job_id": "old", "collection": "kb", "state_rows": 1, "points": 3},
    ]

    body = _body(client, "/partials/ingest/orphans")

    title = 'Remove what <span class="font-mono"><span x-text="item.job_id"></span></span> left?'
    assert title in body
    assert 'its points (<span x-text="pointsText()"></span>)' in body
    assert '<span class="font-mono"><span x-text="item.collection"></span></span>' in body
    assert "x-html" not in body and 'style="' not in body


def test_the_failure_panels_have_their_titles(client: TestClient, deployment: Deployment) -> None:
    expected = {
        "/ingest/jobs": ("The jobs could not be loaded.", "Ingest jobs are not available"),
        "/ingest/runs": ("The runs could not be loaded.", "Runs are not available"),
        "/ingest/orphans": ("The leftovers could not be loaded.", "Leftovers are not available"),
    }
    for path, (fallback, title) in expected.items():
        body = _body(client, path)
        assert f'<template id="ingest-failure" data-fallback="{fallback}">' in body, path
        assert f'<p class="text-lg font-semibold">{title}</p>' in body, path


def test_the_job_editor_imports_the_helpers_it_shares_with_the_lists(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.write_catalog(_job())

    body = _body(client, "/ingest/jobs/handbook/edit")

    # The copies of the request helpers that waited in the editor's inline script are gone; its
    # module imports them from the lists' components (tests/test_ingest_editor_scripts.py).
    assert "const _ING" not in body and "function ingestOutcome" not in body
    assert '<script type="module" src="/static/js/pages/ingest-editor.js' in body
