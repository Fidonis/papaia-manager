"""Managing ingest jobs: what the list shows, what a save does, and what it refuses.

Against a fake ingester that reads the real `jobs.yaml`, so what is checked is the file the
manager wrote and the questions it asked, in the order it asked them. The refusals matter as
much as the happy path: each one has to happen before anything is written, and a catalog the
ingester does not take has to leave `jobs.yaml` as it was.
"""
from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

_CONFIG_DIR = tempfile.mkdtemp(prefix="papaia-jobs-service-config-")
_WORKSPACE_DIR = tempfile.mkdtemp(prefix="papaia-jobs-service-workspace-")

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

from app.config import Settings, get_settings  # noqa: E402
from app.core.audit import audit_path  # noqa: E402
from app.core.ingest import catalog, runs  # noqa: E402
from app.core.ingest.client import IngestClient  # noqa: E402
from app.core.ingest.errors import (  # noqa: E402
    CatalogConflict,
    CatalogRejected,
    Conflict,
    IngestTooOld,
    InvalidJob,
    InvalidRequest,
    NotFound,
)
from app.core.ingest.jobs_service import (  # noqa: E402
    STATE_LOADED,
    STATE_NOT_LOADED,
    STATE_PENDING,
    STATE_REGISTRY_ONLY,
    STATE_STALE,
    STATE_UNKNOWN,
    JobsService,
    RunOptions,
)
from app.core.vectordb.ingest_file import CONNECTIONS_RELPATH  # noqa: E402
from tests.fake_ingest import TOKEN, FakeIngest  # noqa: E402
from tests.fake_qdrant import Fleet  # noqa: E402


class Env:
    def __init__(self, config_dir: Path, settings: Settings) -> None:
        self.config_dir = config_dir
        self.settings = settings
        self.ingest = FakeIngest(config_dir)
        self.fleet = Fleet()
        self.fleet.add("qdrant-ingest", self.ingest)

    def service(self, *, token: str = TOKEN) -> JobsService:
        client = IngestClient(
            "http://qdrant-ingest:8300", token, transport=self.fleet.transport()
        )
        return JobsService(self.settings, client=client)

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

    def file_jobs(self) -> list[dict[str, Any]]:
        if not self.jobs_path.exists():
            return []
        return yaml.safe_load(self.jobs_path.read_text(encoding="utf-8"))["jobs"]

    def file_bytes(self) -> bytes | None:
        return self.jobs_path.read_bytes() if self.jobs_path.exists() else None

    def audit(self) -> list[dict[str, Any]]:
        path = audit_path(str(self.config_dir))
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    config_dir = tmp_path / "config"
    catalog_dir = config_dir / CONNECTIONS_RELPATH.parent
    catalog_dir.mkdir(parents=True)
    (config_dir / ".env").write_text("COMPOSE_PROFILES=keycloak,rag\n", encoding="utf-8")
    (config_dir / "ai" / "rag" / ".env").write_text(
        f"QI_CONNECTIONS_SECRET=s\nQI_API_TOKEN={TOKEN}\nQI_TIMEZONE=Europe/Berlin\n"
        "QI_SECRET_FROM_ENV=hunter2\n",
        encoding="utf-8",
    )
    (config_dir / CONNECTIONS_RELPATH).write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "connections": [
                    {"name": "default", "url": "http://q:6333"},
                    {"name": "research", "url": "http://r:6333"},
                ],
            }
        ),
        encoding="utf-8",
    )
    get_settings.cache_clear()
    settings = get_settings().model_copy(
        update={
            "papaia_config_dir": str(config_dir),
            "qdrant_ingest_url": "http://qdrant-ingest:8300",
        }
    )

    async def idle(_kind: object) -> None:
        return None

    monkeypatch.setattr(runs.runner, "find_runner", idle)
    yield Env(config_dir, settings)
    get_settings.cache_clear()


def _state(**overrides: Any) -> dict[str, Any]:
    """What the editor posts for a new local job."""
    state: dict[str, Any] = {
        "id": "handbook",
        "enabled": True,
        "description": "",
        "source": {"type": "local", "label": "handbook", "path": "/data/local/handbook"},
        "target": {"collection": "kb", "connection": "default"},
        "mode": "upsert",
        "schedule": {"mode": "manual"},
        "embedding": {"model": "nomic-embed-text"},
    }
    state.update(overrides)
    return state


def _elsewhere(job_id: str, collection: str, **overrides: Any) -> dict[str, Any]:
    """Another local job of its own, on a collection of its own."""
    return _authored(
        id=job_id,
        source={"type": "local", "label": job_id, "path": f"/data/local/{job_id}"},
        target={"collection": collection, "connection": "default"},
        **overrides,
    )


def _authored(**overrides: Any) -> dict[str, Any]:
    job: dict[str, Any] = {
        "id": "handbook",
        "source": {"type": "local", "label": "handbook", "path": "/data/local/handbook"},
        "target": {"collection": "kb", "connection": "default"},
        "mode": "upsert",
        "embedding": {"model": "nomic-embed-text"},
    }
    job.update(overrides)
    return job


