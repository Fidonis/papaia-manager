"""Managing the ingester's jobs: the list, the editor, the runs and what hangs off them.

The ingester's own web interface did this. The manager can only reach what is shared with it:
the catalog files in `ai/rag/catalog/` (it is a second writer of `jobs.yaml`) and the
ingester's REST API with its static token. Everything here is built on that, and a few
properties of the ingester decide how:

* **The ingester refuses a catalog as a whole.** One invalid job anywhere keeps the previous
  catalog serving, and every job of the new file is then *not loaded*. So a list built from
  `GET /v1/jobs` alone hides the jobs that matter most. `overview` merges the file, the
  ingester's registry and its error report, and says for every job whether it is serving,
  serving an older definition, not loaded (and why) or not yet read.
* **A write is checked three times.** The schema mirror (`jobspec`) names the field, the
  ingester's `validate` (when it has it) gives its own verdict without writing anything, and
  after the write the ingester is asked to reload and the service checks that it serves what
  was written. A job the ingester refuses is rolled back; a job that is fine in a file in
  which another job is broken is kept, and the answer says it is not active yet and why.
* **Pause is `enabled: false` in the file.** The ingester's own pause is in memory only.
* **A job that is in the file is never an orphan.** After a restart the ingester loads the
  valid subset of a catalog and drops the rest, and the dropped jobs then look like leftovers
  with a Delete that purges their points. The ids in `jobs.yaml` decide, not the ingester's
  registry.
* **An id never changes.** Every point id derives from the job id, so a rename would turn all
  of a job's content into leftovers. The editor offers Duplicate instead.

* **A job's runs go with it.** The ingester prunes a job's history only after one of the job's
  own runs, which never comes for a deleted job, so `delete` also deletes the runs, once the
  ingester has let go of the job. What the job embedded is a separate, explicit choice.

The jobs of the Embedding page (`mgr-` prefix) are listed and can be run and watched, never
edited or paused from here: their next run overwrites any change. They can be deleted, with
their runs, and come back with the next embedding, which writes the job again.
"""
from __future__ import annotations

import contextlib
import copy
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

import yaml

from app.config import Settings
from app.core.audit import redact_params, write_audit_entry
from app.core.ingest import catalog, display, job_forms, jobspec, schedules
from app.core.ingest import secrets as secrets_store
from app.core.ingest.catalog_load import LoadOutcome, loaded_as_written, reload_and_verify
from app.core.ingest.client import IngestClient
from app.core.ingest.errors import (
    CatalogRejected,
    Conflict,
    IngestError,
    IngestRejected,
    IngestTooOld,
    IngestUnavailable,
    InvalidJob,
    InvalidRequest,
    NotFound,
)
from app.core.ingest.jobspec import Issue, RuleContext
from app.core.ingest.runs import (
    JOB_MODE_TEXT,
    RunView,
    refuse_while_blocked,
    run_view_from,
)
from app.core.rag import (
    environment_credentials,
    ingest_timezone,
    rag_backend,
    rag_secrets,
)
from app.core.schedule import ScheduleError
from app.core.vectordb import catalog_io
from app.core.vectordb.ingest_file import IngestFileRepository, entry_etag

log = logging.getLogger(__name__)

# The states of a job in the list.
STATE_LOADED = "loaded"
STATE_STALE = "stale"
STATE_NOT_LOADED = "not_loaded"
STATE_PENDING = "pending"
STATE_REGISTRY_ONLY = "registry_only"
STATE_UNKNOWN = "unknown"

STATE_TEXT = {
    STATE_LOADED: "Active",
    STATE_STALE: "Running its previous version",
    STATE_NOT_LOADED: "Not loaded",
    STATE_PENDING: "Waiting for the ingester",
    STATE_REGISTRY_ONLY: "Not in jobs.yaml",
    STATE_UNKNOWN: "Unknown",
}

FEATURE_PROGRESS = "run_progress"
FEATURE_DOCUMENTS = "documents"
FEATURE_VALIDATE = "validate"
FEATURE_SECRETS = "secret_store"
FEATURE_DELETE_RUNS = "delete_runs"


# ---------------------------------------------------------------------------
# What a page gets
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IngesterState:
    """Whether the ingester can be used, and what it offers."""

    reachable: bool = False
    # The token works: the ingester answered an authenticated request.
    usable: bool = False
    reason: str = ""
    status: str = "unknown"
    version: str = ""
    features: frozenset[str] = frozenset()
    deps: Mapping[str, bool] = field(default_factory=dict)
    config_error: str | None = None
    config_valid: bool | None = None
    config_applied: bool | None = None
    loaded_at: str | None = None
    config_path: str | None = None

    def has(self, feature: str) -> bool:
        return feature in self.features

    @property
    def refused(self) -> bool:
        """The ingester found problems in the catalog and keeps its previous one."""
        return self.usable and self.config_applied is False


@dataclass(frozen=True)
class JobRow:
    id: str
    state: str
    state_text: str
    note: str
    problems: tuple[str, ...]
    enabled: bool
    managed: bool
    in_file: bool
    description: str
    full_scope: str
    source_type: str
    source_title: str
    source_label: str
    source_detail: str
    connection: str
    collection: str
    mode: str
    mode_label: str
    schedule: str
    next_run_at: str | None
    paused_in_ingester: bool
    last_run: RunView | None
    active_run: RunView | None
    documents: int | None
    chunks: int | None
    etag: str

    @property
    def runnable(self) -> bool:
        return self.enabled and self.state in (STATE_LOADED, STATE_STALE)


