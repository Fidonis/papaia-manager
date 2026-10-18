"""The Modules and Overlay tabs of Settings, and the job strip they share.

(The first tab, Manager, is `settings_page` in `ui`: the branding and the host monitoring.)

Both pages are rendered once from what is on disk and then driven by the JSON API; neither
polls a form. The job strip is the only polled element: any job in flight (it holds this
page's change behind itself) and, for a few minutes, the outcome of a configuration job.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.auth.deps import AdminUser
from app.config import Settings, get_settings
from app.core.config import modules, overlay, preview, service
from app.core.ctl import LOCALAI_VARIANTS
from app.core.jobs import Job, JobQueue, JobStatus
from app.core.mail.settings import DEFAULT_PORTS, SECURITY_MODES
from app.routers.api_config import target_json
from app.routers.ui import _ctx, _no_store
from app.templating import templates as _templates

router = APIRouter()

# How long a finished configuration job keeps its strip, like the mail and backup pages.
_OUTCOME_GRACE = timedelta(minutes=5)


def _queue() -> JobQueue | None:
    from app.main import _job_queue  # noqa: PLC0415

    return _job_queue


def describe(job: Job) -> tuple[str, str, str]:
    """(while it runs, when it worked, when it failed) for a configuration job."""
    _, _, name = job.target.partition(":")
    if job.action in (service.ENABLE_ACTION, service.DISABLE_ACTION):
        module = modules.SWITCHABLE.get(name)
        title = module.title if module else name
        if job.action == service.ENABLE_ACTION:
            return (
                f"Switching {title} on",
                f"{title} is on",
                f"Switching {title} on failed",
            )
        scope = job.params.get("scope")
        also = {
            modules.SCOPE_DATA: " and deleting its data",
            modules.SCOPE_ALL: " and deleting its data and settings",
        }.get(str(scope), "")
        done = {
            modules.SCOPE_DATA: ", its data is deleted",
            modules.SCOPE_ALL: ", its data and settings are deleted",
        }.get(str(scope), "")
        return (
            f"Switching {title} off{also}",
            f"{title} is off{done}",
            f"Switching {title} off{also} failed",
        )
    label = next((t.label for t in overlay.TARGETS if t.key == name), name)
    if job.action == service.DISCARD_ACTION:
        return (
            f"Discarding the {label} overlay",
            f"The {label} overlay is discarded",
            f"Discarding the {label} overlay failed",
        )
    if job.action == service.ROLLBACK_ACTION:
        return (
            f"Rolling back the {label} overlay",
            f"The {label} overlay is rolled back",
            f"Rolling back the {label} overlay failed",
        )
    return (
        f"Applying the {label} overlay",
        f"The {label} overlay is applied",
        f"Applying the {label} overlay failed",
    )


def _job_ctx(queue: JobQueue | None) -> dict[str, Any]:
    """The job strip: any job in flight, else a configuration job that finished a moment ago.
    One helper for the pages and the partial, so the state rendered on load and the state
    polled later come from the same code."""
    job: Job | None = None
    active = False
    if queue is not None:
        job = queue.active_job()
        active = job is not None
        if job is None:
            cutoff = datetime.now(tz=UTC) - _OUTCOME_GRACE
            job = next(
                (
                    j
                    for j in queue.list_jobs()
                    if j.action in service.CONFIG_ACTIONS
                    and j.finished_at
                    and j.finished_at > cutoff
                ),
                None,
            )
    is_config = job is not None and job.action in service.CONFIG_ACTIONS
    show_tail = job is not None and (active or job.status == JobStatus.FAILED)
    labels = describe(job) if job is not None and is_config else ("", "", "")
    return {
        "job": job,
        "job_active": active,
        "job_is_config": is_config,
        "job_labels": labels,
        "job_log_tail": (
            "\n".join(queue.read_log(job.id).splitlines()[-8:])
            if queue is not None and job is not None and show_tail
            else ""
        ),
    }


@router.get("/settings/modules", response_class=HTMLResponse)
async def modules_page(
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    """The optional modules and a switch for those that can be switched here."""
    return _no_store(
        _templates.TemplateResponse(
            request,
            "settings_modules.html",
            _ctx(
                request,
                user,
                revision=modules.revision(settings.papaia_config_dir),
                module_rows=modules.state(
                    settings.papaia_config_dir, settings.papaia_workspace_dir
                ),
                variants=[{"id": v, "label": modules.VARIANT_LABELS[v]} for v in LOCALAI_VARIANTS],
                host_only_variants=list(modules.HOST_ONLY_VARIANTS),
                stored_variant=modules.stored_variant(settings.papaia_config_dir),
                security_modes=SECURITY_MODES,
                default_ports=DEFAULT_PORTS,
            ),
        )
    )


@router.get("/settings/overlay", response_class=HTMLResponse)
async def overlay_page(
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
    target: str = "",
) -> HTMLResponse:
    """The overlay files, an editor for them and their saved versions."""
    keys = [t.key for t in overlay.TARGETS]
    selected = target if target in keys else keys[0]
    init = {
        "selected": selected,
        "preview_available": preview.available(settings.papaia_workspace_dir),
        "targets": [target_json(settings, t) for t in overlay.TARGETS],
        "other": overlay.other_files(settings.papaia_config_dir),
    }
    return _no_store(
        _templates.TemplateResponse(
            request, "settings_overlay.html", _ctx(request, user, init=init)
        )
    )


@router.get("/partials/config/job-status", response_class=HTMLResponse)
async def partial_config_job_status(request: Request, user: AdminUser) -> HTMLResponse:
    return _no_store(
        _templates.TemplateResponse(
            request, "partials/config_job_status.html", _ctx(request, user, **_job_ctx(_queue()))
        )
    )
