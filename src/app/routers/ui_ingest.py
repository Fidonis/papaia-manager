"""HTML pages — the ingester's jobs, runs, credentials, leftovers and catalog file.

Server-rendered like the other RAG pages: a page is a thin shell that loads its body from a
partial, and a partial polls itself only while something is working (a run), by rendering its
own `hx-trigger` only then, so the polling stops by itself when the run ends. Everything that
changes something goes through the JSON API (`api_ingest_jobs`), not through these routes.

Lives apart from `ui.py` so that file does not grow by another thousand lines; it borrows
`_ctx` and `_templates` from there.
"""
from __future__ import annotations

from typing import Annotated, Any

import yaml
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.core.ingest import catalog, job_forms, jobspec
from app.core.ingest.errors import CatalogRejected, IngestError, NotFound
from app.core.ingest.jobs_service import (
    FEATURE_DOCUMENTS,
    FEATURE_PROGRESS,
    FEATURE_SECRETS,
    FEATURE_VALIDATE,
    JobRow,
    JobsService,
)
from app.core.schedule import COMMON_TIMEZONES, WEEKDAY_LABELS
from app.routers.rag_deps import JobsServiceDep, RagAdmin
from app.routers.ui import _ctx, _no_store
from app.templating import templates as _templates

router = APIRouter()

# The statuses a run can have in the ingester, in the words of the filter.
RUN_STATUS_CHOICES: tuple[tuple[str, str], ...] = (
    ("running", "Running"),
    ("success", "Finished"),
    ("failed", "Failed"),
    ("interrupted", "Stopped"),
    ("aborted_guard", "Stopped by a safety check"),
    ("aborted_lock", "Gave up waiting for the collection"),
)
FILE_STATUS_TEXT: dict[str, str] = {
    "indexed": "Embedded",
    "skipped_no_text": "No text found",
    "skipped_too_large": "Too large",
    "skipped_unsupported": "Not a supported type",
    "failed_extract": "Could not be read",
    "failed_embed": "Could not be embedded",
}
FILE_STATUS_HELP: dict[str, str] = {
    "skipped_no_text": "No text could be read: a scan, an image or an empty file.",
    "skipped_too_large": "Larger than the size limit, so it was left out.",
    "skipped_unsupported": "The ingester does not read this type of file.",
}
FILES_PAGE = 50


def _section(document: dict[str, Any], name: str) -> dict[str, Any]:
    value = document.get(name)
    return value if isinstance(value, dict) else {}


def _dump(data: Any) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)


def _page(request: Request, user: Any, template: str, **extra: Any) -> HTMLResponse:
    return _templates.TemplateResponse(request, template, _ctx(request, user, **extra))


def _partial(request: Request, user: Any, template: str, **extra: Any) -> HTMLResponse:
    return _no_store(_templates.TemplateResponse(request, template, _ctx(request, user, **extra)))


def _run_dialog_payload(row: JobRow, rows: list[JobRow]) -> dict[str, Any]:
    """What the run dialog needs to know about a job, handed over by the row that opens it."""
    return {
        "id": row.id,
        "collection": row.collection,
        "mode": row.mode,
        "mode_label": row.mode_label,
        "full_scope": row.full_scope,
        "remote": row.source_type not in ("", "local"),
        "first_run": row.last_run is None,
        "siblings": sorted(
            other.id
            for other in rows
            if other.id != row.id and other.collection == row.collection and other.enabled
        ),
    }


def _group(row: JobRow) -> str:
    """The filter chip a row belongs to: needs attention, active, or disabled."""
    last = row.last_run
    if row.state != "loaded" or (last is not None and not last.clean and not last.active):
        return "attention"
    return "active" if row.enabled else "disabled"


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@router.get("/ingest/jobs", response_class=HTMLResponse)
async def jobs_page(request: Request, user: RagAdmin, service: JobsServiceDep) -> HTMLResponse:
    return _page(request, user, "ingest_jobs.html", tz=service.timezone())