# ---------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------


async def test_an_empty_installation_has_no_jobs_and_can_create_the_first(env: Env) -> None:
    view = await env.service().overview()

    assert view.jobs == ()
    assert not view.file_problem and view.editable
    assert view.ingester.usable and view.ingester.features >= {"run_progress", "documents"}


async def test_a_loaded_job_is_active_and_carries_its_last_run_and_totals(env: Env) -> None:
    env.write_catalog(_authored(schedule={"cron": "0 3 * * *"}))
    run_id = env.ingest.start_run("handbook")
    env.ingest.finish(run_id, docs_indexed=3, files_seen=3)
    env.ingest.documents["handbook"] = [
        {"rel_path": "a.md", "status": "indexed", "chunk_count": 4, "source": "x"},
        {"rel_path": "b.md", "status": "indexed", "chunk_count": 2, "source": "y"},
    ]

    [row] = (await env.service().overview()).jobs

    assert row.state == STATE_LOADED and row.note == ""
    assert (row.source_title, row.source_detail) == ("Folder on the server", "/data/local/handbook")
    assert (row.connection, row.collection, row.mode_label) == ("default", "kb", "Keep in sync")
    assert row.schedule == "Every day at 03:00"
    assert row.last_run is not None and row.last_run.status == "success"
    assert (row.documents, row.chunks) == (2, 6)
    assert row.runnable and row.etag


async def test_a_job_the_ingester_did_not_load_says_why_instead_of_vanishing(env: Env) -> None:
    broken = _elsewhere("broken", "x")
    broken["target"]["connection"] = "gone"
    env.write_catalog(_authored(), broken)
    env.ingest.jobs.pop("broken")
    env.ingest.catalog_errors = [
        {"job_id": "broken", "field": "target.connection", "message": "unknown connection 'gone'"}
    ]
    env.ingest.applied = False

    view = await env.service().overview()
    rows = {row.id: row for row in view.jobs}

    assert rows["broken"].state == STATE_NOT_LOADED
    assert rows["broken"].problems == ("target.connection: unknown connection 'gone'",)
    # The good job still runs, on the definition the ingester loaded before.
    assert rows["handbook"].state == STATE_STALE
    assert view.ingester.refused


async def test_a_job_in_the_file_that_the_ingester_has_not_read_yet_is_pending(env: Env) -> None:
    env.write_catalog(_authored())
    env.ingest.jobs.clear()

    [row] = (await env.service().overview()).jobs

    assert row.state == STATE_PENDING and "every 30 seconds" in row.note


async def test_a_job_only_the_ingester_knows_is_listed_and_marked(env: Env) -> None:
    env.write_catalog(_authored())
    env.ingest.jobs["ghost"] = {
        "id": "ghost",
        "source": {"type": "local", "label": "g"},
        "target": {"collection": "kb", "connection": "default"},
        "mode": "append",
    }

    rows = {row.id: row for row in (await env.service().overview()).jobs}

    assert rows["ghost"].state == STATE_REGISTRY_ONLY and not rows["ghost"].in_file
    assert rows["ghost"].mode_label == "Add new files only"


async def test_jobs_of_the_embedding_page_are_marked_managed(env: Env) -> None:
    managed = catalog.managed_job_id("default", "kb", "upload")
    env.write_catalog(_authored(id=managed))

    [row] = (await env.service().overview()).jobs

    assert row.managed


async def test_without_an_ingester_the_list_comes_from_the_file(env: Env) -> None:
    env.write_catalog(_authored())
    env.ingest.down = True

    view = await env.service().overview()

    assert not view.ingester.reachable
    [row] = view.jobs
    assert row.state == STATE_UNKNOWN and "not reachable" in row.note
    assert row.collection == "kb"


async def test_a_refused_token_is_told_apart_from_a_missing_ingester(env: Env) -> None:
    env.write_catalog(_authored())

    view = await env.service(token="wrong").overview()

    assert view.ingester.reachable and not view.ingester.usable
    assert "refused the API token" in view.ingester.reason


async def test_leftovers_count_only_what_the_file_does_not_explain(env: Env) -> None:
    env.write_catalog(_authored(), _elsewhere("dropped", "z"))
    env.ingest.jobs.pop("dropped")
    env.ingest.orphans = [
        {"job_id": "dropped", "collection": "z", "state_rows": 4, "points": 9},
        {"job_id": "deleted-long-ago", "collection": "kb", "state_rows": 2, "points": 3},
    ]

    view = await env.service().overview()

    assert view.leftovers == 1


async def test_an_ingester_that_serves_another_file_cannot_be_edited_from_here(env: Env) -> None:
    env.write_catalog(_authored())
    original = env.ingest._config

    def legacy() -> Any:
        response = original()
        body = json.loads(response.content)
        body["path"] = "/config/jobs.yaml"
        import httpx

        return httpx.Response(200, json=body)

    env.ingest._config = legacy  # type: ignore[method-assign]

    view = await env.service().overview()

    assert view.legacy and not view.editable


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------


