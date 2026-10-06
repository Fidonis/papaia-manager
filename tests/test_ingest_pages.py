"""The ingest pages: that they render, what they say in each state, and what they never do.

Through the real application against a fake ingester, so a template that cannot be rendered
(a missing variable, a macro imported in a circle, a dict called `items`) fails here and not in a
browser. The states are the point: a down ingester, a catalog the ingester refused, an ingester
without the newer features, a run that works and one that failed.
"""
from __future__ import annotations

import base64
import json
import os
import re
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

_CONFIG_DIR = tempfile.mkdtemp(prefix="papaia-pages-config-")
_WORKSPACE_DIR = tempfile.mkdtemp(prefix="papaia-pages-workspace-")

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

from app.config import get_settings  # noqa: E402
from app.core.ingest import catalog, runs  # noqa: E402
from app.core.vectordb.ingest_file import CONNECTIONS_RELPATH  # noqa: E402
from app.main import create_app  # noqa: E402
from app.routers.rag_deps import get_http_transport  # noqa: E402
from tests.fake_ingest import TOKEN, FakeIngest  # noqa: E402
from tests.fake_qdrant import API_KEY, FakeQdrant, Fleet  # noqa: E402

_CSRF = "test-csrf-token-value"


class Deployment:
    def __init__(self, config_dir: Path) -> None:
        self.config_dir = config_dir
        self.ingest = FakeIngest(config_dir)
        self.qdrant = FakeQdrant(API_KEY)
        self.qdrant.add("kb", size=4)
        self.fleet = Fleet()
        self.fleet.add("qdrant-ingest", self.ingest)
        self.fleet.add("qdrant.test", self.qdrant)

    @property
    def jobs_path(self) -> Path:
        return self.config_dir / catalog.JOBS_RELPATH

    def write_catalog(self, *jobs: dict[str, Any], defaults: dict[str, Any] | None = None) -> None:
        document: dict[str, Any] = {"version": 1}
        if defaults:
            document["defaults"] = defaults
        document["jobs"] = list(jobs)
        self.jobs_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        self.ingest.load_catalog()


@pytest.fixture
def deployment(tmp_path: Path) -> Deployment:
    config_dir = tmp_path / "config"
    (config_dir / "manager").mkdir(parents=True)
    (config_dir / CONNECTIONS_RELPATH.parent).mkdir(parents=True, exist_ok=True)
    (config_dir / ".env").write_text("COMPOSE_PROFILES=keycloak,rag\n", encoding="utf-8")
    (config_dir / "ai" / "rag" / ".env").write_text(
        f"QI_CONNECTIONS_SECRET=s\nQI_API_TOKEN={TOKEN}\nQI_TIMEZONE=Europe/Berlin\n"
        f"QDRANT_JWT_SECRET={API_KEY}\nQI_SECRET_FROM_ENV=x\n",
        encoding="utf-8",
    )
    (config_dir / CONNECTIONS_RELPATH).write_text(
        yaml.safe_dump({"version": 1, "connections": [{"name": "default", "url": "http://q:6333"}]}),
        encoding="utf-8",
    )
    return Deployment(config_dir)


