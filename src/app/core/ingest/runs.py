"""Starting an embedding run through the ingester, following it, and cleaning up after it.

A run is the ingester's, not the manager's: it lives in the ingester's own database, so its
status survives a restart of the manager and the manager's single-flight job queue is not
involved (an embedding run of hours would otherwise hold up every backup and add-on action).
The manager does three things around it:

* **Before** (`EmbeddingService.start`): decide what is embedded and how, refuse what cannot
  work, write the managed job into the catalog, make the ingester load it and check that it
  did, and only then start the run.
* **During** (`run_view`, `history`): read the run's state from the ingester and, while it
  works, count the points it has written so far in Qdrant, because the ingester reports its
  counters only when a run ends.
* **After** (`reconcile`): an upload is removed once its run succeeded without a failed
  document, and kept (for a retry, until the time limit) otherwise.

Two modes, both chosen per run through the run request, so the job itself stays harmless:

* **Add / update** is `upsert` with `delete_vanished: false`: new files are added, a file whose
  relative path is already in the collection and whose content changed replaces its chunks,
  unchanged files are skipped, and nothing is ever removed. The staged files are deleted after
  the run, which is why the deletion phase has to be off: the ingester would otherwise read
  every earlier file as vanished.
* **Replace** is `full` with `full_scope: collection`: the collection is dropped and rebuilt
  from this selection (its roles and its model record stay). It is refused unless confirmed,
  and a job of somebody else that serves the collection needs a second confirmation.

Things worth knowing when touching this:

* **A reload can be refused and leave an old definition serving.** The ingester keeps the
  previous catalog when the new one has an invalid job anywhere, so "the job exists" proves
  nothing. The service compares the loaded definition with what it wrote and rolls its write
  back when they differ.
* **One run per collection**, over both kinds of source: the staging folder is the job's path,
  so a second run would change the path under the first.
* **Nothing here ever deletes a file the manager did not stage.**
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any

from app.config import Settings
from app.core import runner
from app.core.audit import redact_params, write_audit_entry
from app.core.ingest import catalog, uploads
from app.core.ingest.catalog_load import reload_and_verify
from app.core.ingest.client import IngestClient
from app.core.ingest.documents import Counts, Documents
from app.core.ingest.errors import (
    Conflict,
    IngestError,
    IngestRejected,
    IngestTooOld,
    IngestUnavailable,
    InvalidRequest,
    NotFound,
)
from app.core.rag import DocumentsDir, documents_dir, rag_active, rag_secrets
from app.core.rag_collections import (
    CollectionInfo,
    CollectionStore,
    CollectionsView,
    InvalidInput,
    validate_embedding_model,
)
from app.core.vectordb.ingest_file import IngestFileRepository

log = logging.getLogger(__name__)

MODE_ADD = "add"
MODE_REPLACE = "replace"

SOURCE_UPLOAD = catalog.KIND_UPLOAD
SOURCE_FOLDER = catalog.KIND_FOLDER

# The staging area lives inside the documents folder, so a selection of the whole folder
# must not read other people's uploads.
_FOLDER_EXCLUDE = [f"{uploads.UPLOADS_DIR}/**"]

MAX_EVENTS = 200

_STATUS_TEXT = {
    "running": "Running",
    "success": "Finished",
    "failed": "Failed",
    "interrupted": "Stopped",
    "aborted_guard": "Stopped by a safety check",
    "aborted_lock": "Gave up waiting for the collection",
}
_MODE_TEXT = {
    "upsert": "Add / update",
    "full/collection": "Replace",
    "full/job": "Rebuild",
    "full": "Rebuild",
    "append": "Append",
}
_COUNTERS = (
    "files_seen",
    "docs_indexed",
    "docs_unchanged",
    "docs_skipped_changed",
    "docs_failed",
    "docs_deleted",
    "chunks_upserted",
    "bytes_read",
)


# ---------------------------------------------------------------------------
# What a caller asks for, and what comes back
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceSpec:
    """What to embed: an upload (all of it, or the paths picked in it) or paths of the folder."""

    kind: str
    batch: str | None = None
    paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class StartRequest:
    collection: str
    mode: str
    source: SourceSpec
    model: str | None = None
    confirm_replace: bool = False
    # Replace only: another job that serves the collection is dropped with its content.
    confirm_other_jobs: bool = False


@dataclass(frozen=True)
class StartedRun:
    run_id: str
    job_id: str
    files: int
    bytes: int


@dataclass(frozen=True)
class RunEvent:
    ts: str
    level: str
    source: str
    message: str


@dataclass(frozen=True)
class RunView:
    run_id: str
    job_id: str
    mode: str
    status: str
    label: str
    active: bool
    started_at: str
    finished_at: str | None
    error: str | None
    counters: dict[str, int]
    events: tuple[RunEvent, ...] = ()
    events_truncated: bool = False
    # Points the run has written so far, counted in Qdrant while it works.
    live_chunks: int | None = None

    @property
    def source_kind(self) -> str | None:
        """What the run read: an `upload` (removed after a clean run), the `folder`, or neither."""
        return catalog.kind_of(self.job_id)

    @property
    def mode_label(self) -> str:
        """The ingester's mode as the page's two choices (and what else it may be)."""
        return _MODE_TEXT.get(self.mode, self.mode)

    @property
    def failed_documents(self) -> int:
        return self.counters.get("docs_failed", 0)

    @property
    def clean(self) -> bool:
        """Succeeded, and no document failed on the way."""
        return self.status == "success" and self.failed_documents == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "job_id": self.job_id,
            "mode": self.mode,
            "mode_label": self.mode_label,
            "source": self.source_kind,
            "status": self.status,
            "label": self.label,
            "active": self.active,
            "clean": self.clean,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "counters": self.counters,
            "live_chunks": self.live_chunks,
            "events": [
                {"ts": e.ts, "level": e.level, "source": e.source, "message": e.message}
                for e in self.events
            ],
            "events_truncated": self.events_truncated,
        }


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def run_view_from(payload: dict[str, Any]) -> RunView:
    """A run as the ingester reports it (`{run, events}`, or the bare run of a listing)."""
    nested = _mapping(payload.get("run"))
    run = nested or payload
    raw_events = payload.get("events")
    events = tuple(
        RunEvent(
            str(item.get("ts") or ""),
            str(item.get("level") or "info"),
            str(item.get("source") or ""),
            str(item.get("message") or ""),
        )
        for item in (raw_events if isinstance(raw_events, list) else [])
        if isinstance(item, dict)
    )
    status = str(run.get("status") or "")
    mode = str(run.get("mode") or "")
    full_scope = run.get("full_scope")
    finished = run.get("finished_at")
    error = run.get("error")
    counters = {key: _int(run.get(key)) for key in _COUNTERS}
    label = _STATUS_TEXT.get(status, status or "Unknown")
    if status == "success" and counters["docs_failed"]:
        label = "Finished with errors"
    return RunView(
        run_id=str(run.get("run_id") or ""),
        job_id=str(run.get("job_id") or ""),
        mode=f"{mode}/{full_scope}" if full_scope else mode,
        status=status,
        label=label,
        active=status == "running",
        started_at=str(run.get("started_at") or ""),
        finished_at=str(finished) if finished else None,
        error=str(error) if error else None,
        counters=counters,
        events=events[:MAX_EVENTS],
        events_truncated=len(events) > MAX_EVENTS,
    )


@dataclass(frozen=True)
class IngestStatus:
    """Whether the ingester can be used, and what the page may offer."""

    ready: bool
    reason: str
    # None while it could not be found out.
    supports_add: bool | None
    documents: DocumentsDir


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


def _served_as_written(detail: dict[str, Any], job: dict[str, Any]) -> bool:
    """Whether the ingester serves the managed definition that was just written.

    The ingester keeps its previous catalog when it refuses a new one, so a job that exists
    may still be the one of an earlier run, with another path.
    """
    config = detail.get("config")
    if not isinstance(config, dict):
        return False
    source = _mapping(config.get("source"))
    target = _mapping(config.get("target"))
    embedding = _mapping(config.get("embedding"))
    filters = _mapping(config.get("filters"))
    wanted = _mapping(job.get("filters"))
    return (
        source.get("path") == job["source"]["path"]
        and source.get("label") == job["source"]["label"]
        and target.get("collection") == job["target"]["collection"]
        and target.get("connection") == job["target"]["connection"]
        and embedding.get("model") == job["embedding"]["model"]
        and list(filters.get("include") or []) == list(wanted.get("include") or [])
        and list(filters.get("exclude") or [])[: len(wanted.get("exclude") or [])]
        == list(wanted.get("exclude") or [])
    )


def browse_root(settings: Settings, source: str, upload: str | None) -> Documents:
    """The root a tree is listed from: the documents folder or one upload's staging folder.

    The staging area is hidden from the folder, so a selection there can never read another
    administrator's upload.
    """
    docs = documents_dir(settings.papaia_config_dir, settings.papaia_workspace_dir)
    if docs.path is None:
        raise IngestUnavailable(docs.reason)
    if source == SOURCE_UPLOAD:
        if not upload:
            raise InvalidRequest("Choose an upload.")
        store = uploads.new_store(settings, docs.path)
        return store.documents(store.get(upload))
    if source == SOURCE_FOLDER:
        return Documents(
            docs.path, catalog.LOCAL_ROOT, hidden_top=frozenset({uploads.UPLOADS_DIR})
        )
    raise InvalidRequest("The source must be 'upload' or 'folder'.")


@dataclass(frozen=True)
class _Plan:
    kind: str
    path: str
    include: list[str]
    exclude: list[str] | None
    counts: Counts
    batch: uploads.Batch | None


class EmbeddingService:
    """The Embedding page's reads and writes against one connection and the ingester."""

    def __init__(
        self,
        settings: Settings,
        *,
        store: CollectionStore,
        client: IngestClient,
    ) -> None:
        self._settings = settings
        self._store = store
        self._client = client
        self._docs = documents_dir(settings.papaia_config_dir, settings.papaia_workspace_dir)

    @property
    def connection(self) -> str:
        return self._store.connection

    @property
    def documents(self) -> DocumentsDir:
        return self._docs

    # ── what the page may offer ─────────────────────────────────────────────

    async def status(self) -> IngestStatus:
        """Whether the ingester can be reached with the token, and what it supports."""
        docs = self._docs
        try:
            await self._client.config()
        except IngestUnavailable as exc:
            return IngestStatus(False, str(exc), None, docs)
        except IngestRejected as exc:
            reason = f"The ingester answered with an error: {exc.detail}"
            return IngestStatus(False, reason, None, docs)
        supports: bool | None
        try:
            supports = await self._client.supports_update_without_delete()
        except IngestError:
            supports = None
        return IngestStatus(True, "", supports, docs)

    async def collections(self) -> CollectionsView:
        """The collections of the connection, or why there are none to show."""
        return await self._store.snapshot()

    def other_jobs(self, collection: str) -> list[str]:
        """Ids of the jobs of somebody else (not the manager's) that write to a collection.

        Replacing the collection drops their content too, so the page asks about them.
        """
        document = catalog.JobsFileRepository(self._settings.papaia_config_dir).snapshot().document
        return sorted(
            job_id
            for job_id in catalog.other_jobs_for(document, collection, set())
            if not catalog.is_managed(job_id)
        )

    def folder(self) -> Documents:
        """The documents folder to browse, without the staging area."""
        return browse_root(self._settings, SOURCE_FOLDER, None)

    def upload_store(self) -> uploads.UploadStore:
        if self._docs.path is None:
            raise IngestUnavailable(self._docs.reason)
        return uploads.new_store(self._settings, self._docs.path)

    # ── starting ────────────────────────────────────────────────────────────

    async def start(self, request: StartRequest, *, user: str) -> StartedRun:
        """Embed a selection into a collection. Nothing is changed before everything checks out."""
        if request.mode not in (MODE_ADD, MODE_REPLACE):
            raise InvalidRequest("The mode must be 'add' or 'replace'.")
        collection = request.collection
        if not catalog.COLLECTION_PATTERN.fullmatch(collection):
            raise InvalidRequest(
                f"The ingester accepts collection names of up to 128 characters: {collection!r}."
            )
        if request.mode == MODE_REPLACE and not request.confirm_replace:
            raise InvalidRequest("Replacing the content of a collection has to be confirmed.")

        info = await self._describe(collection)
        model = self._model(request, info)
        plan = self._plan(request.source)
        if request.mode == MODE_ADD and not await self._client.supports_update_without_delete():
            raise IngestTooOld(
                "This ingester cannot update without deleting. Use Replace, or upgrade the "
                "ingester to a release with the delete_vanished run option."
            )
        await self._refuse_while_blocked()

        connection = self.connection
        job_ids = {kind: catalog.managed_job_id(connection, collection, kind)
                   for kind in (SOURCE_UPLOAD, SOURCE_FOLDER)}
        await self._refuse_while_running(list(job_ids.values()))

        job = catalog.build_job(
            kind=plan.kind,
            connection=connection,
            collection=collection,
            model=model,
            source_path=plan.path,
            include=plan.include,
            exclude=plan.exclude,
        )
        job_id = str(job["id"])
        repo = catalog.JobsFileRepository(self._settings.papaia_config_dir)
        siblings = catalog.other_jobs_for(repo.snapshot().document, collection, {job_id})
        foreign = [sibling for sibling in siblings if not catalog.is_managed(sibling)]
        if request.mode == MODE_REPLACE and foreign and not request.confirm_other_jobs:
            raise Conflict(
                "Other ingest jobs write to this collection and would lose their content: "
                + ", ".join(sorted(foreign)),
                {"other_jobs": sorted(foreign)},
            )

        connection_names = self._connection_names()
        written = repo.upsert(
            job, lambda document: catalog.catalog_problems(document, job_id, connection_names)
        )
        try:
            await self._load(job_id, job)
        except IngestError:
            if not repo.restore(written):
                log.warning("jobs.yaml changed again; job %s was not rolled back", job_id)
            raise

        body: dict[str, Any] = (
            {"mode": "upsert", "delete_vanished": False}
            if request.mode == MODE_ADD
            else {"mode": "full", "full_scope": "collection", "force": bool(siblings)}
        )
        try:
            run_id = await self._client.run(job_id, body)
        except IngestRejected as exc:
            if exc.status == 409:
                raise Conflict("A run of this job is still working; wait for it.") from exc
            raise

        if plan.batch is not None:
            self.upload_store().mark_embedding(
                plan.batch.id,
                run_id=run_id,
                job_id=job_id,
                collection=collection,
                connection=connection,
                mode=request.mode,
            )
        write_audit_entry(
            self._settings.papaia_config_dir,
            user=user,
            action="rag.ingest.run.start",
            target=collection,
            params=redact_params(
                {
                    "connection": connection,
                    "mode": request.mode,
                    "source": plan.kind,
                    "upload": plan.batch.id if plan.batch else None,
                    "files": plan.counts.files,
                    "bytes": plan.counts.bytes,
                    "model": model,
                    "run_id": run_id,
                    "job_id": job_id,
                }
            ),
        )
        return StartedRun(run_id, job_id, plan.counts.files, plan.counts.bytes)

    async def _describe(self, collection: str) -> CollectionInfo:
        try:
            info = await self._store.describe(collection)
        except InvalidInput as exc:
            raise InvalidRequest(str(exc)) from exc
        if info is None:
            raise NotFound(collection)
        return info

    @staticmethod
    def _model(request: StartRequest, info: CollectionInfo) -> str:
        try:
            asked = validate_embedding_model(request.model)
        except InvalidInput as exc:
            raise InvalidRequest(str(exc)) from exc
        recorded = info.embedding_model
        if request.mode == MODE_ADD:
            if recorded and asked and asked != recorded:
                raise InvalidRequest(
                    f"The collection records the model {recorded!r}. Adding with {asked!r} "
                    "would mix two models; use Replace to change the model."
                )
            model = recorded or asked
        else:
            model = asked or recorded
        if not model:
            raise InvalidRequest(
                "Name the embedding model: the collection has none recorded yet."
            )
        return model

    def _plan(self, source: SourceSpec) -> _Plan:
        if source.kind == SOURCE_UPLOAD:
            if not source.batch:
                raise InvalidRequest("Choose an upload.")
            store = self.upload_store()
            batch = store.get(source.batch)
            if batch.state == uploads.STATE_EMBEDDING:
                raise Conflict("This upload is already being embedded.")
            documents = store.documents(batch)
            rels = source.paths or ("",)
            include = documents.include_patterns(rels)
            counts = documents.count(rels)
            exclude = None
            path = documents.container_path()
        elif source.kind == SOURCE_FOLDER:
            documents = self.folder()
            batch = None
            if not source.paths:
                raise InvalidRequest("Select at least one file or folder.")
            include = documents.include_patterns(source.paths)
            counts = documents.count(source.paths)
            exclude = list(_FOLDER_EXCLUDE)
            path = documents.container_path()
        else:
            raise InvalidRequest("The source must be 'upload' or 'folder'.")
        if counts.files == 0:
            raise InvalidRequest("There is nothing to embed in that selection.")
        return _Plan(source.kind, path, include, exclude, counts, batch)

    def _connection_names(self) -> set[str] | None:
        snapshot = IngestFileRepository(self._settings.papaia_config_dir).snapshot()
        if snapshot.structural_error is not None:
            return None
        return {str(entry.get("name")) for entry in snapshot.entries}

    @staticmethod
    async def _refuse_while_blocked() -> None:
        """A restore, an upgrade or a stack action takes the ingester down or rewrites its files."""
        for kind, label in (
            (runner.RESTORE_KIND, "restore"),
            (runner.UPGRADE_KIND, "upgrade"),
            (runner.STACK_KIND, "stack action"),
        ):
            try:
                active = await runner.find_runner(kind)
            except runner.RunnerError:
                return
            if active is not None and active.is_running:
                raise Conflict(f"A {label} is running; embedding has to wait until it is done.")

    async def _refuse_while_running(self, job_ids: list[str]) -> None:
        for job_id in job_ids:
            running = await self._client.list_runs(job_id=job_id, status="running", limit=1)
            if running:
                raise Conflict(
                    "Another embedding run for this collection is still working. "
                    "Wait for it to finish, or abort it.",
                    {"run_id": running[0].get("run_id")},
                )

    async def _load(self, job_id: str, job: dict[str, Any]) -> None:
        """Make the ingester read the catalog now, and check that it serves what was written."""
        await reload_and_verify(
            self._client, job_id, lambda detail: _served_as_written(detail, job)
        )

    # ── following ───────────────────────────────────────────────────────────

    async def run_view(self, run_id: str, *, collection: str | None = None) -> RunView:
        payload = await self._client.get_run(run_id)
        if payload is None:
            raise NotFound(run_id)
        view = run_view_from(payload)
        if view.active and collection:
            view = replace(
                view, live_chunks=await self._store.count_run_points(collection, run_id)
            )
        return view

    async def history(self, collection: str, *, limit: int = 5) -> list[RunView]:
        """The latest runs of both managed jobs of a collection, newest first."""
        views: list[RunView] = []
        for kind in (SOURCE_UPLOAD, SOURCE_FOLDER):
            job_id = catalog.managed_job_id(self.connection, collection, kind)
            rows = await self._client.list_runs(job_id=job_id, limit=limit)
            views.extend(run_view_from(row) for row in rows)
        views.sort(key=lambda view: view.started_at, reverse=True)
        return views[:limit]

    async def abort(self, run_id: str, *, user: str) -> None:
        try:
            await self._client.abort(run_id)
        except IngestRejected as exc:
            if exc.status == 404:
                raise NotFound(run_id) from exc
            if exc.status == 409:
                raise Conflict("That run is not running any more.") from exc
            raise
        write_audit_entry(
            self._settings.papaia_config_dir,
            user=user,
            action="rag.ingest.run.abort",
            target=run_id,
            params={"connection": self.connection},
        )