async def test_a_new_job_is_written_short_loaded_and_audited(env: Env) -> None:
    result = await env.service().save(_state(), create=True, etag=None, user="alice")

    assert result.applied and result.verified and result.created
    assert env.file_jobs() == [_authored()]
    assert "handbook" in env.ingest.jobs
    [entry] = env.audit()
    assert (entry["user"], entry["action"], entry["target"]) == (
        "alice",
        "rag.ingest.job.create",
        "handbook",
    )


async def test_a_job_is_checked_before_anything_is_written(env: Env) -> None:
    bad = _state(
        target={"collection": "", "connection": "nowhere"},
        source={"type": "s3", "label": "x"},
    )

    with pytest.raises(InvalidJob) as raised:
        await env.service().save(bad, create=True, etag=None, user="alice")

    fields = {issue.field for issue in raised.value.issues}
    assert {"target.collection", "source.bucket"} <= fields
    assert env.file_bytes() is None
    assert all(path != "/v1/config/reload" for _, path in env.ingest.calls)


async def test_an_id_that_exists_is_not_overwritten(env: Env) -> None:
    env.write_catalog(_authored(description="mine"))
    before = env.file_bytes()

    with pytest.raises(InvalidJob) as raised:
        await env.service().save(_state(), create=True, etag=None, user="alice")

    assert [(i.field, i.message) for i in raised.value.issues] == [
        ("id", "a job with this id already exists")
    ]
    assert env.file_bytes() == before


@pytest.mark.parametrize("bad_id", ["", "Has Spaces", "new", "mgr-kb-1234-u"])
async def test_an_id_that_cannot_be_used_is_refused(env: Env, bad_id: str) -> None:
    with pytest.raises(InvalidJob) as raised:
        await env.service().save(_state(id=bad_id), create=True, etag=None, user="alice")

    assert "id" in {issue.field for issue in raised.value.issues}


async def test_an_edit_replaces_the_job_in_place_and_keeps_the_others(env: Env) -> None:
    other = _authored(
        id="other",
        source={"type": "local", "label": "o", "path": "/data/local/o"},
        target={"collection": "kb2", "connection": "default"},
    )
    env.write_catalog(other, _authored(), _elsewhere("last", "kb3"))
    service = env.service()
    _, _, etag = service.authored_job("handbook")

    result = await service.save(
        _state(description="Updated", mode="append"), create=False, etag=etag, user="alice"
    )

    assert result.applied and not result.created
    assert [job["id"] for job in env.file_jobs()] == ["other", "handbook", "last"]
    edited = env.file_jobs()[1]
    assert (edited["description"], edited["mode"]) == ("Updated", "append")
    assert env.file_jobs()[0] == other


async def test_an_edit_made_meanwhile_by_somebody_else_is_not_overwritten(env: Env) -> None:
    env.write_catalog(_authored())
    service = env.service()
    _, _, etag = service.authored_job("handbook")
    env.write_catalog(_authored(description="changed by the ingester's own interface"))
    before = env.file_bytes()

    with pytest.raises(CatalogConflict, match="changed by somebody else"):
        await service.save(_state(description="mine"), create=False, etag=etag, user="alice")

    assert env.file_bytes() == before


async def test_the_id_of_a_job_never_changes(env: Env) -> None:
    env.write_catalog(_authored())

    with pytest.raises(InvalidJob) as raised:
        await env.service().save(
            _state(id="renamed"), create=False, etag=None, user="alice", original_id="handbook"
        )

    assert "duplicate the job instead" in raised.value.issues[0].message


async def test_a_job_of_the_embedding_page_cannot_be_edited(env: Env) -> None:
    managed = catalog.managed_job_id("default", "kb", "upload")
    env.write_catalog(_authored(id=managed))

    with pytest.raises(InvalidJob, match="Embedding page"):
        await env.service().save(
            _state(id=managed, description="x"), create=False, etag=None, user="alice"
        )


async def test_what_the_ingester_would_refuse_is_rolled_back(env: Env) -> None:
    env.write_catalog(_authored())
    before = env.file_bytes()
    env.ingest.catalog_errors = [
        {"job_id": "second", "field": "source.pass", "message": "referenced variable is not set"}
    ]

    with pytest.raises(CatalogRejected, match="referenced variable is not set"):
        await env.service().save(
            _state(
                id="second",
                source={"type": "local", "label": "s", "path": "/data/local/s"},
                target={"collection": "kb2", "connection": "default"},
            ),
            create=True,
            etag=None,
            user="alice",
        )

    assert env.file_bytes() == before


