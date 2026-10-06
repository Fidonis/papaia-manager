"""REST API — managing the ingester's jobs, runs, credentials and catalog.

For administrators of a deployment that runs the RAG system, like the other RAG APIs, with the
CSRF check first in every handler that changes something. Five groups:

* **jobs** (`/jobs`): the list, create, edit, enable, disable, delete, run, and what a job has
  embedded (`/files`, `/preview`, `/runs`);
* **runs** (`/job-runs`): read one, abort it. (`/runs` belongs to the Embedding page, whose
  runs are found by collection.)
* **leftovers** (`/orphans`): what the ingester has state for that no job of the file explains;
* **credentials** (`/secrets`): names only, values are written and never read back;
* **catalog** (`/catalog`): the raw text of `jobs.yaml` and its `defaults:`.

A refused job answers 422 with `detail.issues`, one entry per field in the ingester's own
dotted form (`source.bucket`), which is what the editor puts next to the input.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field

from app.auth.csrf import verify_csrf
from app.auth.oidc import OIDCClaims
from app.core.ingest.jobs_service import (
    CatalogView,
    CredentialsView,
    IngesterState,
    JobRow,
    RunOptions,
    SaveResult,
)
from app.core.ingest.jobspec import (
    CHUNK_STRATEGIES,
    MODES,
    SOURCE_TYPES,
    Issue,
    source_forms,
)
from app.routers.rag_deps import JobsServiceDep, RagAdmin
from app.routers.rag_deps import translated as _translated

router = APIRouter(prefix="/api/v1/rag/ingest")


def _user_id(user: OIDCClaims) -> str:
    return user.preferred_username or user.sub


# ---------------------------------------------------------------------------
# What goes in
# ---------------------------------------------------------------------------


class JobBody(BaseModel):
    # The editor's state of the job (see `job_forms.form_state`).
    job: dict[str, Any]
    # The version of the entry the editor opened, so an edit made meanwhile is not overwritten.
    etag: str | None = Field(default=None, max_length=64)
    # The id the job had when it was opened; an edit may not change it.
    original_id: str | None = Field(default=None, max_length=64)


class ValidateBody(BaseModel):
    job: dict[str, Any]
    create: bool = False
    original_id: str | None = Field(default=None, max_length=64)


class RunBody(BaseModel):
    mode: Literal["append", "upsert", "full"] | None = None
    full_scope: Literal["job", "collection"] | None = None
    dry_run: bool = False
    skip_sync: bool = False
    force: bool = False
    delete_vanished: bool = True
    confirm_rebuild: bool = False
    confirm_collection: bool = False


class SchedulePreviewBody(BaseModel):
    schedule: dict[str, Any]


class SecretBody(BaseModel):
    value: str = Field(max_length=70_000)


class RawBody(BaseModel):
    text: str = Field(max_length=1_100_000)
    revision: str = Field(max_length=64)


class ValidateRawBody(BaseModel):
    text: str = Field(max_length=1_100_000)


class DefaultsBody(BaseModel):
    defaults: dict[str, Any]


# ---------------------------------------------------------------------------
# What comes out
# ---------------------------------------------------------------------------


def state_payload(state: IngesterState) -> dict[str, Any]:
    return {
        "reachable": state.reachable,
        "usable": state.usable,
        "reason": state.reason,
        "status": state.status,
        "version": state.version,
        "features": sorted(state.features),
        "deps": dict(state.deps),
        "config_error": state.config_error,
        "config_valid": state.config_valid,
        "config_applied": state.config_applied,
        "loaded_at": state.loaded_at,
        "config_path": state.config_path,
        "refused": state.refused,
    }


def row_payload(row: JobRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "state": row.state,
        "state_text": row.state_text,
        "note": row.note,
        "problems": list(row.problems),
        "enabled": row.enabled,
        "managed": row.managed,
        "in_file": row.in_file,
        "description": row.description,
        "source_type": row.source_type,
        "source_title": row.source_title,
        "source_label": row.source_label,
        "source_detail": row.source_detail,
        "connection": row.connection,
        "collection": row.collection,
        "mode": row.mode,
        "mode_label": row.mode_label,
        "schedule": row.schedule,
        "next_run_at": row.next_run_at,
        "paused_in_ingester": row.paused_in_ingester,
        "last_run": row.last_run.as_dict() if row.last_run else None,
        "active_run": row.active_run.as_dict() if row.active_run else None,
        "documents": row.documents,
        "chunks": row.chunks,
        "etag": row.etag,
        "runnable": row.runnable,
    }


def view_payload(view: CatalogView) -> dict[str, Any]:
    return {
        "ingester": state_payload(view.ingester),
        "jobs": [row_payload(row) for row in view.jobs],
        "file_problem": view.file_problem,
        "exists": view.exists,
        "writable": view.writable,
        "editable": view.editable,
        "revision": view.revision,
        "problems_elsewhere": list(view.problems_elsewhere),
        "legacy": view.legacy,
        "leftovers": view.leftovers,
        "connections": list(view.connections),
    }


def save_payload(result: SaveResult) -> dict[str, Any]:
    return {
        "job_id": result.job_id,
        "created": result.created,
        "applied": result.applied,
        "verified": result.verified,
        "elsewhere": list(result.elsewhere),
        "note": result.note,
    }


def credentials_payload(view: CredentialsView) -> dict[str, Any]:
    return {
        "items": [
            {
                "name": item.name,
                "origin": item.origin,
                "readable": item.readable,
                "used_by": list(item.used_by),
                "shadowed": item.shadowed,
                "deletable": item.deletable,
            }
            for item in view.items
        ],
        "supported": view.supported,
        "has_key": view.has_key,
        "writable": view.writable,
        "file_problem": view.file_problem,
        "ingester": state_payload(view.ingester),
    }


def issues_payload(issues: list[Issue]) -> list[dict[str, Any]]:
    return [issue.as_dict() for issue in issues]


# ---------------------------------------------------------------------------
# The ingester and the list
# ---------------------------------------------------------------------------


@router.get("/ingester")
async def ingester_state(user: RagAdmin, service: JobsServiceDep) -> dict[str, Any]:
    """Whether the ingester can be used and what it offers. Never an error for a down ingester."""
    state, _ = await service.ingester()
    return state_payload(state)


@router.get("/form-spec")
async def form_spec(user: RagAdmin) -> dict[str, Any]:
    """What the editor renders: source types and their fields, the choices of a few selects."""
    return {
        "source_types": list(SOURCE_TYPES),
        "sources": source_forms(),
        "modes": list(MODES),
        "chunk_strategies": list(CHUNK_STRATEGIES),
    }


@router.get("/jobs")
async def list_jobs(user: RagAdmin, service: JobsServiceDep) -> dict[str, Any]:
    with _translated():
        return view_payload(await service.overview())


@router.post("/reload", status_code=status.HTTP_200_OK)
async def reload_catalog(
    request: Request, user: RagAdmin, service: JobsServiceDep
) -> dict[str, Any]:
    """Make the ingester read `jobs.yaml` now, instead of at its next poll."""
    verify_csrf(request)
    with _translated():
        return state_payload(await service.reload())


@router.post("/schedule/preview")
async def schedule_preview(
    request: Request, body: SchedulePreviewBody, user: RagAdmin, service: JobsServiceDep
) -> dict[str, Any]:
    """What a schedule from the builder compiles to and when it would run next.

    A POST because the builder's state is a body, but it changes nothing; it is still
    answered only to a signed-in administrator who sent the CSRF token.
    """
    verify_csrf(request)
    preview = service.schedule_preview(body.schedule)
    return {
        "ok": preview.ok,
        "error": preview.error,
        "description": preview.description,
        "cron": preview.cron,
        "every": preview.every,
        "runs": list(preview.runs),
        "timezone": preview.timezone,
        "counts_from_load": preview.counts_from_load,
        "notes": list(preview.notes),
    }


@router.post("/jobs/validate")
async def validate_job(
    request: Request, body: ValidateBody, user: RagAdmin, service: JobsServiceDep
) -> dict[str, Any]:
    """The problems with an edit, field by field. Writes nothing."""
    verify_csrf(request)
    with _translated():
        issues = await service.validate(
            body.job, create=body.create, original_id=body.original_id
        )
    return {"ok": not issues, "issues": issues_payload(issues)}


@router.post("/jobs", status_code=status.HTTP_201_CREATED)
async def create_job(
    request: Request, body: JobBody, user: RagAdmin, service: JobsServiceDep
) -> dict[str, Any]:
    verify_csrf(request)
    with _translated(str(body.job.get("id") or "")):
        result = await service.save(
            body.job, create=True, etag=None, user=_user_id(user)
        )
    return save_payload(result)


@router.get("/jobs/{job_id}/editor")
async def job_editor(job_id: str, user: RagAdmin, service: JobsServiceDep) -> dict[str, Any]:
    """The state the editor opens a job with. `new` opens a blank one."""
    with _translated(job_id):
        return service.editor(None if job_id == "new" else job_id)


@router.put("/jobs/{job_id}")
async def update_job(
    job_id: str, request: Request, body: JobBody, user: RagAdmin, service: JobsServiceDep
) -> dict[str, Any]:
    verify_csrf(request)
    with _translated(job_id):
        result = await service.save(
            body.job,
            create=False,
            etag=body.etag,
            user=_user_id(user),
            original_id=body.original_id or job_id,
        )
    return save_payload(result)


@router.delete("/jobs/{job_id}")
async def delete_job(
    job_id: str,
    request: Request,
    user: RagAdmin,
    service: JobsServiceDep,
    etag: Annotated[str | None, Query(max_length=64)] = None,
    purge: bool = False,
) -> dict[str, Any]:
    verify_csrf(request)
    with _translated(job_id):
        result = await service.delete(job_id, etag=etag, purge=purge, user=_user_id(user))
    return {
        "job_id": result.job_id,
        "purged": result.purged,
        "deleted_points": result.deleted_points,
        "deleted_rows": result.deleted_rows,
        "note": result.note,
    }


@router.post("/jobs/{job_id}/enable")
async def enable_job(
    job_id: str, request: Request, user: RagAdmin, service: JobsServiceDep
) -> dict[str, Any]:
    verify_csrf(request)
    with _translated(job_id):
        return save_payload(await service.set_enabled(job_id, True, user=_user_id(user)))


@router.post("/jobs/{job_id}/disable")
async def disable_job(
    job_id: str, request: Request, user: RagAdmin, service: JobsServiceDep
) -> dict[str, Any]:
    verify_csrf(request)
    with _translated(job_id):
        return save_payload(await service.set_enabled(job_id, False, user=_user_id(user)))


@router.post("/jobs/{job_id}/run", status_code=status.HTTP_202_ACCEPTED)
async def run_job(
    job_id: str, request: Request, body: RunBody, user: RagAdmin, service: JobsServiceDep
) -> dict[str, Any]:
    verify_csrf(request)
    with _translated(job_id):
        run_id = await service.run_now(
            job_id,
            RunOptions(
                mode=body.mode,
                full_scope=body.full_scope,
                dry_run=body.dry_run,
                skip_sync=body.skip_sync,
                force=body.force,
                delete_vanished=body.delete_vanished,
                confirm_rebuild=body.confirm_rebuild,
                confirm_collection=body.confirm_collection,
            ),
            user=_user_id(user),
        )
    return {"run_id": run_id}


@router.get("/jobs/{job_id}/files")
async def job_files(
    job_id: str,
    user: RagAdmin,
    service: JobsServiceDep,
    status_filter: Annotated[str | None, Query(alias="status", max_length=40)] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    run_id: Annotated[str | None, Query(max_length=64)] = None,
    order: Annotated[Literal["path", "recent"], Query()] = "path",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """What happened to each file of a job (needs an ingester that reports it)."""
    with _translated(job_id):
        result = await service.documents(
            job_id,
            status=status_filter,
            q=q,
            run_id=run_id,
            order=order,
            limit=limit,
            offset=offset,
        )
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="This ingester does not report the files of a job. Use a newer release.",
        )
    return result


@router.get("/jobs/{job_id}/preview")
async def job_preview(
    job_id: str,
    user: RagAdmin,
    service: JobsServiceDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> dict[str, Any]:
    """The files a run would pick up now. For a remote source only what was fetched before."""
    with _translated(job_id):
        files = await service.preview_files(job_id, limit)
    return {"files": files, "count": len(files)}


@router.get("/jobs/{job_id}/runs")
async def runs_of_job(
    job_id: str,
    user: RagAdmin,
    service: JobsServiceDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 10,
) -> dict[str, Any]:
    with _translated(job_id):
        views = await service.runs(job_id=job_id, limit=limit)
    return {"runs": [view.as_dict() for view in views]}


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


@router.get("/job-runs")
async def list_job_runs(
    user: RagAdmin,
    service: JobsServiceDep,
    job_id: Annotated[str | None, Query(max_length=64)] = None,
    status_filter: Annotated[str | None, Query(alias="status", max_length=40)] = None,
    since: Annotated[str | None, Query(max_length=40)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    with _translated():
        views = await service.runs(job_id=job_id, status=status_filter, since=since, limit=limit)
    return {"runs": [view.as_dict() for view in views]}


@router.get("/job-runs/{run_id}")
async def get_job_run(run_id: str, user: RagAdmin, service: JobsServiceDep) -> dict[str, Any]:
    with _translated(run_id):
        view = await service.run(run_id)
    return view.as_dict()


@router.delete("/job-runs/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
async def abort_job_run(
    run_id: str, request: Request, user: RagAdmin, service: JobsServiceDep
) -> Response:
    verify_csrf(request)
    with _translated(run_id):
        await service.abort(run_id, user=_user_id(user))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Leftovers
# ---------------------------------------------------------------------------


@router.get("/orphans")
async def list_orphans(user: RagAdmin, service: JobsServiceDep) -> dict[str, Any]:
    with _translated():
        return {"orphans": await service.orphans()}


@router.delete("/orphans/{job_id}")
async def delete_orphan(
    job_id: str, request: Request, user: RagAdmin, service: JobsServiceDep
) -> dict[str, Any]:
    verify_csrf(request)
    with _translated(job_id):
        return await service.delete_orphan(job_id, user=_user_id(user))


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


@router.get("/secrets")
async def list_secrets(user: RagAdmin, service: JobsServiceDep) -> dict[str, Any]:
    with _translated():
        return credentials_payload(await service.credentials())


@router.put("/secrets/{name}")
async def set_secret(
    name: str, request: Request, body: SecretBody, user: RagAdmin, service: JobsServiceDep
) -> dict[str, Any]:
    verify_csrf(request)
    with _translated(name):
        stored = await service.set_credential(name, body.value, user=_user_id(user))
    return {"name": stored}


@router.delete("/secrets/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_secret(
    name: str, request: Request, user: RagAdmin, service: JobsServiceDep
) -> Response:
    verify_csrf(request)
    with _translated(name):
        await service.delete_credential(name, user=_user_id(user))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# The catalog file
# ---------------------------------------------------------------------------


@router.get("/catalog/raw")
async def get_raw(user: RagAdmin, service: JobsServiceDep) -> dict[str, Any]:
    return service.raw()


@router.post("/catalog/validate")
async def validate_raw(
    request: Request, body: ValidateRawBody, user: RagAdmin, service: JobsServiceDep
) -> dict[str, Any]:
    verify_csrf(request)
    with _translated():
        issues = await service.validate_raw(body.text)
    return {"ok": not issues, "issues": issues_payload(issues)}


@router.put("/catalog/raw")
async def put_raw(
    request: Request, body: RawBody, user: RagAdmin, service: JobsServiceDep
) -> dict[str, Any]:
    verify_csrf(request)
    with _translated("jobs.yaml"):
        outcome, _ = await service.replace_raw(
            body.text, revision=body.revision, user=_user_id(user)
        )
    return {
        "verified": outcome is not None,
        "applied": bool(outcome and outcome.applied),
        "elsewhere": list(outcome.elsewhere) if outcome else [],
        "raw": service.raw(),
    }


@router.get("/catalog/defaults")
async def get_defaults(user: RagAdmin, service: JobsServiceDep) -> dict[str, Any]:
    return {"defaults": service.defaults()}


@router.put("/catalog/defaults")
async def put_defaults(
    request: Request, body: DefaultsBody, user: RagAdmin, service: JobsServiceDep
) -> dict[str, Any]:
    verify_csrf(request)
    with _translated("defaults"):
        outcome = await service.set_defaults(body.defaults, user=_user_id(user))
    return {
        "verified": outcome is not None,
        "applied": bool(outcome and outcome.applied),
        "elsewhere": list(outcome.elsewhere) if outcome else [],
        "defaults": service.defaults(),
    }