@router.get("/ingest/new", response_class=HTMLResponse)
async def new_job_page(
    request: Request,
    user: RagAdmin,
    service: JobsServiceDep,
    duplicate: Annotated[str, Query(max_length=64)] = "",
) -> HTMLResponse:
    """A blank editor, or a copy of a job (its id and source label cleared)."""
    return _editor(request, user, service, None, duplicate)


@router.get("/ingest/jobs/{job_id}/edit", response_class=HTMLResponse, response_model=None)
async def edit_job_page(
    job_id: str, request: Request, user: RagAdmin, service: JobsServiceDep
) -> Response:
    if catalog.is_managed(job_id):
        # Their next run overwrites a change; the page for a job shows what they are.
        return RedirectResponse(f"/ingest/jobs/{job_id}", status_code=303)
    return _editor(request, user, service, job_id, "")


def _editor(
    request: Request, user: Any, service: JobsService, job_id: str | None, duplicate: str
) -> HTMLResponse:
    problem = ""
    editor: dict[str, Any] = {}
    try:
        editor = service.editor(job_id or (duplicate or None))
        if job_id is None and duplicate:
            editor["state"]["id"] = ""
            editor["state"]["source"]["label"] = ""
            editor["etag"] = ""
    except NotFound:
        problem = f"The job {job_id or duplicate!r} is not in jobs.yaml."
    except CatalogRejected as exc:
        problem = str(exc)
    boot = {
        "state": editor.get("state", {}),
        "etag": editor.get("etag", ""),
        "isNew": job_id is None,
        "originalId": job_id or "",
        "timezone": editor.get("timezone", service.timezone()),
        "defaults": editor.get("defaults", {}),
        "inherited": editor.get("inherited", {}),
        "sources": jobspec.source_forms(),
        "credentials": service.credential_names(),
        "connections": service.connections(),
        "modes": list(job_forms.MODE_CHOICES),
        "strategies": job_forms.STRATEGY_TEXT,
        "startups": job_forms.STARTUP_TEXT,
        "presets": list(job_forms.FILTER_PRESETS),
        "timezones": list(COMMON_TIMEZONES),
        "weekdays": [{"key": key, "label": label} for key, label in WEEKDAY_LABELS.items()],
    }
    return _page(
        request,
        user,
        "ingest_job_edit.html",
        job_id=job_id,
        problem=problem,
        boot=boot,
        copy_of=duplicate,
        tz=service.timezone(),
    )


@router.get("/ingest/jobs/{job_id}", response_class=HTMLResponse)
async def job_page(
    job_id: str, request: Request, user: RagAdmin, service: JobsServiceDep
) -> HTMLResponse:
    try:
        row = await service.job_row(job_id)
    except NotFound:
        return _page(
            request, user, "ingest_job.html", job_id=job_id, row=None, tz=service.timezone()
        )
    except IngestError:
        row = None
    view = await service.overview()
    return _page(
        request,
        user,
        "ingest_job.html",
        job_id=job_id,
        row=row,
        view=view,
        run_dialog=_run_dialog_payload(row, list(view.jobs)) if row else None,
        tz=service.timezone(),
    )


@router.get("/ingest/runs", response_class=HTMLResponse)
async def runs_page(
    request: Request,
    user: RagAdmin,
    service: JobsServiceDep,
    job: Annotated[str, Query(max_length=64)] = "",
    status: Annotated[str, Query(max_length=40)] = "",
) -> HTMLResponse:
    view = await service.overview()
    return _page(
        request,
        user,
        "ingest_runs.html",
        job_choices=[row.id for row in view.jobs],
        status_choices=RUN_STATUS_CHOICES,
        selected_job=job,
        selected_status=status,
        tz=service.timezone(),
    )


@router.get("/ingest/runs/{run_id}", response_class=HTMLResponse)
async def run_page(
    run_id: str, request: Request, user: RagAdmin, service: JobsServiceDep
) -> HTMLResponse:
    return _page(request, user, "ingest_run.html", run_id=run_id, tz=service.timezone())


@router.get("/ingest/secrets", response_class=HTMLResponse)
async def secrets_page(request: Request, user: RagAdmin, service: JobsServiceDep) -> HTMLResponse:
    return _page(request, user, "ingest_secrets.html", tz=service.timezone())