async def test_a_good_job_next_to_a_broken_one_is_kept_and_not_active_yet(env: Env) -> None:
    env.write_catalog(_authored(), _elsewhere("broken", "kb9"))
    env.ingest.catalog_errors = [
        {"job_id": "broken", "field": "schedule.cron", "message": "invalid cron expression"}
    ]
    env.ingest.applied = False

    result = await env.service().save(
        _state(description="fine"), create=False, etag=None, user="alice"
    )

    assert result.verified and not result.applied
    assert result.elsewhere == ("broken: schedule.cron: invalid cron expression",)
    assert env.file_jobs()[0]["description"] == "fine"


async def test_saving_while_the_ingester_is_down_keeps_the_change_and_says_it_is_unchecked(
    env: Env,
) -> None:
    env.ingest.down = True

    result = await env.service().save(_state(), create=True, etag=None, user="alice")

    assert not result.verified and not result.applied
    assert "not checked" in result.note
    assert env.file_jobs() == [_authored()]


async def test_the_ingesters_own_verdict_is_asked_before_the_write(env: Env) -> None:
    env.ingest.validation_errors = [
        {"job_id": "handbook", "field": "source.path", "message": "no such folder"},
        {"job_id": "someone-else", "field": "id", "message": "not mine"},
    ]

    with pytest.raises(InvalidJob) as raised:
        await env.service().save(_state(), create=True, etag=None, user="alice")

    assert [(i.field, i.message) for i in raised.value.issues] == [
        ("source.path", "no such folder")
    ]
    assert env.file_bytes() is None
    assert env.ingest.validated, "the candidate catalog was sent to the ingester"


async def test_an_ingester_without_validate_is_not_asked_and_the_mirror_still_checks(
    env: Env,
) -> None:
    env.ingest.features = []

    result = await env.service().save(_state(), create=True, etag=None, user="alice")

    assert result.applied
    assert env.ingest.validated == []
    with pytest.raises(InvalidJob):
        await env.service().save(
            _state(id="b", target={"collection": "k", "connection": "nowhere"}),
            create=True,
            etag=None,
            user="alice",
        )


async def test_the_catalog_defaults_shape_what_is_written(env: Env) -> None:
    env.write_catalog(
        _authored(),
        defaults={
            "chunking": {"words": 512, "overlap": 64},
            "schedule": {"timezone": "Europe/Berlin"},
        },
    )
    state = _state(
        description="d",
        chunking={"strategy": "auto", "words": 512, "overlap": 50},
        schedule={"mode": "daily", "time": "04:00", "timezone": "Europe/Berlin"},
    )
    _, _, etag = env.service().authored_job("handbook")

    await env.service().save(state, create=False, etag=etag, user="alice")

    [job] = env.file_jobs()
    assert job["chunking"] == {"overlap": 50}, "words follows the default; overlap is an override"
    assert job["schedule"] == {"cron": "0 4 * * *"}, "the zone is the catalog's, not repeated"
    assert yaml.safe_load(env.jobs_path.read_text())["defaults"]["chunking"]["words"] == 512


async def test_validating_names_the_field_and_writes_nothing(env: Env) -> None:
    issues = await env.service().validate(
        _state(schedule={"mode": "cron", "cron": "not a cron"}), create=True
    )

    assert "schedule" in {issue.field for issue in issues} or "schedule.cron" in {
        issue.field for issue in issues
    }
    assert env.file_bytes() is None
    assert await env.service().validate(_state(), create=True) == []


# ---------------------------------------------------------------------------
# Enabling, disabling, deleting
# ---------------------------------------------------------------------------


async def test_disabling_writes_enabled_false_and_enabling_drops_the_key_again(env: Env) -> None:
    env.write_catalog(_authored())
    service = env.service()

    off = await service.set_enabled("handbook", False, user="alice")
    assert off.applied and env.file_jobs()[0]["enabled"] is False
    assert env.ingest.jobs["handbook"]["enabled"] is False

    on = await service.set_enabled("handbook", True, user="alice")
    assert on.applied and "enabled" not in env.file_jobs()[0]
    assert [e["action"] for e in env.audit()] == ["rag.ingest.job.disable", "rag.ingest.job.enable"]


async def test_enabling_a_job_that_would_now_clash_is_refused(env: Env) -> None:
    twin = _authored(id="twin", enabled=False)  # same collection and label as handbook
    env.write_catalog(_authored(), twin)
    before = env.file_bytes()

    with pytest.raises(InvalidJob) as raised:
        await env.service().set_enabled("twin", True, user="alice")

    assert "source.label" in {issue.field for issue in raised.value.issues}
    assert env.file_bytes() == before


async def test_a_disabled_job_cannot_be_run(env: Env) -> None:
    env.write_catalog(_authored(enabled=False))

    with pytest.raises(Conflict, match="disabled"):
        await env.service().run_now("handbook", RunOptions(), user="alice")

    assert env.ingest.run_bodies == []