@pytest.fixture
def client(deployment: Deployment, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    async def idle(_kind: object) -> None:
        return None

    monkeypatch.setattr(runs.runner, "find_runner", idle)
    get_settings.cache_clear()
    settings = get_settings().model_copy(
        update={
            "papaia_config_dir": str(deployment.config_dir),
            "qdrant_url": "http://qdrant.test:6333",
            "qdrant_ingest_url": "http://qdrant-ingest:8300",
        }
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_http_transport] = lambda: deployment.fleet.transport()
    yield TestClient(app, follow_redirects=False)
    get_settings.cache_clear()


def _as(client: TestClient, *roles: str) -> TestClient:
    session: dict[str, Any] = {
        "user": {
            "sub": "u-1",
            "preferred_username": "Tester",
            "roles": list(roles),
            "exp": int(time.time()) + 3600,
        },
        "_csrf_token": _CSRF,
    }
    payload = base64.b64encode(json.dumps(session).encode())
    signed = TimestampSigner(get_settings().manager_session_secret).sign(payload).decode()
    client.cookies.clear()
    client.cookies.set("papaia_manager_session", signed)
    return client


def _admin(client: TestClient) -> TestClient:
    return _as(client, "admin")


def _job(job_id: str = "handbook", **overrides: Any) -> dict[str, Any]:
    job: dict[str, Any] = {
        "id": job_id,
        "source": {"type": "local", "label": job_id, "path": f"/data/local/{job_id}"},
        "target": {"collection": "kb", "connection": "default"},
        "mode": "upsert",
        "embedding": {"model": "nomic-embed-text"},
    }
    job.update(overrides)
    return job


def _body(client: TestClient, path: str, **kwargs: Any) -> str:
    response = _admin(client).get(path, **kwargs)
    assert response.status_code == 200, f"{path}: {response.status_code} {response.text[:300]}"
    return response.text


# ---------------------------------------------------------------------------
# Who may see what
# ---------------------------------------------------------------------------

_PAGES = [
    "/ingest/jobs",
    "/ingest/new",
    "/ingest/jobs/handbook",
    "/ingest/jobs/handbook/edit",
    "/ingest/runs",
    "/ingest/runs/some-run",
    "/ingest/secrets",
    "/ingest/orphans",
    "/ingest/catalog",
    "/partials/ingest/jobs",
    "/partials/ingest/jobs/handbook/overview",
    "/partials/ingest/jobs/handbook/runs",
    "/partials/ingest/jobs/handbook/files",
    "/partials/ingest/jobs/handbook/config",
    "/partials/ingest/preview/handbook",
    "/partials/ingest/runs",
    "/partials/ingest/runs/some-run",
    "/partials/ingest/secrets",
    "/partials/ingest/orphans",
]


@pytest.mark.parametrize("path", _PAGES)
def test_every_page_needs_an_administrator(client: TestClient, path: str) -> None:
    assert client.get(path).status_code in (307, 401)
    assert _as(client, "user").get(path).status_code == 403


@pytest.mark.parametrize("path", _PAGES[:9])
def test_every_page_renders_for_an_administrator_even_when_empty(
    client: TestClient, path: str
) -> None:
    response = _admin(client).get(path)

    assert response.status_code == 200, response.text[:400]
    assert "Ingest" in response.text


def test_the_pages_are_not_there_when_the_rag_system_is_off(
    client: TestClient, deployment: Deployment
) -> None:
    (deployment.config_dir / ".env").write_text("COMPOSE_PROFILES=keycloak\n", encoding="utf-8")

    assert _admin(client).get("/ingest/jobs").status_code == 404


def _nav(body: str) -> dict[str, list[str]]:
    nav = body[body.index("<nav") : body.index("</nav>")]
    parts = re.split(r'<p class="sidebar-label[^>]*>([^<]+)</p>', nav)
    groups = {"": re.findall(r'aria-label="([^"]+)"', parts[0])}
    for caption, chunk in zip(parts[1::2], parts[2::2], strict=True):
        groups[caption] = re.findall(r'aria-label="([^"]+)"', chunk)
    return groups


def test_the_sidebar_lights_the_right_entry(client: TestClient) -> None:
    def active(path: str) -> list[str]:
        body = _body(client, path)
        return re.findall(r'aria-label="([^"]+)"\s+class="[^"]*bg-secondary/15', body)

    assert active("/ingest/jobs") == ["Ingest Jobs"]
    assert active("/ingest/secrets") == ["Ingest Jobs"]
    assert active("/ingest/new") == ["Ingest Jobs"]
    assert active("/ingest/runs") == ["Ingest Runs"]
    assert active("/ingest/runs/abc") == ["Ingest Runs"]
    # The manager's own queue is another page and does not light these.
    assert "Ingest Jobs" not in active("/jobs")


# ---------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------


def test_an_empty_list_invites_the_first_job(client: TestClient) -> None:
    body = _body(client, "/partials/ingest/jobs")

    assert "No ingest jobs yet" in body and 'href="/ingest/new"' in body


def test_a_job_is_a_row_with_its_state_its_actions_and_no_polling(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.write_catalog(_job(schedule={"cron": "0 3 * * *"}, description="The handbook"))

    body = _body(client, "/partials/ingest/jobs")

    assert 'href="/ingest/jobs/handbook"' in body and "The handbook" in body
    assert "Every day at 03:00" in body and "Keep in sync" in body
    assert 'data-group="active"' in body
    assert "ingest-run" in body and 'href="/ingest/jobs/handbook/edit"' in body
    assert 'hx-trigger="every 3s"' not in body, "nothing works, so nothing polls"
    assert "Never run" in body


def test_the_list_polls_itself_only_while_a_run_works(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.write_catalog(_job())
    run_id = deployment.ingest.start_run("handbook")
    deployment.ingest.progress(run_id, phase="embedding", files_seen=10, files_done=4)

    body = _body(client, "/partials/ingest/jobs")

    assert 'hx-trigger="every 3s"' in body
    assert "Reading and embedding the files" in body
    assert '<progress class="progress progress-info w-full" value="40"' in body

    deployment.ingest.finish(run_id, docs_indexed=10, files_seen=10)
    assert 'hx-trigger="every 3s"' not in _body(client, "/partials/ingest/jobs")


def test_a_job_the_ingester_dropped_says_why_and_the_catalog_banner_explains(
    client: TestClient, deployment: Deployment
) -> None:
    broken = _job("broken", target={"collection": "x", "connection": "gone"})
    deployment.write_catalog(_job(), broken)
    deployment.ingest.jobs.pop("broken")
    deployment.ingest.catalog_errors = [
        {"job_id": "broken", "field": "target.connection", "message": "unknown connection 'gone'"},
        {"job_id": None, "field": "defaults", "message": "unsupported sections: surprise"},
    ]
    deployment.ingest.applied = False

    body = _body(client, "/partials/ingest/jobs")

    assert "Not loaded" in body and "target.connection: unknown connection &#39;gone&#39;" in body
    assert "keeps its previous catalog" in body
    assert "unsupported sections: surprise" in body
    assert "Running its previous version" in body
    assert 'data-chip="attention"' in body and 'data-group="attention"' in body


def test_a_down_ingester_is_a_banner_and_the_file_still_lists(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.write_catalog(_job())
    deployment.ingest.down = True

    body = _body(client, "/partials/ingest/jobs")

    assert "The ingester cannot be reached" in body and "handbook" in body


def test_a_refused_token_has_its_own_banner(client: TestClient, deployment: Deployment) -> None:
    deployment.write_catalog(_job())
    deployment.ingest.token = "another"

    body = _body(client, "/partials/ingest/jobs")

    assert "cannot use it" in body and "refused the API token" in body


def test_leftovers_and_unreachable_dependencies_are_flagged(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.write_catalog(_job())
    deployment.ingest.orphans = [
        {"job_id": "old", "collection": "kb", "state_rows": 1, "points": 2}
    ]
    deployment.ingest.deps = {"qdrant": True, "embeddings": False, "tika": True}

    body = _body(client, "/partials/ingest/jobs")

    assert 'href="/ingest/orphans"' in body and "cannot reach <b>embeddings</b>" in body


def test_a_job_of_the_embedding_page_has_no_edit_or_run_and_says_where_it_comes_from(
    client: TestClient, deployment: Deployment
) -> None:
    managed = catalog.managed_job_id("default", "kb", "upload")
    deployment.write_catalog(_job(managed))

    body = _body(client, "/partials/ingest/jobs")

    assert "Embedding page" in body
    assert f'href="/ingest/jobs/{managed}/edit"' not in body
    assert "Run</button>" not in body


def test_what_a_job_says_about_itself_cannot_break_the_page(
    client: TestClient, deployment: Deployment
) -> None:
    nasty = '<script>alert(1)</script>"\'</script>'
    deployment.write_catalog(_job(description=nasty))

    for path in ("/partials/ingest/jobs", "/partials/ingest/jobs/handbook/config"):
        assert "<script>alert(1)" not in _body(client, path), path
    edit = _body(client, "/ingest/jobs/handbook/edit")
    assert "<script>alert(1)" not in edit and "</script><script>" not in edit


# ---------------------------------------------------------------------------
# One job
# ---------------------------------------------------------------------------


def test_the_job_page_has_its_tabs_and_loads_the_first(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.write_catalog(_job())

    body = _body(client, "/ingest/jobs/handbook")

    for tab in ("overview", "runs", "files", "config"):
        assert f'data-tab="{tab}"' in body
    assert 'href="/ingest/jobs/handbook/edit"' in body and "Duplicate" in body
    assert "const _JOB = &#34;handbook&#34;" in body or 'const _JOB = "handbook"' in body


def test_the_first_tab_starts_after_the_deferred_htmx_script_has_run(
    client: TestClient, deployment: Deployment
) -> None:
    """htmx is a deferred script; a call made while the page is parsed finds no `htmx`."""
    deployment.write_catalog(_job())

    body = _body(client, "/ingest/jobs/handbook")

    assert re.search(r'<script src="[^"]*htmx[^"]*" defer>', body)
    start = body.index("document.addEventListener('DOMContentLoaded'")
    assert body.index("jobTab(hash || 'overview'") > start
    assert "\njobTab(" not in body, "a bare call would run before htmx exists"


def test_an_unknown_job_is_a_page_that_says_so(client: TestClient) -> None:
    body = _body(client, "/ingest/jobs/nope")

    assert "There is no job" in body


def test_the_overview_says_what_the_job_does_and_how_its_last_run_went(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.write_catalog(
        _job(
            schedule={"every": "6h"},
            filters={"include": ["**/*.pdf"], "exclude": ["**/drafts/**"]},
            target={"collection": "kb", "connection": "default", "acl_tags": ["dept:hr"]},
        )
    )
    run_id = deployment.ingest.start_run("handbook")
    deployment.ingest.finish(run_id, docs_indexed=3, docs_failed=1, files_seen=4)
    deployment.ingest.documents["handbook"] = [
        {"rel_path": "a.pdf", "status": "indexed", "chunk_count": 5, "source": "s"}
    ]

    body = _body(client, "/partials/ingest/jobs/handbook/overview")

    assert "Every 6 hours" in body and "**/*.pdf" in body and "**/drafts/**" in body
    assert "nomic-embed-text" in body and "dept:hr" in body
    assert "Finished with errors" in body
    assert "See the files that failed" in body
    assert "1 file tracked" in body


def test_the_overview_polls_while_the_run_works_and_shows_the_file_in_hand(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.write_catalog(_job())
    run_id = deployment.ingest.start_run("handbook")
    deployment.ingest.progress(
        run_id, phase="embedding", files_seen=20, files_done=5, current="docs/guide.pdf"
    )

    body = _body(client, "/partials/ingest/jobs/handbook/overview")

    assert 'hx-trigger="every 3s"' in body
    assert "5 of 20 files" in body and "docs/guide.pdf" in body


def test_a_disabled_job_says_what_that_means(client: TestClient, deployment: Deployment) -> None:
    deployment.write_catalog(_job(enabled=False))

    body = _body(client, "/partials/ingest/jobs/handbook/overview")

    assert "This job is disabled" in body


def test_the_runs_tab_lists_the_runs_of_the_job(client: TestClient, deployment: Deployment) -> None:
    other = _job("other", target={"collection": "kb2", "connection": "default"})
    deployment.write_catalog(_job(), other)
    first = deployment.ingest.start_run("handbook")
    deployment.ingest.finish(first, files_seen=2, docs_indexed=2)
    deployment.ingest.start_run("other")

    body = _body(client, "/partials/ingest/jobs/handbook/runs")

    assert f"/ingest/runs/{first}" in body
    assert 'href="/ingest/jobs/other"' not in body


def test_the_files_tab_filters_and_pages_and_explains_a_missing_feature(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.write_catalog(_job())
    deployment.ingest.documents["handbook"] = [
        {"rel_path": f"f{i}.md", "status": "indexed", "chunk_count": 1, "source": "s",
         "indexed_at": "2026-10-05T10:00:00+00:00"}
        for i in range(60)
    ] + [
        {"rel_path": "broken.pdf", "status": "failed_extract", "chunk_count": 0, "source": "b",
         "last_error": "Tika answered <500>", "indexed_at": "2026-10-05T10:00:00+00:00"}
    ]

    tab = _body(client, "/partials/ingest/jobs/handbook/files")
    assert 'id="files-table"' in tab and "Could not be read 1" in tab and "Next" in tab

    only = _body(client, "/partials/ingest/jobs/handbook/files?rows=1&status=failed_extract")
    assert 'id="files-table"' not in only, "a filter change swaps the rows only"
    assert "broken.pdf" in only and "Tika answered &lt;500&gt;" in only

    empty = _body(client, "/partials/ingest/jobs/handbook/files?rows=1&q=nothing-like-this")
    assert "No file matches" in empty

    deployment.ingest.features = []
    old = _body(client, "/partials/ingest/jobs/handbook/files")
    assert "does not report what happened to each file" in old


def test_the_configuration_tab_shows_references_never_values(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.write_catalog(
        _job(
            source={
                "type": "webdav",
                "label": "cloud",
                "url": "https://cloud.test",
                "pass": "${env:QI_SECRET_DAV}",
            }
        ),
        defaults={"chunking": {"words": 300}},
    )

    body = _body(client, "/partials/ingest/jobs/handbook/config")

    assert "${env:QI_SECRET_DAV}" in body
    assert "Inherited from the catalog defaults" in body and "words: 300" in body


def test_the_preview_lists_files_or_says_why_there_are_none(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.write_catalog(_job())
    assert "The filters match no file" in _body(client, "/partials/ingest/preview/handbook")

    deployment.ingest.preview_files["handbook"] = [
        {"rel_path": "a/b.md", "source": "s", "size": 2048}
    ]
    body = _body(client, "/partials/ingest/preview/handbook")

    assert "a/b.md" in body and "2.0 KB" in body


# ---------------------------------------------------------------------------
# The editor
# ---------------------------------------------------------------------------


def _boot(body: str) -> dict[str, Any]:
    """The JSON the editor starts from, as the browser would read it."""
    import html

    match = re.search(r"x-data='jobEditor\((.*?)\)'\s", body, re.S)
    assert match, "the editor has no boot data"
    return json.loads(html.unescape(match.group(1)))  # type: ignore[no-any-return]


def test_a_new_job_opens_a_blank_editor_with_everything_it_needs(client: TestClient) -> None:
    body = _body(client, "/ingest/new")
    boot = _boot(body)

    assert boot["isNew"] is True and boot["state"]["id"] == ""
    assert len(boot["sources"]) == 9 and boot["connections"] == ["default"]
    assert {c["name"] for c in boot["credentials"]} == {"QI_SECRET_FROM_ENV"}
    assert [m["value"] for m in boot["modes"]] == ["append", "upsert", "full"]
    assert boot["timezone"] == "Europe/Berlin"
    assert "Where do the files come from?" in body and "When?" in body


def test_a_select_with_generated_options_is_set_again_once_they_exist(client: TestClient) -> None:
    """x-model fills a select before x-for has made its options, so it would show the first one."""
    body = _body(client, "/ingest/new")

    selects = re.findall(r"<select\b(.*?)</select>", body, re.S)
    generated = [s for s in selects if "x-for" in s]

    assert len(generated) >= 5
    for select in generated:
        assert 'x-effect="sync($el,' in select.split("<template", 1)[0], select[:200]


def test_leaving_the_editor_with_unsaved_changes_asks_in_an_app_dialog(
    client: TestClient, deployment: Deployment
) -> None:
    """A link out of a dirty editor opens the app's own dialog, not only the browser's prompt."""
    deployment.write_catalog(_job())

    for path in ("/ingest/new", "/ingest/jobs/handbook/edit"):
        body = _body(client, path)

        dialog = re.search(r'<dialog id="leave-editor".*?</dialog>', body, re.S)
        assert dialog, path
        assert "Discard your changes?" in dialog.group(0)
        assert "Keep editing" in dialog.group(0) and "Discard changes" in dialog.group(0)
        assert "guardLink(event)" in body and "$refs.leave.showModal()" in body
        # The browser's prompt stays as the last resort: a reload, a closed tab, the back button.
        assert "addEventListener('beforeunload'" in body


def test_an_existing_job_opens_with_its_values_and_its_etag(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.write_catalog(
        _job(description="Docs", schedule={"cron": "0 4 * * mon"}),
        defaults={"chunking": {"words": 512, "overlap": 64}},
    )

    boot = _boot(_body(client, "/ingest/jobs/handbook/edit"))

    assert boot["isNew"] is False and boot["originalId"] == "handbook" and boot["etag"]
    assert boot["state"]["description"] == "Docs"
    assert boot["state"]["schedule"]["mode"] == "weekly"
    assert boot["state"]["chunking"]["words"] == 512, "the effective value, not an empty field"
    assert boot["defaults"]["chunking.words"] == 512


def test_a_copy_starts_without_an_id_a_label_or_an_etag(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.write_catalog(_job(description="Docs"))

    boot = _boot(_body(client, "/ingest/new?duplicate=handbook"))

    assert boot["isNew"] is True and boot["etag"] == ""
    assert boot["state"]["id"] == "" and boot["state"]["source"]["label"] == ""
    assert boot["state"]["description"] == "Docs"


def test_a_job_of_the_embedding_page_is_never_edited(
    client: TestClient, deployment: Deployment
) -> None:
    managed = catalog.managed_job_id("default", "kb", "upload")
    deployment.write_catalog(_job(managed))

    response = _admin(client).get(f"/ingest/jobs/{managed}/edit")

    assert response.status_code == 303 and response.headers["location"] == f"/ingest/jobs/{managed}"


def test_a_job_that_is_not_in_the_file_has_an_editor_that_says_so(client: TestClient) -> None:
    assert "is not in jobs.yaml" in _body(client, "/ingest/jobs/ghost/edit")


def test_a_file_that_cannot_be_read_blocks_the_editor(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.jobs_path.write_text("jobs: [unterminated", encoding="utf-8")

    assert "This job cannot be edited" in _body(client, "/ingest/new")


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


def test_the_runs_list_shows_status_job_and_progress_and_polls_while_one_works(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.write_catalog(_job())
    done = deployment.ingest.start_run("handbook")
    deployment.ingest.finish(done, files_seen=3, docs_indexed=3)
    working = deployment.ingest.start_run("handbook")
    deployment.ingest.progress(working, phase="syncing")

    body = _body(client, "/partials/ingest/runs?job=handbook&status=")

    assert f"/ingest/runs/{done}" in body and f"/ingest/runs/{working}" in body
    assert "Fetching the files from the source" in body
    assert 'hx-trigger="every 3s"' in body
    assert 'hx-get="/partials/ingest/runs?job=handbook"' in body, "the poll keeps the filter"


def test_a_filter_narrows_the_runs_and_an_empty_list_explains(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.write_catalog(_job())
    ok = deployment.ingest.start_run("handbook")
    deployment.ingest.finish(ok, files_seen=1)
    bad = deployment.ingest.start_run("handbook")
    deployment.ingest.finish(bad, "failed", error="boom")

    failed = _body(client, "/partials/ingest/runs?status=failed")

    assert f"/ingest/runs/{bad}" in failed and f"/ingest/runs/{ok}" not in failed
    assert "No runs" in _body(client, "/partials/ingest/runs?job=nobody")


def test_a_dry_run_is_marked_in_the_list_and_on_its_page(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.write_catalog(_job())
    run_id = deployment.ingest.start_run("handbook")
    deployment.ingest.progress(run_id, dry_run=True)
    deployment.ingest.finish(run_id, files_seen=2, docs_indexed=2)

    assert "Dry run" in _body(client, "/partials/ingest/runs")
    detail = _body(client, f"/partials/ingest/runs/{run_id}")
    assert "This was a dry run" in detail and "Would embed" in detail


def test_a_working_run_shows_progress_the_file_in_hand_and_an_abort(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.write_catalog(_job())
    run_id = deployment.ingest.start_run("handbook")
    deployment.ingest.progress(run_id, phase="embedding", files_seen=40, files_done=10,
                               current="a/b.md")

    body = _body(client, f"/partials/ingest/runs/{run_id}")

    assert f'hx-get="/partials/ingest/runs/{run_id}"' in body and 'hx-trigger="every 2s"' in body
    assert "10 of 40 files" in body and "a/b.md" in body
    assert f"ingestAbort('{run_id}'" in body and "takes effect between two files" in body


def test_an_ingester_without_progress_says_when_the_figures_arrive(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.ingest.features = []
    deployment.write_catalog(_job())
    run_id = deployment.ingest.start_run("handbook")

    body = _body(client, f"/partials/ingest/runs/{run_id}")

    assert "reports its counts when a run ends" in body
    assert '<progress class="progress progress-info w-full"></progress>' in body


def test_a_failed_fetch_shows_the_tools_output_escaped_and_a_way_forward(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.write_catalog(_job())
    run_id = deployment.ingest.start_run("handbook")
    deployment.ingest.finish(
        run_id,
        "failed",
        error="sync failed with exit code 1",
        events=[{"level": "error", "source": "sync", "message": "sync failed", "ts": "t"}],
    )
    deployment.ingest.runs[run_id]["sync_status"] = "failed"
    deployment.ingest.runs[run_id]["sync_stderr_tail"] = "ERROR: <script>alert(1)</script> denied"

    body = _body(client, f"/partials/ingest/runs/{run_id}")

    assert "could not be fetched" in body
    assert "Output of the fetch" in body and "<script>alert(1)" not in body
    assert "&lt;script&gt;alert(1)" in body


def test_the_events_name_the_file_and_are_split_into_problems_and_all(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.write_catalog(_job())
    run_id = deployment.ingest.start_run("handbook")
    deployment.ingest.finish(
        run_id,
        files_seen=2,
        docs_failed=1,
        events=[
            {"level": "info", "source": "local://docs/a.md", "message": "no text", "ts": "t"},
            {"level": "error", "source": "local://docs/b.pdf", "message": "failed", "ts": "t"},
        ],
    )

    body = _body(client, f"/partials/ingest/runs/{run_id}")

    assert "1 problem" in body and "local://docs/b.pdf" in body and "All messages (2)" in body


@pytest.mark.parametrize(
    ("status", "fragment"),
    [
        ("aborted_guard", "safety check"),
        ("aborted_lock", "Another run was changing"),
        ("interrupted", "was stopped"),
    ],
)
def test_each_way_a_run_can_stop_has_its_explanation(
    client: TestClient, deployment: Deployment, status: str, fragment: str
) -> None:
    deployment.write_catalog(_job())
    run_id = deployment.ingest.start_run("handbook")
    deployment.ingest.finish(run_id, status, error="some reason")

    assert fragment in _body(client, f"/partials/ingest/runs/{run_id}")


def test_a_run_the_ingester_no_longer_knows_is_a_message_not_an_error(client: TestClient) -> None:
    body = _body(client, "/partials/ingest/runs/forgotten")

    assert "does not know this run any more" in body


# ---------------------------------------------------------------------------
# Credentials, leftovers, the catalog file
# ---------------------------------------------------------------------------


def test_the_credentials_page_lists_names_and_where_they_are_used_and_never_a_value(
    client: TestClient, deployment: Deployment
) -> None:
    from app.core.ingest.secrets import SecretsRepository

    SecretsRepository(deployment.config_dir, "s").set("QI_SECRET_DAV", "very-secret-value")
    deployment.write_catalog(
        _job(source={"type": "webdav", "label": "c", "url": "https://c.test",
                     "pass": "${env:QI_SECRET_DAV}"})
    )

    body = _body(client, "/partials/ingest/secrets")

    assert "QI_SECRET_DAV" in body and "QI_SECRET_FROM_ENV" in body
    assert 'href="/ingest/jobs/handbook"' in body
    assert "Delete (in use)" in body and "ai/rag/.env" in body
    assert "very-secret-value" not in body
    assert "secrets.yaml" in body


def test_the_credentials_page_explains_a_missing_key_and_an_old_ingester(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.ingest.features = []
    (deployment.config_dir / "ai" / "rag" / ".env").write_text(
        f"QI_API_TOKEN={TOKEN}\n", encoding="utf-8"
    )

    body = re.sub(r"\s+", " ", _body(client, "/partials/ingest/secrets"))

    assert "QI_CONNECTIONS_SECRET" in body and "no credential can be stored" in body
    assert "does not read stored credentials" in body


def test_the_leftovers_page_never_offers_to_delete_a_job_that_is_in_the_file(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.write_catalog(_job())
    deployment.ingest.jobs.pop("handbook")
    deployment.ingest.orphans = [
        {"job_id": "handbook", "collection": "kb", "state_rows": 2, "points": 9},
        {"job_id": "old", "collection": "kb", "state_rows": 1, "points": 3},
    ]

    body = _body(client, "/partials/ingest/orphans")

    assert "still in jobs.yaml" in body and "Repair the job" in body
    assert body.count("orphan-delete") >= 2
    assert '"job_id": "old"' in body.replace("&#34;", '"') or "&#34;old&#34;" in body
    assert "&#34;handbook&#34;, &#34;collection" not in body


def test_an_empty_leftovers_page_says_so(client: TestClient) -> None:
    assert "Nothing is left over" in _body(client, "/partials/ingest/orphans")


def test_the_catalog_page_carries_the_file_and_the_defaults(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.write_catalog(_job(), defaults={"embedding": {"model": "bge-m3"}})

    body = _body(client, "/ingest/catalog")

    assert "version: 1" in body and "bge-m3" in body
    assert "catalogDefaults(" in body and "rawEditor(" in body