@router.get("/ingest/orphans", response_class=HTMLResponse)
async def orphans_page(request: Request, user: RagAdmin, service: JobsServiceDep) -> HTMLResponse:
    return _page(request, user, "ingest_orphans.html", tz=service.timezone())


@router.get("/ingest/catalog", response_class=HTMLResponse)
async def catalog_page(request: Request, user: RagAdmin, service: JobsServiceDep) -> HTMLResponse:
    return _page(
        request,
        user,
        "ingest_catalog.html",
        raw=service.raw(),
        defaults=service.defaults(),
        chunk_strategies=list(jobspec.CHUNK_STRATEGIES),
        timezones=list(COMMON_TIMEZONES),
        tz=service.timezone(),
    )


# ---------------------------------------------------------------------------
# Partials
# ---------------------------------------------------------------------------


@router.get("/partials/ingest/jobs", response_class=HTMLResponse)
async def partial_jobs(
    request: Request, user: RagAdmin, service: JobsServiceDep
) -> HTMLResponse:
    view = await service.overview()
    rows = list(view.jobs)
    groups = {row.id: _group(row) for row in rows}
    return _partial(
        request,
        user,
        "partials/ingest_job_list.html",
        view=view,
        rows=rows,
        groups=groups,
        counts={
            name: list(groups.values()).count(name)
            for name in ("attention", "active", "disabled")
        },
        dialogs={row.id: _run_dialog_payload(row, rows) for row in rows},
        active=any(row.active_run for row in rows),
        tz=service.timezone(),
    )


@router.get("/partials/ingest/jobs/{job_id}/{tab}", response_class=HTMLResponse)
async def partial_job_tab(
    job_id: str,
    tab: str,
    request: Request,
    user: RagAdmin,
    service: JobsServiceDep,
    status: Annotated[str, Query(max_length=40)] = "",
    q: Annotated[str, Query(max_length=200)] = "",
    run: Annotated[str, Query(max_length=64)] = "",
    offset: Annotated[int, Query(ge=0)] = 0,
    rows: Annotated[bool, Query()] = False,
) -> HTMLResponse:
    """One tab of a job: overview, runs, files (what happened to each) and configuration."""
    tz = service.timezone()
    try:
        row = await service.job_row(job_id)
    except NotFound:
        return _partial(request, user, "partials/ingest_job_gone.html", job_id=job_id)
    if tab == "overview":
        try:
            raw, defaults, _ = service.authored_job(job_id)
        except NotFound:
            raw, defaults = {}, {}  # known to the ingester only
        merged = jobspec.effective(raw, defaults)
        target = _section(merged, "target")
        filters = _section(merged, "filters")
        embedding = _section(merged, "embedding")
        return _partial(
            request,
            user,
            "partials/ingest_job_overview.html",
            row=row,
            model=embedding.get("model") or "",
            acl_tags=list(target.get("acl_tags") or []),
            include=list(filters.get("include") or []),
            exclude=list(filters.get("exclude") or []),
            mode_text=next(
                (c["text"] for c in job_forms.MODE_CHOICES if c["value"] == row.mode), ""
            ),
            active=row.active_run is not None,
            tz=tz,
        )
    if tab == "runs":
        runs = await service.runs(job_id=job_id, limit=20)
        return _partial(
            request,
            user,
            "partials/ingest_run_rows.html",
            runs=runs,
            show_job=False,
            active=any(view.active for view in runs),
            poll_url=f"/partials/ingest/jobs/{job_id}/runs",
            tz=tz,
        )
    if tab == "files":
        state, _ = await service.ingester()
        if not state.has(FEATURE_DOCUMENTS):
            return _partial(
                request, user, "partials/ingest_job_files.html", supported=False, row=row, tz=tz
            )
        result = await service.documents(
            job_id,
            status=status or None,
            q=q or None,
            run_id=run or None,
            limit=FILES_PAGE,
            offset=offset,
        )
        return _partial(
            request,
            user,
            "partials/ingest_job_files.html",
            supported=True,
            row=row,
            result=result or {"total": 0, "counts": {}, "items": []},
            status_text=FILE_STATUS_TEXT,
            status_help=FILE_STATUS_HELP,
            rows_only=rows,
            selected_status=status,
            query=q,
            selected_run=run,
            offset=offset,
            page=FILES_PAGE,
            tz=tz,
        )
    if tab == "config":
        try:
            raw, defaults, _ = service.authored_job(job_id)
        except NotFound:
            return _partial(request, user, "partials/ingest_job_gone.html", job_id=job_id)
        inherited = jobspec.known_defaults(defaults)
        return _partial(
            request,
            user,
            "partials/ingest_job_config.html",
            row=row,
            yaml_text=_dump(raw),
            defaults=inherited,
            defaults_text=_dump(inherited),
            tz=tz,
        )
    return _partial(request, user, "partials/ingest_job_gone.html", job_id=job_id)