async def test_deleting_removes_the_entry_and_purging_removes_its_content(env: Env) -> None:
    env.write_catalog(_authored(), _elsewhere("keep", "kb2"))
    env.ingest.orphans = [{"job_id": "handbook", "collection": "kb", "state_rows": 4, "points": 12}]
    _, _, etag = env.service().authored_job("handbook")

    result = await env.service().delete("handbook", etag=etag, purge=True, user="alice")

    assert [job["id"] for job in env.file_jobs()] == ["keep"]
    assert (result.purged, result.deleted_points, result.deleted_rows) == (True, 12, 4)
    assert env.audit()[-1]["action"] == "rag.ingest.job.delete"


async def test_deleting_without_purge_leaves_the_content_for_the_leftovers_page(env: Env) -> None:
    env.write_catalog(_authored())
    env.ingest.orphans = [{"job_id": "handbook", "collection": "kb", "state_rows": 4, "points": 12}]

    result = await env.service().delete("handbook", etag=None, purge=False, user="alice")

    assert not result.purged and env.file_jobs() == []
    assert env.ingest.orphans, "nothing was removed from the collection"


async def test_a_job_with_a_working_run_cannot_be_deleted(env: Env) -> None:
    env.write_catalog(_authored())
    env.ingest.start_run("handbook")
    before = env.file_bytes()

    with pytest.raises(Conflict, match="working"):
        await env.service().delete("handbook", etag=None, purge=False, user="alice")

    assert env.file_bytes() == before
    assert env.ingest.delete_runs_calls == [], "no history is touched while a run works"


async def test_a_job_of_the_embedding_page_can_be_deleted_but_not_paused(env: Env) -> None:
    managed = catalog.managed_job_id("default", "kb", "upload")
    env.write_catalog(_authored(id=managed))
    env.ingest.seed_run(managed, "2026-10-05T10:00:00+00:00")

    with pytest.raises(Conflict):
        await env.service().set_enabled(managed, False, user="alice")
    result = await env.service().delete(managed, etag=None, purge=False, user="alice")

    assert env.file_jobs() == []
    assert result.deleted_runs == 1
    assert env.ingest.runs == {}


async def test_deleting_a_job_deletes_its_runs_and_only_its_runs(env: Env) -> None:
    env.write_catalog(_authored(), _elsewhere("keep", "kb2"))
    for day in ("01", "02", "03"):
        env.ingest.seed_run("handbook", f"2026-10-{day}T10:00:00+00:00", log_lines=2)
    kept = env.ingest.seed_run("keep", "2026-10-02T10:00:00+00:00")

    result = await env.service().delete("handbook", etag=None, purge=False, user="alice")

    assert result.deleted_runs == 3
    assert list(env.ingest.runs) == [kept]
    # The ingester has let go of the job before its history is touched.
    calls = env.ingest.calls
    reload_at = calls.index(("POST", "/v1/config/reload"))
    assert reload_at < calls.index(("DELETE", "/v1/jobs/handbook/runs"))
    entry = env.audit()[-1]
    assert entry["action"] == "rag.ingest.job.delete"
    assert entry["params"]["deleted_runs"] == 3


async def test_deleting_a_job_keeps_a_run_that_started_meanwhile(env: Env) -> None:
    env.write_catalog(_authored())
    env.ingest.seed_run("handbook", "2026-10-01T10:00:00+00:00")
    original = env.ingest._delete_runs

    def late(job_id: str, query: dict[str, str]) -> Any:
        # A run starts between the check that none works and the deletion.
        env.ingest.seed_run(job_id, "2026-10-07T10:00:00+00:00", "running")
        return original(job_id, query)

    env.ingest._delete_runs = late  # type: ignore[method-assign]

    result = await env.service().delete("handbook", etag=None, purge=False, user="alice")

    assert result.deleted_runs == 1
    assert "still working was kept" in result.note
    assert [run["status"] for run in env.ingest.runs.values()] == ["running"]


async def test_an_ingester_that_cannot_delete_runs_keeps_the_history_and_says_so(env: Env) -> None:
    env.write_catalog(_authored())
    env.ingest.features = ["run_progress", "documents", "validate", "secret_store"]
    env.ingest.seed_run("handbook", "2026-10-01T10:00:00+00:00")

    result = await env.service().delete("handbook", etag=None, purge=False, user="alice")

    assert env.file_jobs() == []
    assert result.deleted_runs == 0
    assert "cannot delete runs" in result.note
    assert len(env.ingest.runs) == 1


async def test_runs_stay_while_the_ingester_still_serves_the_deleted_job(env: Env) -> None:
    env.write_catalog(_authored(), _elsewhere("broken", "kb9"))
    env.ingest.seed_run("handbook", "2026-10-01T10:00:00+00:00")
    # Another job is invalid, so the ingester keeps its previous catalog and the job with it.
    env.ingest.catalog_errors = [
        {"job_id": "broken", "field": "schedule.cron", "message": "invalid cron expression"}
    ]

    result = await env.service().delete("handbook", etag=None, purge=False, user="alice")

    assert [job["id"] for job in env.file_jobs()] == ["broken"]
    assert result.deleted_runs == 0
    assert "still serves this job" in result.note
    assert env.ingest.delete_runs_calls == []
    assert len(env.ingest.runs) == 1