# ---------------------------------------------------------------------------
# After a run
# ---------------------------------------------------------------------------


@dataclass
class Reconciled:
    """What one pass did, for the log and the tests."""

    consumed: list[str]
    kept: list[str]
    expired: list[str]
    orphans: int = 0
    # Uploads whose run is still working or could not be asked about.
    waiting: int = 0


def _audit(settings: Settings, action: str, target: str, **params: Any) -> None:
    write_audit_entry(
        settings.papaia_config_dir,
        user="manager",
        action=action,
        target=target,
        params=redact_params(params),
    )


async def reconcile(settings: Settings, *, transport: Any = None) -> Reconciled:
    """Apply the clean-up rules to every upload, once. Never raises for an ingester problem.

    * an upload whose run ended `success` with no failed document: remove it;
    * one whose run failed, stopped, or had failed documents: keep it, with the reason;
    * one whose run the ingester no longer knows: keep it (the files are the only trace left);
    * one past the time limit, in any state except a run that is verifiably still working:
      remove it, because a forgotten upload is the risk this exists to prevent.
    """
    result = Reconciled([], [], [])
    if not rag_active(settings.papaia_config_dir):
        return result
    docs = documents_dir(settings.papaia_config_dir, settings.papaia_workspace_dir)
    if docs.path is None:
        return result
    store = uploads.new_store(settings, docs.path)
    working: set[str] = set()

    pending = [b for b in store.batches() if b.state == uploads.STATE_EMBEDDING and b.run_id]
    token = rag_secrets(settings.papaia_config_dir).ingest_api_token
    if pending and token:
        async with IngestClient(
            settings.qdrant_ingest_url, token, transport=transport
        ) as client:
            for batch in pending:
                try:
                    payload = await client.get_run(str(batch.run_id))
                except IngestError:
                    continue  # cannot tell: leave it, the time limit still applies
                try:
                    _settle(settings, store, batch, payload, result, working)
                except IngestError:
                    log.warning("upload %s could not be removed; it will be tried again", batch.id)

    ttl = settings.ingest_upload_ttl_hours
    for batch in store.expired(ttl, keep=frozenset(working)):
        try:
            store.remove(batch.id)
        except IngestError:
            log.warning("upload %s is past its time limit and could not be removed", batch.id)
            continue
        result.expired.append(batch.id)
        _audit(
            settings, "rag.ingest.upload.expire", batch.id,
            files=batch.files, bytes=batch.bytes, owner=batch.owner, hours=ttl,
        )
    for path in store.orphans(ttl):
        store.remove_orphan(path)
        result.orphans += 1
    for batch in store.dropped_manifests():
        store.drop_manifest(batch.id)
    if result.orphans:
        _audit(settings, "rag.ingest.upload.expire", "orphans", count=result.orphans, hours=ttl)
    result.waiting = sum(1 for b in store.batches() if b.state == uploads.STATE_EMBEDDING)
    return result


def _settle(
    settings: Settings,
    store: uploads.UploadStore,
    batch: uploads.Batch,
    payload: dict[str, Any] | None,
    result: Reconciled,
    working: set[str],
) -> None:
    if payload is None:
        store.mark_kept(batch.id, "The ingester no longer knows the run; the files were kept.")
        result.kept.append(batch.id)
        return
    view = run_view_from(payload)
    if view.active:
        working.add(batch.id)
        return
    if view.clean:
        store.remove(batch.id)
        result.consumed.append(batch.id)
        _audit(
            settings, "rag.ingest.upload.consume", batch.id,
            files=batch.files, bytes=batch.bytes, run_id=view.run_id, owner=batch.owner,
        )
        return
    note = view.error or (
        f"{view.failed_documents} document(s) failed" if view.failed_documents else view.label
    )
    store.mark_kept(batch.id, note)
    result.kept.append(batch.id)
    _audit(
        settings, "rag.ingest.upload.keep", batch.id,
        run_id=view.run_id, status=view.status, failed=view.failed_documents,
    )