@router.get("/partials/ingest/preview/{job_id}", response_class=HTMLResponse)
async def partial_preview(
    job_id: str, request: Request, user: RagAdmin, service: JobsServiceDep
) -> HTMLResponse:
    """What a run would pick up now, for the Files tab."""
    problem = ""
    files: list[dict[str, Any]] = []
    try:
        files = await service.preview_files(job_id, 100)
    except IngestError as exc:
        problem = str(exc)
    return _partial(
        request, user, "partials/ingest_preview.html", files=files, problem=problem, job_id=job_id
    )


@router.get("/partials/ingest/runs", response_class=HTMLResponse)
async def partial_runs(
    request: Request,
    user: RagAdmin,
    service: JobsServiceDep,
    job: Annotated[str, Query(max_length=64)] = "",
    status: Annotated[str, Query(max_length=40)] = "",
) -> HTMLResponse:
    problem = ""
    runs: list[Any] = []
    try:
        runs = await service.runs(job_id=job or None, status=status or None, limit=60)
    except IngestError as exc:
        problem = str(exc)
    query = "&".join(
        part
        for part in (f"job={job}" if job else "", f"status={status}" if status else "")
        if part
    )
    return _partial(
        request,
        user,
        "partials/ingest_run_rows.html",
        runs=runs,
        show_job=True,
        problem=problem,
        active=any(view.active for view in runs),
        poll_url="/partials/ingest/runs" + (f"?{query}" if query else ""),
        tz=service.timezone(),
    )


@router.get("/partials/ingest/runs/{run_id}", response_class=HTMLResponse)
async def partial_run(
    run_id: str, request: Request, user: RagAdmin, service: JobsServiceDep
) -> HTMLResponse:
    state, _ = await service.ingester()
    problem = ""
    view = None
    try:
        view = await service.run(run_id)
    except NotFound:
        problem = "The ingester does not know this run any more. It keeps the latest 200 per job."
    except IngestError as exc:
        problem = str(exc)
    return _partial(
        request,
        user,
        "partials/ingest_run_detail.html",
        view=view,
        problem=problem,
        progress_supported=state.has(FEATURE_PROGRESS) if state.usable else None,
        tz=service.timezone(),
    )


@router.get("/partials/ingest/secrets", response_class=HTMLResponse)
async def partial_secrets(
    request: Request, user: RagAdmin, service: JobsServiceDep
) -> HTMLResponse:
    return _partial(
        request,
        user,
        "partials/ingest_secret_list.html",
        view=await service.credentials(),
        secrets_feature=FEATURE_SECRETS,
        tz=service.timezone(),
    )


@router.get("/partials/ingest/orphans", response_class=HTMLResponse)
async def partial_orphans(
    request: Request, user: RagAdmin, service: JobsServiceDep
) -> HTMLResponse:
    problem = ""
    orphans: list[dict[str, Any]] = []
    try:
        orphans = await service.orphans()
    except IngestError as exc:
        problem = str(exc)
    return _partial(
        request,
        user,
        "partials/ingest_orphan_list.html",
        orphans=orphans,
        problem=problem,
        validate_feature=FEATURE_VALIDATE,
    )