async def test_a_failing_history_step_does_not_stop_the_purge(env: Env) -> None:
    env.write_catalog(_authored())
    env.ingest.orphans = [{"job_id": "handbook", "collection": "kb", "state_rows": 4, "points": 12}]
    env.ingest.seed_run("handbook", "2026-10-01T10:00:00+00:00")
    env.ingest._delete_runs = (  # type: ignore[method-assign]
        lambda job_id, query: httpx.Response(500, json={"detail": "the ingester is unwell"})
    )

    result = await env.service().delete("handbook", etag=None, purge=True, user="alice")

    assert result.purged and result.deleted_points == 12
    assert result.deleted_runs == 0
    assert "runs could not be deleted" in result.note


async def test_deleting_with_the_ingester_down_removes_the_entry_and_points_to_the_runs_page(
    env: Env,
) -> None:
    env.write_catalog(_authored())
    env.ingest.down = True

    result = await env.service().delete("handbook", etag=None, purge=False, user="alice")

    assert env.file_jobs() == []
    assert "could not be reached" in result.note
    assert "Runs page" in result.note


async def test_a_job_that_is_not_in_the_file_is_not_found_before_anything_is_deleted(
    env: Env,
) -> None:
    env.ingest.seed_run("ghost", "2026-10-01T10:00:00+00:00")

    with pytest.raises(NotFound):
        await env.service().delete("ghost", etag=None, purge=False, user="alice")

    assert env.ingest.delete_runs_calls == []


# ---------------------------------------------------------------------------
# Deleting a job's runs
# ---------------------------------------------------------------------------


def _seed_week(env: Env, job_id: str = "handbook") -> None:
    """One run a day at 10:00 UTC from 1 to 5 October 2026, each with two log lines."""
    for day in range(1, 6):
        env.ingest.seed_run(job_id, f"2026-10-0{day}T10:00:00+00:00", log_lines=2)


async def test_runs_are_counted_before_they_are_deleted(env: Env) -> None:
    env.write_catalog(_authored())
    _seed_week(env)

    preview = await env.service().delete_runs(
        "handbook", since=date(2026, 10, 2), until=date(2026, 10, 3), dry_run=True, user="alice"
    )

    assert (preview.matched, preview.matched_events, preview.deleted_runs) == (2, 4, 0)
    assert preview.dry_run is True
    assert len(env.ingest.runs) == 5
    assert env.audit() == [], "counting is not a change"


async def test_runs_of_the_chosen_days_are_deleted_and_audited(env: Env) -> None:
    env.write_catalog(_authored())
    _seed_week(env)

    result = await env.service().delete_runs(
        "handbook", since=date(2026, 10, 2), until=date(2026, 10, 3), user="alice"
    )

    assert (result.deleted_runs, result.deleted_events) == (2, 4)
    left = sorted(run["started_at"][:10] for run in env.ingest.runs.values())
    assert left == ["2026-10-01", "2026-10-04", "2026-10-05"]
    entry = env.audit()[-1]
    assert entry["action"] == "rag.ingest.run.delete" and entry["target"] == "handbook"
    assert entry["params"] == {
        "since": "2026-10-02", "until": "2026-10-03", "deleted_runs": 2,
        "deleted_events": 4, "skipped_running": 0,
    }


async def test_the_days_are_days_of_the_ingesters_zone(env: Env) -> None:
    env.write_catalog(_authored())
    # 23:30 UTC on the 2nd is already the 3rd in Berlin (UTC+2 in October).
    env.ingest.seed_run("handbook", "2026-10-02T21:59:00+00:00")
    env.ingest.seed_run("handbook", "2026-10-02T23:30:00+00:00")
    env.ingest.seed_run("handbook", "2026-10-03T22:00:00+00:00")

    await env.service().delete_runs(
        "handbook", since=date(2026, 10, 3), until=date(2026, 10, 3), user="alice"
    )

    sent = env.ingest.delete_runs_calls[-1]
    assert sent["since"] == "2026-10-02T22:00:00+00:00"
    assert sent["until"] == "2026-10-03T22:00:00+00:00"
    left = sorted(run["started_at"] for run in env.ingest.runs.values())
    assert left == ["2026-10-02T21:59:00+00:00", "2026-10-03T22:00:00+00:00"]


async def test_leaving_both_days_out_deletes_the_whole_history_of_a_job_that_is_gone(
    env: Env,
) -> None:
    # No job in the catalog at all: the history of a deleted job is what this is for.
    _seed_week(env, "ghost")
    env.ingest.seed_run("other", "2026-10-01T10:00:00+00:00")

    result = await env.service().delete_runs("ghost", user="alice")

    assert result.deleted_runs == 5
    assert env.ingest.delete_runs_calls[-1].keys() == {"job_id", "confirm"}
    assert [run["job_id"] for run in env.ingest.runs.values()] == ["other"]


