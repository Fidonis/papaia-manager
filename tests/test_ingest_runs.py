"""Starting an embedding run, following it, and cleaning up after it.

Against a fake ingester that reads the real `jobs.yaml` and a fake Qdrant, so what is checked is
what the manager wrote and asked for, in the order it did. The refusals matter as much as the
happy path: each one has to happen before anything is written, and a catalog the ingester does
not take has to leave `jobs.yaml` as it was.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

_CONFIG_DIR = tempfile.mkdtemp(prefix="papaia-ingest-runs-config-")
_WORKSPACE_DIR = tempfile.mkdtemp(prefix="papaia-ingest-runs-workspace-")

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
from app.core.ingest import catalog, runs, uploads  # noqa: E402
from app.core.ingest import watcher as watcher_module  # noqa: E402
from app.core.ingest.client import IngestClient  # noqa: E402
from app.core.ingest.errors import (  # noqa: E402
    CatalogRejected,
    Conflict,
    IngestTooOld,
    IngestUnavailable,
    InvalidRequest,
    NotFound,
)
from app.core.ingest.runs import (  # noqa: E402
    EmbeddingService,
    SourceSpec,
    StartRequest,
    reconcile,
)
from app.core.ingest.watcher import IngestWatcher  # noqa: E402
from app.core.qdrant import QdrantClient  # noqa: E402
from app.core.rag import rag_backend  # noqa: E402
from app.core.rag_collections import CollectionStore, meta_point_id  # noqa: E402
from app.core.vectordb.ingest_file import CONNECTIONS_RELPATH  # noqa: E402
from tests.fake_ingest import TOKEN, FakeIngest  # noqa: E402
from tests.fake_qdrant import API_KEY, FakeQdrant, Fleet  # noqa: E402

MODEL = "nomic-embed-text"


class Bytes:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def read(self, size: int = -1, /) -> bytes:
        chunk, self._data = self._data, b""
        return chunk


class Env:
    """One deployment: a config directory, a Qdrant, an ingester, and a service on them."""

    def __init__(self, config_dir: Path, settings: Settings) -> None:
        self.config_dir = config_dir
        self.settings = settings
        self.docs = config_dir / "ai" / "rag" / "documents"
        self.qdrant = FakeQdrant(API_KEY)
        self.qdrant.add("kb", size=4)
        self.ingest = FakeIngest(config_dir)
        self.fleet = Fleet()
        self.fleet.add("qdrant.test", self.qdrant)
        self.fleet.add("qdrant-ingest", self.ingest)

    def service(self, *, token: str = TOKEN) -> EmbeddingService:
        qdrant = QdrantClient("http://qdrant.test:6333", API_KEY, transport=self.fleet.transport())
        store = CollectionStore(qdrant, rag_backend(str(self.config_dir)), connection="default")
        ingest = IngestClient(
            "http://qdrant-ingest:8300", token, transport=self.fleet.transport()
        )
        return EmbeddingService(self.settings, store=store, client=ingest)

    def uploads(self) -> uploads.UploadStore:
        return uploads.new_store(self.settings, self.docs)

    async def staged(self, files: dict[str, bytes], owner: str = "alice") -> uploads.Batch:
        store = self.uploads()
        batch = store.create(owner_sub=f"sub-{owner}", owner_name=owner)
        for path, data in files.items():
            await store.save_file(batch.id, path, Bytes(data))
        return store.get(batch.id)

    def record_model(self, collection: str = "kb", model: str = MODEL) -> None:
        if "_collection_meta" not in self.qdrant.collections:
            self.qdrant.add("_collection_meta", size=1)
        self.qdrant.put(
            "_collection_meta",
            meta_point_id(collection),
            {"collection": collection, "embedding_model": model, "vector_dimension": 4},
        )

    def jobs(self) -> list[dict[str, Any]]:
        path = self.config_dir / catalog.JOBS_RELPATH
        if not path.exists():
            return []
        return yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"]

    def jobs_bytes(self) -> bytes | None:
        path = self.config_dir / catalog.JOBS_RELPATH
        return path.read_bytes() if path.exists() else None

    def audit(self) -> list[dict[str, Any]]:
        path = audit_path(str(self.config_dir))
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    config_dir = tmp_path / "config"
    (config_dir / "ai" / "rag" / "documents").mkdir(parents=True)
    (config_dir / CONNECTIONS_RELPATH.parent).mkdir(parents=True, exist_ok=True)
    (config_dir / ".env").write_text("COMPOSE_PROFILES=keycloak,rag\n", encoding="utf-8")
    (config_dir / "ai" / "rag" / ".env").write_text(
        f"QDRANT_JWT_SECRET={API_KEY}\nQI_CONNECTIONS_SECRET=s\nQI_API_TOKEN={TOKEN}\n",
        encoding="utf-8",
    )
    (config_dir / CONNECTIONS_RELPATH).write_text(
        yaml.safe_dump({"version": 1, "connections": [{"name": "default", "url": "http://q:6333"}]}),
        encoding="utf-8",
    )
    get_settings.cache_clear()
    settings = get_settings().model_copy(
        update={
            "papaia_config_dir": str(config_dir),
            "qdrant_ingest_url": "http://qdrant-ingest:8300",
            "ingest_upload_ttl_hours": 24,
        }
    )

    async def idle(_kind: object) -> None:
        return None

    monkeypatch.setattr(runs.runner, "find_runner", idle)
    yield Env(config_dir, settings)
    get_settings.cache_clear()


def _add(batch: uploads.Batch, **changes: Any) -> StartRequest:
    return StartRequest(
        collection=changes.pop("collection", "kb"),
        mode=changes.pop("mode", "add"),
        source=changes.pop("source", SourceSpec("upload", batch.id)),
        **changes,
    )


# ---------------------------------------------------------------------------
# Add / update
# ---------------------------------------------------------------------------


async def test_adding_an_upload_writes_the_job_loads_it_and_starts_an_upsert_without_deletion(
    env: Env,
) -> None:
    batch = await env.staged({"handbook/one.md": b"# one", "two.md": b"two"})
    env.record_model()

    started = await env.service().start(_add(batch), user="alice")

    assert started.files == 2
    [job] = env.jobs()
    assert job["id"] == started.job_id == catalog.managed_job_id("default", "kb", "upload")
    assert job["source"] == {
        "type": "local",
        "label": "manager-upload",
        "path": batch.container_path,
    }
    assert "filters" not in job, "the whole upload is embedded"
    assert job["target"] == {"collection": "kb", "connection": "default"}
    assert job["embedding"] == {"model": MODEL}
    assert env.ingest.run_bodies == [(started.job_id, {"mode": "upsert", "delete_vanished": False})]
    marked = env.uploads().get(batch.id)
    assert (marked.state, marked.run_id, marked.mode) == ("embedding", started.run_id, "add")
    assert marked.collection == "kb"


async def test_the_start_is_audited_without_file_names_or_the_token(env: Env) -> None:
    batch = await env.staged({"payroll-2026-secret.xlsx": b"x"})
    env.record_model()

    started = await env.service().start(_add(batch), user="alice")

    [entry] = [e for e in env.audit() if e["action"] == "rag.ingest.run.start"]
    assert entry["user"] == "alice" and entry["target"] == "kb"
    assert entry["params"]["run_id"] == started.run_id
    assert entry["params"]["files"] == 1
    text = json.dumps(entry)
    assert "payroll" not in text and TOKEN not in text


async def test_a_selection_inside_an_upload_becomes_include_globs(env: Env) -> None:
    batch = await env.staged({"a/one.md": b"1", "a/two.md": b"2", "b.md": b"3", "c/d.md": b"4"})
    env.record_model()

    started = await env.service().start(
        _add(batch, source=SourceSpec("upload", batch.id, ("a", "b.md"))), user="alice"
    )

    [job] = env.jobs()
    assert job["filters"] == {"include": ["a/**", "b.md"]}
    assert started.files == 3


async def test_a_selection_of_the_folder_excludes_the_staging_area(env: Env) -> None:
    (env.docs / "handbook").mkdir()
    (env.docs / "handbook" / "a.md").write_text("a", encoding="utf-8")
    await env.staged({"secret.pdf": b"x"}, owner="bob")
    env.record_model()

    started = await env.service().start(
        StartRequest("kb", "add", SourceSpec("folder", paths=("",))), user="alice"
    )

    [job] = env.jobs()
    assert job["id"] == catalog.managed_job_id("default", "kb", "folder")
    assert job["source"] == {"type": "local", "label": "manager-folder", "path": "/data/local"}
    assert job["filters"] == {"exclude": ["uploads/**"]}
    assert started.files == 1, "the other administrator's upload is not counted"


async def test_the_folder_selection_of_a_path_in_the_staging_area_is_refused(env: Env) -> None:
    batch = await env.staged({"secret.pdf": b"x"}, owner="bob")
    env.record_model()

    with pytest.raises(InvalidRequest):
        await env.service().start(
            StartRequest("kb", "add", SourceSpec("folder", paths=(batch.relpath,))), user="alice"
        )

    assert env.jobs() == []


async def test_the_recorded_model_is_used_when_none_is_given(env: Env) -> None:
    batch = await env.staged({"a.md": b"x"})
    env.record_model(model="bge-m3")

    await env.service().start(_add(batch), user="alice")

    assert env.jobs()[0]["embedding"] == {"model": "bge-m3"}


async def test_adding_with_another_model_than_the_recorded_one_is_refused(env: Env) -> None:
    batch = await env.staged({"a.md": b"x"})
    env.record_model(model="bge-m3")

    with pytest.raises(InvalidRequest, match="use Replace to change the model"):
        await env.service().start(_add(batch, model="nomic-embed-text"), user="alice")

    assert env.jobs() == [] and env.ingest.run_bodies == []


async def test_a_collection_without_a_recorded_model_needs_one_to_be_named(env: Env) -> None:
    batch = await env.staged({"a.md": b"x"})
    service = env.service()

    with pytest.raises(InvalidRequest, match="Name the embedding model"):
        await service.start(_add(batch), user="alice")
    await service.start(_add(batch, model="bge-m3"), user="alice")

    assert env.jobs()[0]["embedding"] == {"model": "bge-m3"}


# ---------------------------------------------------------------------------
# Replace
# ---------------------------------------------------------------------------


async def test_replacing_needs_a_confirmation(env: Env) -> None:
    batch = await env.staged({"a.md": b"x"})
    env.record_model()

    with pytest.raises(InvalidRequest, match="has to be confirmed"):
        await env.service().start(_add(batch, mode="replace"), user="alice")

    assert env.jobs() == []


async def test_replacing_rebuilds_the_whole_collection_and_may_change_the_model(
    env: Env,
) -> None:
    batch = await env.staged({"a.md": b"x"})
    env.record_model(model="old-model")

    started = await env.service().start(
        _add(batch, mode="replace", confirm_replace=True, model="new-model"), user="alice"
    )

    assert env.jobs()[0]["embedding"] == {"model": "new-model"}
    assert env.ingest.run_bodies == [
        (started.job_id, {"mode": "full", "full_scope": "collection", "force": False})
    ]


async def test_replacing_a_collection_another_job_writes_to_needs_a_second_confirmation(
    env: Env,
) -> None:
    catalog_dir = env.config_dir / "ai" / "rag" / "catalog"
    (catalog_dir / "jobs.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "jobs": [
                    {
                        "id": "nightly",
                        "source": {"type": "s3", "label": "bucket", "bucket": "b"},
                        "target": {"collection": "kb", "connection": "default"},
                        "embedding": {"model": MODEL},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    batch = await env.staged({"a.md": b"x"})
    env.record_model()
    service = env.service()

    with pytest.raises(Conflict, match="nightly") as caught:
        await service.start(_add(batch, mode="replace", confirm_replace=True), user="alice")
    assert caught.value.detail == {"other_jobs": ["nightly"]}
    assert env.ingest.run_bodies == []

    await service.start(
        _add(batch, mode="replace", confirm_replace=True, confirm_other_jobs=True), user="alice"
    )

    assert env.ingest.run_bodies[0][1]["force"] is True
    assert [job["id"] for job in env.jobs()] == [
        "nightly",
        catalog.managed_job_id("default", "kb", "upload"),
    ]


async def test_the_other_managed_job_of_a_collection_is_not_a_job_of_somebody_else(
    env: Env,
) -> None:
    (env.docs / "a.md").write_text("a", encoding="utf-8")
    batch = await env.staged({"b.md": b"x"})
    env.record_model()
    service = env.service()
    await service.start(StartRequest("kb", "add", SourceSpec("folder", paths=("a.md",))), user="a")
    env.ingest.finish(env.ingest.last_run_id())

    # The ingester counts the folder job as a sibling and wants `force`; the page must not
    # ask the administrator about a job the manager itself keeps.
    await service.start(_add(batch, mode="replace", confirm_replace=True), user="a")

    assert env.ingest.run_bodies[-1][1]["force"] is True


# ---------------------------------------------------------------------------
# Refusals happen before anything is written
# ---------------------------------------------------------------------------


async def test_a_collection_that_does_not_exist_is_not_found_and_nothing_is_written(
    env: Env,
) -> None:
    batch = await env.staged({"a.md": b"x"})

    with pytest.raises(NotFound):
        await env.service().start(_add(batch, collection="nope"), user="alice")

    assert env.jobs() == [] and env.ingest.run_bodies == []


async def test_a_collection_name_the_ingester_cannot_use_is_refused(env: Env) -> None:
    batch = await env.staged({"a.md": b"x"})
    env.qdrant.add("x" * 200)

    with pytest.raises(InvalidRequest, match="128 characters"):
        await env.service().start(_add(batch, collection="x" * 200), user="alice")


async def test_a_system_collection_cannot_be_embedded_into(env: Env) -> None:
    batch = await env.staged({"a.md": b"x"})
    env.record_model()

    with pytest.raises(InvalidRequest):
        await env.service().start(_add(batch, collection="_collection_meta"), user="alice")


async def test_an_empty_selection_is_refused(env: Env) -> None:
    batch = await env.staged({"a.md": b"x"})
    env.record_model()
    (env.docs / "empty").mkdir()

    with pytest.raises(InvalidRequest, match="nothing to embed"):
        await env.service().start(
            StartRequest("kb", "add", SourceSpec("folder", paths=("empty",))), user="alice"
        )
    with pytest.raises(InvalidRequest, match="at least one"):
        await env.service().start(StartRequest("kb", "add", SourceSpec("folder")), user="alice")
    with pytest.raises(InvalidRequest, match="Choose an upload"):
        await env.service().start(StartRequest("kb", "add", SourceSpec("upload")), user="alice")
    assert batch.id and env.jobs() == []


async def test_an_unknown_upload_and_an_unknown_source_are_refused(env: Env) -> None:
    env.record_model()

    with pytest.raises(NotFound):
        await env.service().start(
            StartRequest("kb", "add", SourceSpec("upload", "20261005-100000-deadbeef")),
            user="alice",
        )
    with pytest.raises(InvalidRequest, match="'upload' or 'folder'"):
        await env.service().start(StartRequest("kb", "add", SourceSpec("ftp")), user="alice")
    with pytest.raises(InvalidRequest, match="'add' or 'replace'"):
        await env.service().start(StartRequest("kb", "merge", SourceSpec("folder")), user="alice")


async def test_an_older_ingester_cannot_add_but_can_still_replace(env: Env) -> None:
    batch = await env.staged({"a.md": b"x"})
    env.record_model()
    env.ingest.supports_delete_vanished = False
    service = env.service()

    with pytest.raises(IngestTooOld, match="delete_vanished"):
        await service.start(_add(batch), user="alice")
    assert env.jobs() == [] and env.ingest.run_bodies == []

    await service.start(_add(batch, mode="replace", confirm_replace=True), user="alice")

    assert env.ingest.run_bodies[0][1] == {
        "mode": "full",
        "full_scope": "collection",
        "force": False,
    }


async def test_a_second_run_for_the_collection_waits_for_the_first(env: Env) -> None:
    first = await env.staged({"a.md": b"x"})
    second = await env.staged({"b.md": b"y"}, owner="bob")
    env.record_model()
    service = env.service()
    await service.start(_add(first), user="alice")
    before = env.jobs_bytes()

    with pytest.raises(Conflict, match="still working"):
        await service.start(_add(second), user="bob")

    assert env.jobs_bytes() == before, "the running upload's path is not changed under it"
    assert env.uploads().get(second.id).state == "staged"


async def test_a_run_of_the_other_kind_also_blocks_the_collection(env: Env) -> None:
    (env.docs / "a.md").write_text("a", encoding="utf-8")
    batch = await env.staged({"b.md": b"y"})
    env.record_model()
    service = env.service()
    await service.start(StartRequest("kb", "add", SourceSpec("folder", paths=("a.md",))), user="a")

    with pytest.raises(Conflict, match="still working"):
        await service.start(_add(batch), user="a")


async def test_an_upload_that_a_run_is_reading_cannot_be_started_again(env: Env) -> None:
    batch = await env.staged({"a.md": b"x"})
    env.record_model()
    env.qdrant.add("other")
    env.record_model("other")
    service = env.service()
    await service.start(_add(batch), user="alice")

    with pytest.raises(Conflict, match="already being embedded"):
        await service.start(_add(batch, collection="other"), user="alice")


@pytest.mark.parametrize("label", ["restore", "upgrade", "stack action"])
async def test_nothing_starts_while_a_restore_an_upgrade_or_a_stack_action_runs(
    env: Env, monkeypatch: pytest.MonkeyPatch, label: str
) -> None:
    kinds = {
        "restore": runs.runner.RESTORE_KIND,
        "upgrade": runs.runner.UPGRADE_KIND,
        "stack action": runs.runner.STACK_KIND,
    }

    class Running:
        is_running = True

    async def find(kind: object) -> object:
        return Running() if kind is kinds[label] else None

    monkeypatch.setattr(runs.runner, "find_runner", find)
    batch = await env.staged({"a.md": b"x"})
    env.record_model()

    with pytest.raises(Conflict, match=label):
        await env.service().start(_add(batch), user="alice")

    assert env.jobs() == []


async def test_a_docker_that_cannot_be_asked_does_not_stop_a_run(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(_kind: object) -> None:
        raise runs.runner.RunnerError("docker is not reachable")

    monkeypatch.setattr(runs.runner, "find_runner", broken)
    batch = await env.staged({"a.md": b"x"})
    env.record_model()

    await env.service().start(_add(batch), user="alice")


async def test_without_a_token_nothing_is_written(env: Env) -> None:
    batch = await env.staged({"a.md": b"x"})
    env.record_model()

    with pytest.raises(IngestUnavailable, match="QI_API_TOKEN"):
        await env.service(token="").start(_add(batch), user="alice")

    assert env.jobs() == []


async def test_a_wrong_token_and_a_down_ingester_are_unavailable_not_errors(env: Env) -> None:
    batch = await env.staged({"a.md": b"x"})
    env.record_model()

    with pytest.raises(IngestUnavailable, match="refused the API token"):
        await env.service(token="wrong").start(_add(batch), user="alice")
    env.ingest.down = True
    with pytest.raises(IngestUnavailable, match="not reachable"):
        await env.service().start(_add(batch), user="alice")

    assert env.jobs() == []


# ---------------------------------------------------------------------------
# A catalog the ingester does not take
# ---------------------------------------------------------------------------


async def test_a_job_the_ingester_refuses_is_rolled_back(env: Env) -> None:
    catalog_dir = env.config_dir / "ai" / "rag" / "catalog"
    (catalog_dir / "jobs.yaml").write_text(
        yaml.safe_dump({"version": 1, "jobs": []}), encoding="utf-8"
    )
    before = env.jobs_bytes()
    batch = await env.staged({"a.md": b"x"})
    env.record_model()
    env.ingest.catalog_errors = [
        {"job_id": catalog.managed_job_id("default", "kb", "upload"), "field": "embedding.model",
         "message": "no such model"}
    ]

    with pytest.raises(CatalogRejected, match="no such model"):
        await env.service().start(_add(batch), user="alice")

    assert env.jobs_bytes() == before
    assert env.ingest.run_bodies == []
    assert env.uploads().get(batch.id).state == "staged"


async def test_an_old_definition_the_ingester_keeps_serving_is_not_trusted(env: Env) -> None:
    first = await env.staged({"a.md": b"x"})
    second = await env.staged({"b.md": b"y"}, owner="bob")
    env.record_model()
    service = env.service()
    await service.start(_add(first), user="alice")
    env.ingest.finish(env.ingest.last_run_id())
    # Somebody else's job elsewhere makes the ingester refuse every new catalog, and it keeps
    # serving the previous one: the job exists, with the path of the FIRST upload.
    env.ingest.catalog_errors = [
        {"job_id": "someone-elses", "field": "source.type", "message": "unsupported"}
    ]

    with pytest.raises(CatalogRejected, match="someone-elses"):
        await service.start(_add(second), user="bob")

    assert env.jobs()[0]["source"]["path"] == first.container_path, "rolled back"
    assert len(env.ingest.run_bodies) == 1, "no run was started on the old definition"


async def test_a_catalog_that_would_break_another_jobs_collection_is_refused_before_writing(
    env: Env,
) -> None:
    (env.config_dir / "ai" / "rag" / "catalog" / "jobs.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "jobs": [
                    {
                        "id": "hr",
                        "source": {"type": "s3", "label": "bucket", "bucket": "b"},
                        "target": {"collection": "kb", "connection": "default"},
                        "embedding": {"model": "bge-m3"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    before = env.jobs_bytes()
    batch = await env.staged({"a.md": b"x"})
    env.record_model(model=MODEL)

    with pytest.raises(CatalogRejected, match="records exactly one model"):
        await env.service().start(_add(batch), user="alice")

    assert env.jobs_bytes() == before and env.ingest.run_bodies == []


# ---------------------------------------------------------------------------
# Following a run
# ---------------------------------------------------------------------------


async def test_a_running_run_shows_the_chunks_written_so_far(env: Env) -> None:
    batch = await env.staged({"a.md": b"x"})
    env.record_model()
    service = env.service()
    started = await service.start(_add(batch), user="alice")
    for index in range(3):
        env.qdrant.put("kb", f"p{index}", {"ingest_run": started.run_id, "source": "s"})
    env.qdrant.put("kb", "old", {"ingest_run": "an-earlier-run", "source": "s"})

    view = await service.run_view(started.run_id, collection="kb")

    assert view.active and view.status == "running" and view.label == "Running"
    assert view.live_chunks == 3


async def test_a_finished_run_shows_the_ingesters_counters_and_events(env: Env) -> None:
    batch = await env.staged({"a.md": b"x"})
    env.record_model()
    service = env.service()
    started = await service.start(_add(batch), user="alice")
    env.ingest.finish(
        started.run_id,
        files_seen=4,
        docs_indexed=3,
        docs_unchanged=1,
        chunks_upserted=12,
        events=[
            {"run_id": started.run_id, "seq": 1, "ts": "t", "level": "warning",
             "source": "local://manager-upload/a.md", "message": "no extractable text"}
        ],
    )

    view = await service.run_view(started.run_id, collection="kb")

    assert not view.active and view.clean and view.label == "Finished"
    assert view.live_chunks is None, "only a working run is counted"
    assert view.counters["docs_indexed"] == 3 and view.counters["chunks_upserted"] == 12
    assert [(e.level, e.message) for e in view.events] == [("warning", "no extractable text")]
    assert view.as_dict()["counters"]["files_seen"] == 4


async def test_a_run_with_a_failed_document_is_finished_with_errors(env: Env) -> None:
    batch = await env.staged({"a.md": b"x"})
    env.record_model()
    service = env.service()
    started = await service.start(_add(batch), user="alice")
    env.ingest.finish(started.run_id, docs_failed=1, docs_indexed=2)

    view = await service.run_view(started.run_id)

    assert view.label == "Finished with errors" and not view.clean


@pytest.mark.parametrize(
    ("status", "label"),
    [
        ("failed", "Failed"),
        ("interrupted", "Stopped"),
        ("aborted_guard", "Stopped by a safety check"),
        ("aborted_lock", "Gave up waiting for the collection"),
    ],
)
async def test_the_other_endings_are_named_in_words(env: Env, status: str, label: str) -> None:
    run_id = env.ingest.start_run("mgr-x")
    env.ingest.finish(run_id, status, error="boom")

    view = await env.service().run_view(run_id)

    assert (view.label, view.error, view.clean) == (label, "boom", False)


async def test_a_long_event_list_is_cut_off(env: Env) -> None:
    run_id = env.ingest.start_run("mgr-x")
    env.ingest.finish(
        run_id,
        events=[
            {"ts": "t", "level": "error", "source": f"s{i}", "message": "m"} for i in range(500)
        ],
    )

    view = await env.service().run_view(run_id)

    assert len(view.events) == runs.MAX_EVENTS and view.events_truncated


async def test_an_unknown_run_is_not_found(env: Env) -> None:
    with pytest.raises(NotFound):
        await env.service().run_view("nope")


async def test_the_history_merges_both_kinds_newest_first(env: Env) -> None:
    older = env.ingest.start_run(catalog.managed_job_id("default", "kb", "upload"))
    newer = env.ingest.start_run(catalog.managed_job_id("default", "kb", "folder"))
    env.ingest.start_run(catalog.managed_job_id("default", "other", "upload"))

    views = await env.service().history("kb")

    assert [view.run_id for view in views] == [newer, older]


async def test_a_run_can_be_aborted_once(env: Env) -> None:
    batch = await env.staged({"a.md": b"x"})
    env.record_model()
    service = env.service()
    started = await service.start(_add(batch), user="alice")

    await service.abort(started.run_id, user="alice")

    assert env.ingest.runs[started.run_id]["status"] == "interrupted"
    assert [e["action"] for e in env.audit()][-1] == "rag.ingest.run.abort"
    with pytest.raises(Conflict, match="not running"):
        await service.abort(started.run_id, user="alice")
    with pytest.raises(NotFound):
        await service.abort("nope", user="alice")


async def test_the_status_says_whether_the_ingester_can_be_used(env: Env) -> None:
    ready = await env.service().status()
    assert (ready.ready, ready.supports_add, ready.documents.path) == (True, True, env.docs)

    env.ingest.supports_delete_vanished = False
    assert (await env.service().status()).supports_add is False

    no_token = await env.service(token="").status()
    assert not no_token.ready and "QI_API_TOKEN" in no_token.reason

    env.ingest.down = True
    down = await env.service().status()
    assert not down.ready and "not reachable" in down.reason


# ---------------------------------------------------------------------------
# After a run
# ---------------------------------------------------------------------------


async def _embedding(env: Env, files: dict[str, bytes] | None = None) -> tuple[uploads.Batch, str]:
    batch = await env.staged(files or {"a.md": b"x"})
    env.record_model()
    started = await env.service().start(_add(batch), user="alice")
    return batch, started.run_id


async def _reconcile(env: Env) -> runs.Reconciled:
    return await reconcile(env.settings, transport=env.fleet.transport())


async def test_an_upload_is_removed_once_its_run_succeeded_cleanly(env: Env) -> None:
    batch, run_id = await _embedding(env, {"secret/plan.pdf": b"confidential"})
    folder = env.uploads().directory(batch)
    assert (folder / "secret" / "plan.pdf").is_file()

    working = await _reconcile(env)
    assert working.consumed == [] and working.waiting == 1 and folder.is_dir()

    env.ingest.finish(run_id, docs_indexed=1)
    done = await _reconcile(env)

    assert done.consumed == [batch.id] and done.waiting == 0
    assert not folder.exists() and not folder.parent.exists()
    with pytest.raises(NotFound):
        env.uploads().get(batch.id)
    assert "rag.ingest.upload.consume" in [e["action"] for e in env.audit()]


@pytest.mark.parametrize(
    ("status", "counters", "note"),
    [
        ("failed", {}, "the embedding endpoint died"),
        ("interrupted", {}, "interrupted by shutdown"),
        ("success", {"docs_failed": 2}, "2 document(s) failed"),
        ("aborted_guard", {}, "guard"),
    ],
)
async def test_an_upload_is_kept_when_its_run_did_not_succeed_cleanly(
    env: Env, status: str, counters: dict[str, int], note: str
) -> None:
    batch, run_id = await _embedding(env)
    env.ingest.finish(run_id, status, error=note if status != "success" else None, **counters)

    result = await _reconcile(env)

    assert result.kept == [batch.id] and result.consumed == []
    kept = env.uploads().get(batch.id)
    assert kept.state == "kept" and note in (kept.note or "")
    assert (env.uploads().directory(batch) / "a.md").is_file()


async def test_an_upload_whose_run_the_ingester_forgot_is_kept(env: Env) -> None:
    batch, run_id = await _embedding(env)
    del env.ingest.runs[run_id]

    result = await _reconcile(env)

    assert result.kept == [batch.id]
    assert "no longer knows" in (env.uploads().get(batch.id).note or "")


async def test_nothing_changes_while_the_ingester_cannot_be_asked(env: Env) -> None:
    batch, run_id = await _embedding(env)
    env.ingest.finish(run_id)
    env.ingest.down = True

    result = await _reconcile(env)

    assert result.consumed == [] and result.waiting == 1
    assert env.uploads().get(batch.id).state == "embedding"


async def test_a_forgotten_upload_is_removed_after_the_limit_even_without_a_token(
    env: Env,
) -> None:
    from dataclasses import replace
    from datetime import UTC, datetime, timedelta

    batch = await env.staged({"a.md": b"x"})
    old = (datetime.now(UTC) - timedelta(hours=30)).isoformat(timespec="seconds")
    env.uploads()._write(replace(batch, updated=old))  # noqa: SLF001
    (env.config_dir / "ai" / "rag" / ".env").write_text("QDRANT_JWT_SECRET=k\n", encoding="utf-8")

    result = await _reconcile(env)

    assert result.expired == [batch.id]
    assert not env.uploads().directory(batch).exists()
    assert "rag.ingest.upload.expire" in [e["action"] for e in env.audit()]


async def test_a_run_that_is_still_working_is_not_expired(env: Env) -> None:
    from dataclasses import replace
    from datetime import UTC, datetime, timedelta

    batch, _run_id = await _embedding(env)
    old = (datetime.now(UTC) - timedelta(hours=100)).isoformat(timespec="seconds")
    env.uploads()._write(replace(env.uploads().get(batch.id), updated=old))  # noqa: SLF001

    result = await _reconcile(env)

    assert result.expired == [] and env.uploads().directory(batch).is_dir()


async def test_without_the_rag_profile_nothing_is_touched(env: Env) -> None:
    batch, run_id = await _embedding(env)
    env.ingest.finish(run_id)
    (env.config_dir / ".env").write_text("COMPOSE_PROFILES=keycloak\n", encoding="utf-8")

    result = await _reconcile(env)

    assert result.consumed == [] and env.uploads().directory(batch).is_dir()


async def test_a_pass_that_cannot_remove_the_files_keeps_the_record_and_goes_on(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch, run_id = await _embedding(env)
    env.ingest.finish(run_id)

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(uploads.shutil, "rmtree", refuse)

    result = await _reconcile(env)

    assert result.consumed == []
    assert env.uploads().get(batch.id).state == "embedding", "found again by the next pass"


# ---------------------------------------------------------------------------
# The task that cleans up when nobody is looking
# ---------------------------------------------------------------------------


async def _wait_for(condition: object, seconds: float = 3.0) -> bool:
    assert callable(condition)
    for _ in range(int(seconds / 0.02)):
        if condition():
            return True
        await asyncio.sleep(0.02)
    return False


async def test_an_upload_is_removed_after_its_run_without_anyone_looking(env: Env) -> None:
    batch, run_id = await _embedding(env)
    folder = env.uploads().directory(batch)
    watching = IngestWatcher(
        env.settings, transport=env.fleet.transport(), busy_interval=0.02, idle_interval=0.02
    )
    watching.start()
    try:
        await asyncio.sleep(0.15)
        assert folder.is_dir(), "the run is still working"

        env.ingest.finish(run_id, docs_indexed=1)

        assert await _wait_for(lambda: not folder.exists())
    finally:
        watching.shutdown()
        await watching.stopped()


async def test_a_pass_that_fails_costs_that_pass_and_nothing_else(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    real = watcher_module.reconcile

    async def sometimes_broken(*args: object, **kwargs: object) -> object:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("the clean-up broke once")
        return await real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(watcher_module, "reconcile", sometimes_broken)
    watching = IngestWatcher(env.settings, busy_interval=0.02, idle_interval=0.02)
    watching.start()
    try:
        assert await _wait_for(lambda: len(calls) >= 3)
    finally:
        watching.shutdown()
        await watching.stopped()


async def test_starting_twice_runs_one_task_and_shutdown_stops_it(env: Env) -> None:
    watching = IngestWatcher(env.settings, busy_interval=0.01, idle_interval=0.01)
    watching.start()
    first = watching._task  # noqa: SLF001
    watching.start()

    assert watching._task is first  # noqa: SLF001
    watching.shutdown()
    await watching.stopped()

    assert first is not None and first.done()
    assert watching._task is None  # noqa: SLF001
