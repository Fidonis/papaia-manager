"""The Mail page: the mail relay's settings, its status and the job that applies a change.

The page is a form that is rendered once, from what the core stored, and two polled
strips next to it: the relay's status and the job in flight. The form is never inside a
polled element, because a swap would throw away what is being typed (and a password
with it).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.auth.deps import AdminUser
from app.config import Settings, get_settings
from app.core.jobs import Job, JobQueue, JobStatus
from app.core.mail import service as mail_service
from app.core.mail import status as mail_status
from app.core.mail.settings import (
    DEFAULT_PORTS,
    SECURITY_MODES,
    load_mail_settings,
    settings_json,
    supported,
)
from app.routers.ui import _ctx, _no_store
from app.templating import templates as _templates

router = APIRouter()

# How long a finished mail job keeps its strip. Like the backup page's: no acknowledge
# button, the outcome clears itself.
_OUTCOME_GRACE = timedelta(minutes=5)

_MAIL_ACTIONS = (mail_service.APPLY_ACTION, mail_service.DISABLE_ACTION)


def _queue() -> JobQueue | None:
    from app.main import _job_queue  # noqa: PLC0415

    return _job_queue


def _job_ctx(queue: JobQueue | None) -> dict[str, Any]:
    """The job strip: any job in flight (it queues this page's change behind itself), else
    a mail job that finished a moment ago. One helper for the page and its partial, so the
    state rendered on load and the state polled later are produced by the same code."""
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
                    if j.action in _MAIL_ACTIONS and j.finished_at and j.finished_at > cutoff
                ),
                None,
            )
    # The tail of a running job, and of a failed one: the sentence that says why is the
    # last line, and the page it would otherwise take a click to find.
    show_tail = job is not None and (active or job.status == JobStatus.FAILED)
    return {
        "job": job,
        "job_active": active,
        "job_is_mail": job is not None and job.action in _MAIL_ACTIONS,
        "job_log_tail": (
            "\n".join(queue.read_log(job.id).splitlines()[-8:])
            if queue is not None and job is not None and show_tail
            else ""
        ),
    }


@router.get("/mail", response_class=HTMLResponse)
async def mail_page(
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    """The mail relay's settings, or why there are none (a core without the relay)."""
    current = load_mail_settings(settings.papaia_config_dir)
    return _no_store(
        _templates.TemplateResponse(
            request,
            "mail.html",
            _ctx(
                request,
                user,
                mail_supported=supported(settings.papaia_workspace_dir),
                mail=settings_json(current),
                security_modes=SECURITY_MODES,
                default_ports=DEFAULT_PORTS,
            ),
        )
    )


@router.get("/partials/mail/status", response_class=HTMLResponse)
async def partial_mail_status(
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
    fresh: bool = False,
) -> HTMLResponse:
    """The relay's state and queue. `fresh` is the "Re-check" button; the loader still folds a
    repeat within a few seconds into the run that just finished."""
    current = load_mail_settings(settings.papaia_config_dir)
    is_supported = supported(settings.papaia_workspace_dir)
    reading = None
    if is_supported and current.profile_on:
        reading = await mail_status.load_status(
            config_dir=settings.papaia_config_dir,
            workspace_dir=settings.papaia_workspace_dir,
            force=fresh,
        )
    return _no_store(
        _templates.TemplateResponse(
            request,
            "partials/mail_status.html",
            _ctx(
                request,
                user,
                mail_supported=is_supported,
                mail=settings_json(current),
                relay=reading,
            ),
        )
    )


@router.get("/partials/mail/job-status", response_class=HTMLResponse)
async def partial_mail_job_status(request: Request, user: AdminUser) -> HTMLResponse:
    return _no_store(
        _templates.TemplateResponse(
            request, "partials/mail_job_status.html", _ctx(request, user, **_job_ctx(_queue()))
        )
    )