async def test_a_run_that_is_working_is_kept_and_reported(env: Env) -> None:
    env.write_catalog(_authored())
    env.ingest.seed_run("handbook", "2026-10-01T10:00:00+00:00")
    env.ingest.seed_run("handbook", "2026-10-02T10:00:00+00:00", "running")

    result = await env.service().delete_runs("handbook", user="alice")

    assert (result.deleted_runs, result.skipped_running) == (1, 1)
    assert [run["status"] for run in env.ingest.runs.values()] == ["running"]


async def test_the_runs_of_a_job_of_the_embedding_page_can_be_deleted(env: Env) -> None:
    managed = catalog.managed_job_id("default", "kb", "folder")
    env.write_catalog(_authored(id=managed))
    env.ingest.seed_run(managed, "2026-10-01T10:00:00+00:00")

    result = await env.service().delete_runs(managed, user="alice")

    assert result.deleted_runs == 1


async def test_the_first_day_may_not_be_after_the_last(env: Env) -> None:
    with pytest.raises(InvalidRequest, match="first day"):
        await env.service().delete_runs(
            "handbook", since=date(2026, 10, 5), until=date(2026, 10, 1), user="alice"
        )

    assert env.ingest.delete_runs_calls == []


async def test_something_that_is_not_a_job_id_is_refused_before_it_reaches_the_ingester(
    env: Env,
) -> None:
    with pytest.raises(InvalidRequest, match="not a job id"):
        await env.service().delete_runs("../runs", user="alice")

    assert env.ingest.calls == []


async def test_an_ingester_without_the_feature_is_told_apart(env: Env) -> None:
    env.ingest.features = ["run_progress", "documents", "validate", "secret_store"]
    env.ingest.seed_run("handbook", "2026-10-01T10:00:00+00:00")

    with pytest.raises(IngestTooOld, match="delete_runs"):
        await env.service().delete_runs("handbook", user="alice")

    assert len(env.ingest.runs) == 1


async def test_deleting_a_job_that_is_gone_is_not_found(env: Env) -> None:
    with pytest.raises(NotFound):
        await env.service().delete("nope", etag=None, purge=False, user="alice")


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


async def test_a_run_starts_with_only_what_was_asked_for(env: Env) -> None:
    env.write_catalog(_authored())

    run_id = await env.service().run_now("handbook", RunOptions(dry_run=True), user="alice")

    assert env.ingest.run_bodies == [("handbook", {"dry_run": True})]
    assert run_id in env.ingest.runs
    assert env.audit()[-1]["action"] == "rag.ingest.job.run"


@pytest.mark.parametrize(
    ("options", "message"),
    [
        (RunOptions(mode="full"), "Rebuilding"),
        (
            RunOptions(mode="full", confirm_rebuild=True, full_scope="collection"),
            "whole collection",
        ),
        (RunOptions(mode="append", delete_vanished=False), "keeps in sync"),
    ],
)
async def test_the_destructive_runs_have_to_be_confirmed(
    env: Env, options: RunOptions, message: str
) -> None:
    env.write_catalog(_authored())

    with pytest.raises(InvalidRequest, match=message):
        await env.service().run_now("handbook", options, user="alice")

    assert env.ingest.run_bodies == []


async def test_a_dry_run_of_a_rebuild_needs_no_confirmation(env: Env) -> None:
    env.write_catalog(_authored())

    await env.service().run_now("handbook", RunOptions(mode="full", dry_run=True), user="alice")

    assert env.ingest.run_bodies == [("handbook", {"mode": "full", "dry_run": True})]


async def test_a_second_run_of_a_working_job_is_a_conflict(env: Env) -> None:
    env.write_catalog(_authored())
    env.ingest.start_run("handbook")

    with pytest.raises(Conflict, match="already working"):
        await env.service().run_now("handbook", RunOptions(), user="alice")


async def test_a_run_of_an_unknown_job_is_not_found(env: Env) -> None:
    with pytest.raises(NotFound):
        await env.service().run_now("nope", RunOptions(), user="alice")


async def test_a_working_run_can_be_aborted_and_it_is_audited(env: Env) -> None:
    env.write_catalog(_authored())
    run_id = env.ingest.start_run("handbook")

    await env.service().abort(run_id, user="alice")

    assert env.ingest.runs[run_id]["status"] == "interrupted"
    assert env.audit()[-1]["action"] == "rag.ingest.run.abort"
    with pytest.raises(Conflict):
        await env.service().abort(run_id, user="alice")
    with pytest.raises(NotFound):
        await env.service().abort("unknown", user="alice")