@dataclass(frozen=True)
class CatalogView:
    ingester: IngesterState
    jobs: tuple[JobRow, ...]
    # Problems that stop the file from being read at all (not YAML, wrong shape).
    file_problem: str | None
    exists: bool
    writable: bool
    revision: str
    # Catalog-level problems of the ingester that belong to no job of this list.
    problems_elsewhere: tuple[str, ...]
    # The ingester serves a path other than the writable one (an installation that predates it).
    legacy: bool
    # Ids the ingester has state for that no job of the file explains.
    leftovers: int | None
    connections: tuple[str, ...]

    @property
    def editable(self) -> bool:
        return self.file_problem is None and self.writable and not self.legacy


@dataclass(frozen=True)
class SaveResult:
    job_id: str
    created: bool
    # The ingester serves the job as written.
    applied: bool
    # The ingester was asked and answered (False when it could not be reached).
    verified: bool
    # Why the change is saved but not active: problems elsewhere in the file, or no ingester.
    elsewhere: tuple[str, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class DeleteResult:
    job_id: str
    deleted_points: int = 0
    deleted_rows: int = 0
    purged: bool = False
    note: str = ""
    deleted_runs: int = 0


@dataclass(frozen=True)
class RunsDeleteResult:
    """What deleting a job's run history did, or with `dry_run` what it would do."""

    job_id: str
    # The runs (and their log lines) that were deleted, or that a real call would delete.
    matched: int = 0
    matched_events: int = 0
    deleted_runs: int = 0
    deleted_events: int = 0
    # Runs in the period that are still working. They are never deleted.
    skipped_running: int = 0
    dry_run: bool = False


@dataclass(frozen=True)
class RunOptions:
    mode: str | None = None
    full_scope: str | None = None
    dry_run: bool = False
    skip_sync: bool = False
    force: bool = False
    delete_vanished: bool = True
    confirm_rebuild: bool = False
    confirm_collection: bool = False


@dataclass(frozen=True)
class Credential:
    name: str
    origin: str  # "environment" or "store"
    readable: bool
    used_by: tuple[str, ...]
    # A stored value of the same name exists but is never used: the environment wins.
    shadowed: bool = False

    @property
    def deletable(self) -> bool:
        return self.origin == "store" and not self.used_by


@dataclass(frozen=True)
class CredentialsView:
    items: tuple[Credential, ...]
    # None while the ingester could not be asked; False for one that does not read the store.
    supported: bool | None
    has_key: bool
    writable: bool
    file_problem: str | None
    ingester: IngesterState


@dataclass(frozen=True)
class _Candidate:
    entry: dict[str, Any]
    document: dict[str, Any]
    defaults: dict[str, Any]
    issues: list[Issue]
    # The entry satisfies the schema, so the loader's rules can be applied to it.
    schema_ok: bool


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _user_of(user: str) -> str:
    return user or "manager"


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


class JobsService:
    """The job management pages' reads and writes, against the catalog and the ingester."""

    def __init__(self, settings: Settings, *, client: IngestClient) -> None:
        self._settings = settings
        self._client = client
        self._config_dir = settings.papaia_config_dir
        self._repo = catalog.JobsFileRepository(self._config_dir)

    # ── context ─────────────────────────────────────────────────────────────

    def _connection_names(self) -> frozenset[str] | None:
        snapshot = IngestFileRepository(self._config_dir).snapshot()
        if snapshot.structural_error is not None:
            return None
        return frozenset(str(entry.get("name")) for entry in snapshot.entries)

    def _rules(self) -> RuleContext:
        """What the ingester checks a job against.

        The credentials are deliberately not checked here: the ingester's environment is
        more than `ai/rag/.env`, so a name the manager does not know may still resolve there.
        The editor only offers names it knows; the ingester's `validate` has the last word.
        """
        return RuleContext(
            connections=self._connection_names(),
            secrets=None,
            system_collections=rag_backend(self._config_dir).system_collections,
        )

    @staticmethod
    def _defaults(document: Mapping[str, Any]) -> dict[str, Any]:
        defaults = document.get("defaults")
        return dict(defaults) if isinstance(defaults, Mapping) else {}

    def timezone(self) -> str:
        return ingest_timezone(self._config_dir)

    def connections(self) -> list[str]:
        """The names of the connections of the ingester's store, for the editor's selector."""
        return sorted(self._connection_names() or ())

    def _audit(self, user: str, action: str, target: str, **params: Any) -> None:
        write_audit_entry(
            self._config_dir,
            user=_user_of(user),
            action=action,
            target=target,
            params=redact_params(params),
        )

    # ── the ingester ────────────────────────────────────────────────────────

    async def ingester(self) -> tuple[IngesterState, dict[str, Any]]:
        """The ingester's state and its catalog report (empty when it is not usable)."""
        try:
            health = await self._client.health()
        except IngestUnavailable as exc:
            return IngesterState(reason=str(exc)), {}
        except IngestRejected as exc:
            return IngesterState(reason=f"The ingester answered with an error: {exc.detail}"), {}
        raw_deps = health.get("deps")
        base = IngesterState(
            reachable=True,
            status=str(health.get("status") or "unknown"),
            version=str(health.get("version") or ""),
            features=frozenset(str(f) for f in health.get("features") or []),
            deps=(
                {str(k): bool(v) for k, v in raw_deps.items()}
                if isinstance(raw_deps, dict)
                else {}
            ),
            config_error=health.get("config_error") or None,
        )
        try:
            config = await self._client.config()
        except IngestUnavailable as exc:
            return replace(base, reason=str(exc)), {}
        except IngestRejected as exc:
            reason = f"The ingester answered with an error: {exc.detail}"
            return replace(base, reason=reason), {}
        valid, applied, path = config.get("valid"), config.get("applied"), config.get("path")
        return (
            replace(
                base,
                usable=True,
                config_valid=valid if isinstance(valid, bool) else None,
                config_applied=applied if isinstance(applied, bool) else None,
                loaded_at=str(config["loaded_at"]) if config.get("loaded_at") else None,
                config_path=str(path) if path else None,
            ),
            config,
        )

    # ── the list ────────────────────────────────────────────────────────────

    async def overview(self) -> CatalogView:
        snapshot = self._repo.snapshot()
        state, config = await self.ingester()
        document = snapshot.document
        defaults = self._defaults(document)
        file_jobs = (
            [job for job in document.get("jobs") or [] if isinstance(job, dict)]
            if snapshot.structural_error is None
            else []
        )
        file_ids = {str(job.get("id")) for job in file_jobs if isinstance(job.get("id"), str)}

        registry: dict[str, dict[str, Any]] = {}
        running: dict[str, RunView] = {}
        errors: list[dict[str, Any]] = []
        leftovers: int | None = None
        if state.usable:
            errors = [e for e in config.get("errors") or [] if isinstance(e, dict)]
            try:
                registry = {
                    str(row.get("id")): row
                    for row in await self._client.list_jobs()
                    if row.get("id")
                }
                for payload in await self._client.list_runs(status="running", limit=100):
                    view = run_view_from(payload)
                    running.setdefault(view.job_id, view)
            except IngestError as exc:
                log.warning("could not read the job list of the ingester: %s", exc)
            try:
                leftovers = sum(
                    1 for o in await self._client.orphans() if o.get("job_id") not in file_ids
                )
            except IngestError:
                leftovers = None

        by_job: dict[str, list[dict[str, Any]]] = {}
        elsewhere: list[str] = []
        for item in errors:
            job_id = item.get("job_id")
            text = f"{item.get('field')}: {item.get('message')}"
            if isinstance(job_id, str) and job_id in file_ids:
                by_job.setdefault(job_id, []).append(item)
            elif isinstance(job_id, str):
                elsewhere.append(f"{job_id}: {text}")
            else:
                elsewhere.append(text)

        rows: list[JobRow] = []
        for raw in file_jobs:
            job_id = raw.get("id")
            if not isinstance(job_id, str):
                continue
            rows.append(
                self._row(
                    raw,
                    defaults,
                    registry.get(job_id),
                    by_job.get(job_id, []),
                    running.get(job_id),
                    state,
                )
            )
        for job_id, summary in registry.items():
            if job_id not in file_ids:
                rows.append(self._registry_row(summary, running.get(job_id)))
        rows.sort(key=lambda row: row.id)

        # The manager edits ai/rag/catalog/jobs.yaml. An ingester that serves another file
        # (an installation that predates the catalog folder) would never see the changes.
        legacy = bool(
            state.config_path
            and not state.config_path.replace("\\", "/").endswith("/catalog/jobs.yaml")
        )
        return CatalogView(
            ingester=state,
            jobs=tuple(rows),
            file_problem=snapshot.structural_error,
            exists=snapshot.exists,
            writable=snapshot.writable,
            revision=snapshot.revision,
            problems_elsewhere=tuple(elsewhere),
            legacy=legacy,
            leftovers=leftovers,
            connections=tuple(sorted(self._connection_names() or ())),
        )

    def _row(
        self,
        raw: dict[str, Any],
        defaults: Mapping[str, Any],
        summary: dict[str, Any] | None,
        problems: list[dict[str, Any]],
        active: RunView | None,
        state: IngesterState,
    ) -> JobRow:
        job_id = str(raw.get("id"))
        merged = jobspec.effective(raw, defaults)
        source = _mapping(raw.get("source"))
        target = _mapping(raw.get("target"))
        mode = str(raw.get("mode") or "")
        problem_texts = tuple(f"{p.get('field')}: {p.get('message')}" for p in problems)
        if not state.usable:
            row_state, note = STATE_UNKNOWN, state.reason or "The ingester cannot be reached."
        elif problems:
            row_state, note = STATE_NOT_LOADED, "The ingester does not load this job."
        elif summary is None:
            row_state = STATE_PENDING
            note = (
                "The ingester reads the file every 30 seconds. Open Reload to read it now."
                if state.config_applied is not False
                else "The ingester is not applying changes because of problems elsewhere in "
                "jobs.yaml."
            )
        elif state.config_applied is False:
            row_state = STATE_STALE
            note = (
                "The ingester found problems in jobs.yaml and keeps the previous catalog, so "
                "recent changes to this job are not active yet."
            )
        else:
            row_state, note = STATE_LOADED, ""
        last = summary.get("last_run") if summary else None
        last_view = run_view_from(last) if isinstance(last, dict) else None
        documents = _mapping(summary.get("documents")) if summary else {}
        return JobRow(
            id=job_id,
            state=row_state,
            state_text=STATE_TEXT[row_state],
            note=note,
            problems=problem_texts,
            enabled=raw.get("enabled", True) is not False,
            managed=catalog.is_managed(job_id),
            in_file=True,
            description=str(raw.get("description") or ""),
            full_scope=str(raw.get("full_scope") or "job"),
            source_type=str(source.get("type") or ""),
            source_title=job_forms.source_title(str(source.get("type") or "")),
            source_label=str(source.get("label") or ""),
            source_detail=job_forms.source_detail(source),
            connection=str(target.get("connection") or ""),
            collection=str(target.get("collection") or ""),
            mode=mode,
            mode_label=JOB_MODE_TEXT.get(mode, mode),
            schedule=schedules.describe_block(_mapping(merged.get("schedule"))),
            next_run_at=(
                str(summary["next_run_at"]) if summary and summary.get("next_run_at") else None
            ),
            paused_in_ingester=bool(summary and summary.get("paused")),
            last_run=last_view,
            active_run=active if active is not None else (
                last_view if last_view is not None and last_view.active else None
            ),
            documents=documents.get("total") if isinstance(documents.get("total"), int) else None,
            chunks=documents.get("chunks") if isinstance(documents.get("chunks"), int) else None,
            etag=entry_etag(raw),
        )

    def _registry_row(self, summary: dict[str, Any], active: RunView | None) -> JobRow:
        job_id = str(summary.get("id"))
        source = _mapping(summary.get("source"))
        block = {"cron": summary.get("cron"), "every": summary.get("every")}
        mode = str(summary.get("mode") or "")
        last = summary.get("last_run")
        last_view = run_view_from(last) if isinstance(last, dict) else None
        return JobRow(
            id=job_id,
            state=STATE_REGISTRY_ONLY,
            state_text=STATE_TEXT[STATE_REGISTRY_ONLY],
            note=(
                "The ingester is running this job, but it is not in jobs.yaml. It reads the "
                "file every 30 seconds, or the file it serves is another one."
            ),
            problems=(),
            enabled=bool(summary.get("enabled", True)),
            managed=catalog.is_managed(job_id),
            in_file=False,
            description="",
            full_scope="job",
            source_type=str(source.get("type") or ""),
            source_title=job_forms.source_title(str(source.get("type") or "")),
            source_label=str(source.get("label") or ""),
            source_detail="",
            connection=str(summary.get("connection") or ""),
            collection=str(summary.get("collection") or ""),
            mode=mode,
            mode_label=JOB_MODE_TEXT.get(mode, mode),
            schedule=schedules.describe_block({k: v for k, v in block.items() if v}),
            next_run_at=str(summary["next_run_at"]) if summary.get("next_run_at") else None,
            paused_in_ingester=bool(summary.get("paused")),
            last_run=last_view,
            active_run=active,
            documents=None,
            chunks=None,
            etag="",
        )

    async def job_row(self, job_id: str) -> JobRow:
        view = await self.overview()
        for row in view.jobs:
            if row.id == job_id:
                return row
        raise NotFound(job_id)

    # ── opening a job ───────────────────────────────────────────────────────

    def editor(self, job_id: str | None) -> dict[str, Any]:
        """Everything the editor starts from: the job's state, its etag, the defaults."""
        snapshot = self._repo.snapshot()
        if snapshot.structural_error is not None:
            raise CatalogRejected(f"jobs.yaml cannot be read: {snapshot.structural_error}")
        defaults = self._defaults(snapshot.document)
        timezone = self.timezone()
        if job_id is None:
            state = job_forms.blank_state(defaults, timezone=timezone)
            etag = ""
        else:
            raw = self._find(snapshot.document, job_id)
            if raw is None:
                raise NotFound(job_id)
            state = job_forms.form_state(raw, defaults, timezone=timezone)
            etag = entry_etag(raw)
        defaulted = {
            f"{section}.{key}": value
            for section, values in jobspec.known_defaults(defaults).items()
            if isinstance(values, Mapping)
            for key, value in values.items()
        }
        return {
            "state": state,
            "etag": etag,
            "managed": bool(job_id and catalog.is_managed(job_id)),
            "defaults": defaulted,
            "inherited": jobspec.inherited_sections(defaults),
            "timezone": timezone,
        }

    @staticmethod
    def _find(document: Mapping[str, Any], job_id: str) -> dict[str, Any] | None:
        for job in document.get("jobs") or []:
            if isinstance(job, dict) and job.get("id") == job_id:
                return job
        return None

    def authored_job(self, job_id: str) -> tuple[dict[str, Any], dict[str, Any], str]:
        """The job as written, the catalog defaults and the job's etag."""
        snapshot = self._repo.snapshot()
        raw = self._find(snapshot.document, job_id)
        if raw is None:
            raise NotFound(job_id)
        return copy.deepcopy(raw), self._defaults(snapshot.document), entry_etag(raw)

    # ── checking a job before it is written ─────────────────────────────────

    def _candidate(
        self, state: Mapping[str, Any], *, create: bool, original_id: str | None
    ) -> _Candidate:
        """The entry the editor's state becomes, the catalog with it in, and what is wrong."""
        snapshot = self._repo.snapshot()
        if snapshot.structural_error is not None:
            raise CatalogRejected(f"jobs.yaml cannot be changed: {snapshot.structural_error}")
        document = copy.deepcopy(snapshot.document)
        defaults = self._defaults(document)
        entry, issues = job_forms.authored(state, defaults)
        job_id = str(entry.get("id") or "")
        if create:
            problem = jobspec.id_problem(job_id)
            if problem:
                issues.append(Issue(job_id or None, "id", problem))
            elif self._find(document, job_id) is not None:
                issues.append(Issue(job_id, "id", "a job with this id already exists"))
        else:
            if original_id is not None and job_id != original_id:
                issues.append(
                    Issue(
                        original_id,
                        "id",
                        "the id of a job cannot change, because its content is tied to it; "
                        "duplicate the job instead",
                    )
                )
            if catalog.is_managed(original_id or job_id):
                issues.append(
                    Issue(
                        job_id,
                        "id",
                        "this job belongs to the Embedding page; its next run would overwrite "
                        "a change",
                    )
                )
        spec, schema_issues = jobspec.parse_job(entry, defaults)
        issues.extend(schema_issues)
        jobs = document.setdefault("jobs", [])
        for index, existing in enumerate(jobs):
            if isinstance(existing, dict) and existing.get("id") == job_id:
                jobs[index] = entry
                break
        else:
            jobs.append(entry)
        return _Candidate(entry, document, defaults, issues, spec is not None)

    async def _problems(self, candidate: _Candidate) -> list[Issue]:
        """Everything wrong with a candidate: the schema, the loader's rules, the ingester."""
        issues = list(candidate.issues)
        job_id = str(candidate.entry.get("id") or "")
        if candidate.schema_ok:
            issues.extend(jobspec.check_one(candidate.document, job_id, self._rules()))
        if not issues:
            issues.extend(await self._ask_ingester(candidate.document, job_id))
        return issues

    async def validate(
        self, state: Mapping[str, Any], *, create: bool, original_id: str | None = None
    ) -> list[Issue]:
        """The problems with an edit, field by field. Nothing is written."""
        return await self._problems(
            self._candidate(state, create=create, original_id=original_id)
        )

    async def _ask_ingester(self, document: Mapping[str, Any], job_id: str) -> list[Issue]:
        """The ingester's own verdict on the candidate catalog, restricted to this job."""
        try:
            result = await self._client.validate(catalog_io.dump_document(document))
        except IngestError:
            return []
        if result is None:
            return []
        return [
            Issue(job_id, str(item.get("field")), str(item.get("message")))
            for item in result.get("errors") or []
            if isinstance(item, dict) and item.get("job_id") == job_id
        ]

    # ── saving ──────────────────────────────────────────────────────────────

    async def save(
        self,
        state: Mapping[str, Any],
        *,
        create: bool,
        etag: str | None,
        user: str,
        original_id: str | None = None,
    ) -> SaveResult:
        """Write a job, make the ingester load it, check that it did, undo it if it refused."""
        candidate = self._candidate(state, create=create, original_id=original_id)
        issues = await self._problems(candidate)
        if issues:
            raise InvalidJob(issues)
        entry, defaults = candidate.entry, candidate.defaults
        job_id = str(entry.get("id") or "")

        context = self._rules()
        written = self._repo.put_job(
            entry,
            create=create,
            etag=etag or None,
            rules=lambda doc: [str(i) for i in jobspec.check_one(doc, job_id, context)],
        )
        outcome, note = await self._after_write(written, job_id, jobspec.effective(entry, defaults))
        target = _mapping(entry.get("target"))
        self._audit(
            user,
            "rag.ingest.job.create" if create else "rag.ingest.job.update",
            job_id,
            connection=target.get("connection"),
            collection=target.get("collection"),
            source=_mapping(entry.get("source")).get("type"),
            applied=outcome.applied if outcome else False,
        )
        return SaveResult(
            job_id=job_id,
            created=create,
            applied=bool(outcome and outcome.applied),
            verified=outcome is not None,
            elsewhere=outcome.elsewhere if outcome else (),
            note=note,
        )

    async def _after_write(
        self, written: catalog.Written, job_id: str, wanted: dict[str, Any]
    ) -> tuple[LoadOutcome | None, str]:
        """Reload and check; undo the write if the ingester refuses the job.

        Returns the outcome (None when the ingester could not be asked) and a note.
        """
        try:
            outcome = await reload_and_verify(
                self._client,
                job_id,
                lambda detail: loaded_as_written(detail, wanted),
                tolerate_other_problems=True,
            )
        except CatalogRejected:
            if not self._repo.restore(written):
                log.warning("jobs.yaml changed again; job %s was not rolled back", job_id)
            raise
        except IngestUnavailable as exc:
            return None, f"Saved, but not checked: {exc}"
        except IngestError:
            if not self._repo.restore(written):
                log.warning("jobs.yaml changed again; job %s was not rolled back", job_id)
            raise
        return outcome, ""

    # ── enabling, disabling, deleting ───────────────────────────────────────

    async def set_enabled(self, job_id: str, enabled: bool, *, user: str) -> SaveResult:
        raw, defaults, _ = self.authored_job(job_id)
        if catalog.is_managed(job_id):
            raise Conflict("This job belongs to the Embedding page and cannot be changed here.")
        if enabled:
            # A disabled job is exempt from the cross-job rules, so enabling can fail them.
            candidate = copy.deepcopy(self._repo.snapshot().document)
            target = self._find(candidate, job_id)
            if target is not None:
                target.pop("enabled", None)
            problems = jobspec.check_one(candidate, job_id, self._rules())
            if problems:
                raise InvalidJob(problems)
        context = self._rules()
        written = self._repo.set_enabled(
            job_id,
            enabled,
            rules=(lambda doc: [str(i) for i in jobspec.check_one(doc, job_id, context)])
            if enabled
            else None,
        )
        wanted = jobspec.effective({**raw, "enabled": enabled}, defaults)
        outcome, note = await self._after_write(written, job_id, wanted)
        self._audit(
            user, "rag.ingest.job.enable" if enabled else "rag.ingest.job.disable", job_id
        )
        return SaveResult(
            job_id,
            created=False,
            applied=bool(outcome and outcome.applied),
            verified=outcome is not None,
            elsewhere=outcome.elsewhere if outcome else (),
            note=note,
        )

    async def delete(
        self, job_id: str, *, etag: str | None, purge: bool, user: str
    ) -> DeleteResult:
        """Remove a job from the catalog with its runs, and (on request) its content.

        The runs go only once the ingester has let go of the job: while it still serves it (it
        keeps its previous catalog when another job is invalid) a run could start and write to
        the history just deleted. A step that fails is reported in the note and does not stop
        the others.
        """
        if self._find(self._repo.snapshot().document, job_id) is None:
            raise NotFound(job_id)
        await self._refuse_while_running(job_id, "deleted")
        self._repo.remove_job(job_id, etag=etag or None)
        notes: list[str] = []
        points = rows = deleted_runs = 0
        purged = False
        try:
            await self._client.reload()
            deleted_runs = await self._delete_history_of_removed(job_id, notes)
            if purge:
                orphan = next(
                    (o for o in await self._client.orphans() if o.get("job_id") == job_id), None
                )
                if orphan is not None:
                    result = await self._client.delete_orphan(job_id)
                    points = int(result.get("deleted_points") or 0)
                    rows = int(result.get("deleted_rows") or 0)
                    purged = True
                else:
                    notes.append("The job had embedded nothing, so there was nothing to remove.")
        except IngestUnavailable as exc:
            notes.append(
                f"The job was removed from jobs.yaml, but the ingester could not be reached: "
                f"{exc} Its content stays in the collection and can be removed under "
                "Leftovers once the ingester is back, and its runs can be deleted on the "
                "Runs page."
            )
        except IngestError as exc:
            notes.append(f"The job was removed from jobs.yaml, but cleaning up failed: {exc}")
        self._audit(
            user,
            "rag.ingest.job.delete",
            job_id,
            purge=purge,
            deleted_points=points,
            deleted_rows=rows,
            deleted_runs=deleted_runs,
        )
        return DeleteResult(job_id, points, rows, purged, " ".join(notes), deleted_runs)

    async def _delete_history_of_removed(self, job_id: str, notes: list[str]) -> int:
        """Delete the runs of a job that was just removed from the file. Never raises."""
        try:
            if await self._client.job(job_id) is not None:
                notes.append(
                    "The ingester still serves this job, because it keeps its previous catalog "
                    "while another job is invalid. Its runs were kept; delete them on the Runs "
                    "page once the catalog is repaired."
                )
                return 0
            result = await self._client.delete_runs(job_id)
        except IngestError as exc:
            notes.append(f"Its runs could not be deleted: {exc}")
            return 0
        if result is None:
            notes.append(
                "This ingester cannot delete runs, so the runs of the job stay in its history."
            )
            return 0
        if int(result.get("skipped_running") or 0):
            notes.append("A run that was still working was kept.")
        return int(result.get("deleted_runs") or 0)

    async def delete_runs(
        self,
        job_id: str,
        *,
        since: date | None = None,
        until: date | None = None,
        dry_run: bool = False,
        user: str,
    ) -> RunsDeleteResult:
        """Delete the run history of a job: all of it, or the whole days `since` to `until`.

        The days are days in the zone the ingester schedules in and both are included; a
        missing one leaves that side open. The job need not exist any more (the history of a
        deleted job is what this is for) and may be one of the Embedding page's. A run that is
        still working is never deleted, the ingester keeps it and says how many. What the job
        embedded is not touched. With `dry_run` nothing is deleted and the result says what a
        real call would delete.
        """
        if not jobspec.is_valid_id(job_id):
            raise InvalidRequest(f"{job_id!r} is not a job id.")
        if since is not None and until is not None and since > until:
            raise InvalidRequest("The first day is after the last day.")
        start, end = display.day_bounds(since, until, self.timezone())
        result = await self._client.delete_runs(job_id, since=start, until=end, dry_run=dry_run)
        if result is None:
            raise IngestTooOld(
                "This ingester cannot delete runs. Use an ingester release that has the "
                f"{FEATURE_DELETE_RUNS} feature."
            )
        outcome = RunsDeleteResult(
            job_id=job_id,
            matched=int(result.get("matched") or 0),
            matched_events=int(result.get("matched_events") or 0),
            deleted_runs=int(result.get("deleted_runs") or 0),
            deleted_events=int(result.get("deleted_events") or 0),
            skipped_running=int(result.get("skipped_running") or 0),
            dry_run=dry_run,
        )
        if not dry_run:
            self._audit(
                user,
                "rag.ingest.run.delete",
                job_id,
                since=since.isoformat() if since else None,
                until=until.isoformat() if until else None,
                deleted_runs=outcome.deleted_runs,
                deleted_events=outcome.deleted_events,
                skipped_running=outcome.skipped_running,
            )
        return outcome

    async def _refuse_while_running(self, job_id: str, what: str) -> None:
        try:
            running = await self._client.list_runs(job_id=job_id, status="running", limit=1)
        except IngestError:
            return
        if running:
            raise Conflict(
                f"A run of this job is working. Wait for it, or abort it, before it is {what}.",
                {"run_id": running[0].get("run_id")},
            )

    # ── running ─────────────────────────────────────────────────────────────

    async def run_now(self, job_id: str, options: RunOptions, *, user: str) -> str:
        """Start a run of a job. The destructive modes have to be confirmed."""
        detail = await self._client.job(job_id)
        if detail is None:
            raise NotFound(job_id)
        config = _mapping(detail.get("config"))
        if config.get("enabled") is False:
            raise Conflict("The job is disabled. Enable it first.")
        effective_mode = options.mode or str(config.get("mode") or "")
        if not options.dry_run:
            if effective_mode == "full" and not options.confirm_rebuild:
                raise InvalidRequest("Rebuilding a job's content has to be confirmed.")
            if (
                effective_mode == "full"
                and (options.full_scope or config.get("full_scope")) == "collection"
                and not options.confirm_collection
            ):
                raise InvalidRequest("Replacing the whole collection has to be confirmed.")
        if not options.delete_vanished and effective_mode != "upsert":
            raise InvalidRequest("Keeping vanished files only applies to a job that keeps in sync.")
        await refuse_while_blocked("a run")

        body: dict[str, Any] = {}
        if options.mode:
            body["mode"] = options.mode
        if options.full_scope:
            body["full_scope"] = options.full_scope
        if options.dry_run:
            body["dry_run"] = True
        if options.skip_sync:
            body["skip_sync"] = True
        if options.force:
            body["force"] = True
        if not options.delete_vanished:
            body["delete_vanished"] = False
        try:
            run_id = await self._client.run(job_id, body)
        except IngestRejected as exc:
            if exc.status == 409:
                raise Conflict("A run of this job is already working.") from exc
            raise
        self._audit(user, "rag.ingest.job.run", job_id, run_id=run_id, **body)
        return run_id

    async def abort(self, run_id: str, *, user: str) -> None:
        try:
            await self._client.abort(run_id)
        except IngestRejected as exc:
            if exc.status == 404:
                raise NotFound(run_id) from exc
            if exc.status == 409:
                raise Conflict("That run is not running any more.") from exc
            raise
        self._audit(user, "rag.ingest.run.abort", run_id)

    async def resume(self, job_id: str, *, user: str) -> None:
        """Lift the ingester's own in-memory pause (it survives neither a restart nor a reload)."""
        try:
            await self._client.resume(job_id)
        except IngestRejected as exc:
            if exc.status == 404:
                raise NotFound(job_id) from exc
            raise
        self._audit(user, "rag.ingest.job.resume", job_id)

    async def reload(self) -> IngesterState:
        """Make the ingester read the catalog now."""
        await self._client.reload()
        state, _ = await self.ingester()
        return state

    def schedule_preview(self, raw_plan: Mapping[str, Any]) -> schedules.Preview:
        """What a schedule from the builder compiles to, and when it would run next."""
        try:
            plan = job_forms.plan_from(raw_plan)
        except (ScheduleError, ValueError, TypeError) as exc:
            return schedules.Preview(ok=False, error=str(exc))
        sched_defaults = self.defaults().get("schedule")
        defaults_zone = (
            sched_defaults.get("timezone") if isinstance(sched_defaults, Mapping) else None
        )
        return schedules.preview(
            plan,
            default_timezone=schedules.effective_timezone(None, defaults_zone, self.timezone()),
        )

    # ── runs and files ──────────────────────────────────────────────────────

    async def runs(
        self,
        *,
        job_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        since: str | None = None,
    ) -> list[RunView]:
        params: dict[str, Any] = {"limit": limit}
        if job_id:
            params["job_id"] = job_id
        if status:
            params["status"] = status
        if since:
            params["since"] = since
        result = await self._client.request("GET", "/v1/runs", params=params)
        rows = [r for r in result if isinstance(r, dict)] if isinstance(result, list) else []
        return [run_view_from(row) for row in rows]

    async def run(self, run_id: str) -> RunView:
        payload = await self._client.get_run(run_id)
        if payload is None:
            raise NotFound(run_id)
        return run_view_from(payload)

    async def documents(self, job_id: str, **params: Any) -> dict[str, Any] | None:
        return await self._client.documents(job_id, **params)

    async def preview_files(self, job_id: str, limit: int = 50) -> list[dict[str, Any]]:
        return await self._client.preview(job_id, limit)

    # ── leftovers ───────────────────────────────────────────────────────────

    async def orphans(self) -> list[dict[str, Any]]:
        """What the ingester has state for that is not explained by a job of the file.

        An entry whose id is still in `jobs.yaml` is not a leftover: the ingester dropped
        the job when it loaded the catalog (a secret or a connection went missing, for
        example) and the job will come back when that is repaired. It is listed, marked, and
        can not be deleted.
        """
        in_file = {
            str(job.get("id"))
            for job in self._repo.snapshot().document.get("jobs") or []
            if isinstance(job, dict)
        }
        return [
            {**orphan, "in_file": orphan.get("job_id") in in_file}
            for orphan in await self._client.orphans()
        ]

    async def delete_orphan(self, job_id: str, *, user: str) -> dict[str, Any]:
        snapshot = self._repo.snapshot()
        if self._find(snapshot.document, job_id) is not None:
            raise Conflict(
                f"The job {job_id!r} is still in jobs.yaml, so its content is not left over: "
                "the ingester did not load it. Repair the job instead."
            )
        await self._refuse_while_running(job_id, "cleaned up")
        try:
            result = await self._client.delete_orphan(job_id)
        except IngestRejected as exc:
            if exc.status == 409:
                raise Conflict(f"The job {job_id!r} is still in the ingester's catalog.") from exc
            raise
        self._audit(
            user,
            "rag.ingest.orphan.delete",
            job_id,
            deleted_points=result.get("deleted_points"),
            deleted_rows=result.get("deleted_rows"),
        )
        return result

    # ── the file itself ─────────────────────────────────────────────────────

    def raw(self) -> dict[str, Any]:
        snapshot = self._repo.snapshot()
        text = snapshot.raw.decode("utf-8", errors="replace") if snapshot.exists else (
            "version: 1\n\njobs: []\n"
        )
        return {
            "text": text,
            "revision": snapshot.revision,
            "exists": snapshot.exists,
            "writable": snapshot.writable,
            "problem": snapshot.structural_error,
        }

    async def validate_raw(self, text: str) -> list[Issue]:
        """What the ingester would say about this text: its own answer, else the mirror's."""
        try:
            result = await self._client.validate(text)
        except IngestError:
            result = None
        if result is not None:
            return [
                Issue(
                    item.get("job_id") if isinstance(item.get("job_id"), str) else None,
                    str(item.get("field")),
                    str(item.get("message")),
                )
                for item in result.get("errors") or []
                if isinstance(item, dict)
            ]
        try:
            document = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            first = str(exc).strip().splitlines()[0] if str(exc).strip() else "invalid YAML"
            return [Issue(None, "jobs_file", f"invalid YAML: {first}")]
        if document is None:
            document = {}
        if not isinstance(document, dict):
            return [Issue(None, "jobs_file", "top level must be a mapping")]
        return jobspec.check_catalog(document, self._rules())

    async def replace_raw(
        self, text: str, *, revision: str, user: str
    ) -> tuple[LoadOutcome | None, list[Issue]]:
        """Replace jobs.yaml with the text as typed (comments kept), if it would load."""
        if len(text.encode("utf-8")) > 1_000_000:
            raise InvalidRequest("The file is larger than 1 MB.")
        issues = await self.validate_raw(text)
        if issues:
            raise InvalidJob(issues)
        self._repo.replace_raw(text.encode("utf-8"), expected_revision=revision)
        self._audit(user, "rag.ingest.catalog.raw", "jobs.yaml", bytes=len(text.encode("utf-8")))
        try:
            info = await self._client.reload()
        except IngestError:
            return None, []
        errors = [e for e in info.get("errors") or [] if isinstance(e, dict)]
        return LoadOutcome(
            not errors and bool(info.get("applied", True)),
            tuple(f"{e.get('job_id') or 'jobs.yaml'}: {e.get('field')}: {e.get('message')}"
                  for e in errors),
        ), []

    def defaults(self) -> dict[str, Any]:
        document = self._repo.snapshot().document
        return jobspec.known_defaults(self._defaults(document))

    async def set_defaults(self, sections: Mapping[str, Any], *, user: str) -> LoadOutcome | None:
        """Replace the catalog's `defaults:`; every job has to stay valid with them."""
        clean = {
            key: value
            for key, value in jobspec.known_defaults(sections).items()
            if isinstance(value, Mapping) and value
        }
        unknown = set(sections) - set(jobspec.DEFAULT_SECTIONS)
        if unknown:
            raise InvalidJob(
                [Issue(None, "defaults", "unsupported sections: " + ", ".join(sorted(unknown)))]
            )
        document = copy.deepcopy(self._repo.snapshot().document)
        if clean:
            document["defaults"] = clean
        else:
            document.pop("defaults", None)
        issues = jobspec.check_catalog(document, self._rules())
        if issues:
            raise InvalidJob(issues)
        self._repo.set_defaults(clean)
        self._audit(user, "rag.ingest.defaults.update", "defaults", sections=sorted(clean))
        try:
            info = await self._client.reload()
        except IngestError:
            return None
        errors = [e for e in info.get("errors") or [] if isinstance(e, dict)]
        return LoadOutcome(not errors, tuple(str(e.get("message")) for e in errors))

    # ── credentials ─────────────────────────────────────────────────────────

    def credential_names(self) -> list[dict[str, Any]]:
        """The credentials the editor may offer: name and where it comes from."""
        env = environment_credentials(self._config_dir)
        store = secrets_store.SecretsRepository(
            self._config_dir, rag_secrets(self._config_dir).connections_secret
        ).snapshot()
        names = {name: "environment" for name in env}
        for name in store.names:
            names.setdefault(name, "store")
        return [{"name": name, "origin": origin} for name, origin in sorted(names.items())]

    async def credentials(self) -> CredentialsView:
        secrets = rag_secrets(self._config_dir)
        repo = secrets_store.SecretsRepository(self._config_dir, secrets.connections_secret)
        snapshot = repo.snapshot()
        readable = repo.readable(snapshot)
        env = environment_credentials(self._config_dir)
        used = secrets_store.references(self._repo.snapshot().document)
        items: list[Credential] = []
        for name in sorted(env | set(snapshot.names)):
            from_env = name in env
            items.append(
                Credential(
                    name=name,
                    origin="environment" if from_env else "store",
                    readable=True if from_env else readable.get(name, False),
                    used_by=tuple(used.get(name, ())),
                    shadowed=from_env and name in snapshot.names,
                )
            )
        state, _ = await self.ingester()
        supported: bool | None = (FEATURE_SECRETS in state.features) if state.reachable else None
        return CredentialsView(
            items=tuple(items),
            supported=supported,
            has_key=repo.has_key,
            writable=snapshot.writable,
            file_problem=snapshot.structural_error,
            ingester=state,
        )

    async def set_credential(self, name: str, value: str, *, user: str) -> str:
        """Store a credential encrypted. Returns the full name."""
        try:
            full = secrets_store.full_name(name)
        except secrets_store.SecretProblem as exc:
            raise InvalidRequest(str(exc)) from exc
        if full in environment_credentials(self._config_dir):
            raise Conflict(
                f"{full} is already defined in ai/rag/.env. The ingester answers from the "
                "environment first, so a stored value would never be used. Change it there, "
                "or choose another name."
            )
        state, _ = await self.ingester()
        if state.reachable and FEATURE_SECRETS not in state.features:
            raise IngestTooOld(
                "This ingester does not read stored credentials. Use an ingester release "
                "with the secret store, or put the credential into ai/rag/.env."
            )
        repo = secrets_store.SecretsRepository(
            self._config_dir, rag_secrets(self._config_dir).connections_secret
        )
        try:
            created = repo.set(full, value)
        except secrets_store.SecretProblem as exc:
            raise InvalidRequest(str(exc)) from exc
        self._audit(user, "rag.ingest.secret.set", full, created=created)
        if state.usable:
            with contextlib.suppress(IngestError):
                await self._client.reload()  # the jobs that were waiting for it
        return full

    async def delete_credential(self, name: str, *, user: str) -> None:
        try:
            full = secrets_store.full_name(name)
        except secrets_store.SecretProblem as exc:
            raise InvalidRequest(str(exc)) from exc
        if full in environment_credentials(self._config_dir):
            raise Conflict(
                f"{full} comes from ai/rag/.env and cannot be deleted here. Remove it from "
                "that file."
            )
        used = secrets_store.references(self._repo.snapshot().document).get(full, [])
        if used:
            raise Conflict(
                f"{full} is used by: {', '.join(used)}. Change those jobs first.",
                {"used_by": used},
            )
        repo = secrets_store.SecretsRepository(
            self._config_dir, rag_secrets(self._config_dir).connections_secret
        )
        if not repo.delete(full):
            raise NotFound(full)
        self._audit(user, "rag.ingest.secret.delete", full)