async def test_runs_and_a_run_carry_the_progress_the_ingester_reports(env: Env) -> None:
    env.write_catalog(_authored())
    run_id = env.ingest.start_run("handbook")
    env.ingest.progress(run_id, phase="embedding", files_seen=40, files_done=10, current="a/b.md")

    [view] = await env.service().runs(job_id="handbook")
    single = await env.service().run(run_id)

    assert view.run_id == single.run_id == run_id
    assert (single.phase, single.files_done, single.current) == ("embedding", 10, "a/b.md")
    assert single.progress == 0.25 and single.phase_label == "Reading and embedding the files"
    assert single.dry_run is False


async def test_an_ingester_without_progress_reports_none(env: Env) -> None:
    env.ingest.features = []
    env.write_catalog(_authored())
    run_id = env.ingest.start_run("handbook")

    view = await env.service().run(run_id)

    assert view.files_done is None and view.progress is None and view.dry_run is None


# ---------------------------------------------------------------------------
# Leftovers
# ---------------------------------------------------------------------------


async def test_a_job_that_is_in_the_file_is_never_offered_as_a_leftover(env: Env) -> None:
    """After a restart the ingester drops a job whose secret went missing; it is not leftover."""
    env.write_catalog(_authored())
    env.ingest.jobs.pop("handbook")
    env.ingest.orphans = [
        {"job_id": "handbook", "collection": "kb", "state_rows": 4, "points": 12},
        {"job_id": "deleted-long-ago", "collection": "kb", "state_rows": 1, "points": 1},
    ]
    service = env.service()

    listed = {o["job_id"]: o["in_file"] for o in await service.orphans()}
    assert listed == {"handbook": True, "deleted-long-ago": False}

    with pytest.raises(Conflict, match="still in jobs.yaml"):
        await service.delete_orphan("handbook", user="alice")
    result = await service.delete_orphan("deleted-long-ago", user="alice")
    assert result["deleted_points"] == 1
    assert [o["job_id"] for o in env.ingest.orphans] == ["handbook"]
    assert env.audit()[-1]["action"] == "rag.ingest.orphan.delete"


async def test_a_job_with_a_working_run_is_not_cleaned_up(env: Env) -> None:
    env.ingest.orphans = [{"job_id": "gone", "collection": "kb", "state_rows": 1, "points": 1}]
    env.ingest.start_run("gone")

    with pytest.raises(Conflict, match="working"):
        await env.service().delete_orphan("gone", user="alice")


# ---------------------------------------------------------------------------
# The file itself
# ---------------------------------------------------------------------------


async def test_the_raw_text_is_replaced_as_typed_comments_included(env: Env) -> None:
    env.write_catalog(_authored())
    service = env.service()
    current = service.raw()
    typed = current["text"].replace("version: 1", "# my notes\nversion: 1")

    outcome, _ = await service.replace_raw(typed, revision=current["revision"], user="alice")

    assert env.file_bytes() == typed.encode("utf-8")
    assert outcome is not None and outcome.applied
    assert env.audit()[-1]["action"] == "rag.ingest.catalog.raw"


async def test_raw_text_that_would_not_load_is_refused_and_nothing_changes(env: Env) -> None:
    env.write_catalog(_authored())
    before = env.file_bytes()
    service = env.service()

    with pytest.raises(InvalidJob):
        await service.replace_raw(
            "jobs: [unterminated", revision=service.raw()["revision"], user="alice"
        )

    assert env.file_bytes() == before


async def test_raw_text_is_checked_by_the_mirror_when_the_ingester_cannot(env: Env) -> None:
    env.ingest.features = []
    env.write_catalog(_authored())
    service = env.service()
    lost = _authored(target={"collection": "k", "connection": "nowhere"})
    text = yaml.safe_dump({"version": 1, "jobs": [lost]})

    issues = await service.validate_raw(text)

    assert "target.connection" in {issue.field for issue in issues}


async def test_a_file_that_changed_since_it_was_opened_is_a_conflict(env: Env) -> None:
    env.write_catalog(_authored())
    service = env.service()
    revision = service.raw()["revision"]
    env.write_catalog(_authored(description="changed"))
    before = env.file_bytes()

    with pytest.raises(CatalogConflict, match="changed since it was opened"):
        await service.replace_raw(
            service.raw()["text"], revision=revision, user="alice"
        )

    assert env.file_bytes() == before


async def test_the_catalog_defaults_can_be_replaced_if_every_job_stays_valid(env: Env) -> None:
    env.write_catalog(_authored())
    service = env.service()

    await service.set_defaults(
        {"embedding": {"model": "bge-m3"}, "chunking": {"words": 300}}, user="alice"
    )
    assert service.defaults() == {"embedding": {"model": "bge-m3"}, "chunking": {"words": 300}}

    with pytest.raises(InvalidJob):
        await service.set_defaults({"chunking": {"words": 100, "overlap": 100}}, user="alice")
    with pytest.raises(InvalidJob, match="unsupported"):
        await service.set_defaults({"surprise": {"x": 1}}, user="alice")

    await service.set_defaults({}, user="alice")
    assert service.defaults() == {}
