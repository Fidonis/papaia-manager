"""The Notifications tab of the Mail page: who is told about what.

The recipients and their matrix are rendered once, from what is stored, and never inside a
polled element: a poll that replaced them would throw away a dialog being filled in. Two small
strips next to them are polled -- whether notifications can go out at all, and the log of what
was sent.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.auth.deps import AdminUser
from app.config import Settings, get_settings
from app.core.audit import AuditFilter, query_entries
from app.core.notifications.model import INFO_BY_EVENT, Event
from app.core.notifications.service import NotificationService
from app.routers.api_notifications import document
from app.routers.ui import _ctx, _no_store
from app.templating import templates as _templates

if TYPE_CHECKING:
    from app.core.audit import AuditEntry

router = APIRouter()

LOG_ROWS = 15


def _service() -> NotificationService | None:
    from app.main import _notification_service  # noqa: PLC0415

    return _notification_service


def _event_label(value: object) -> str:
    try:
        return INFO_BY_EVENT[Event(str(value))].label
    except ValueError:
        return "Test" if value == "test" else str(value)


def _log_rows(entries: list[AuditEntry]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for entry in entries:
        params = entry.params or {}
        rows.append(
            {
                "ts": entry.ts,
                "event": _event_label(params.get("event", "")),
                "subject": str(params.get("subject", "")),
                "recipient": entry.target,
                "ok": entry.action == "notify.sent",
                "reason": str(params.get("reason", "")),
            }
        )
    return rows


def _strip(doc: dict[str, Any], service: NotificationService | None) -> dict[str, Any]:
    """The state of the strip above the recipients, as a plain dict for the partial."""
    subscribed = sum(1 for r in doc["recipients"] if r["enabled"] and r["events"])
    last_pass: datetime | None = service.last_pass_at if service is not None else None
    return {
        "error": doc["error"],
        "mail_supported": doc["mail"]["supported"],
        "mail_ready": doc["mail"]["ready"],
        "subscribed": subscribed,
        "interval": doc["settings"]["check_interval_minutes"],
        "last_pass": last_pass.isoformat() if last_pass is not None else "",
        "running": service is not None,
    }


@router.get("/mail", response_class=HTMLResponse)
async def notifications_page(
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    """The recipients and what each of them receives."""
    doc = document(settings)
    return _no_store(
        _templates.TemplateResponse(
            request,
            "notifications.html",
            _ctx(request, user, doc=doc, strip=_strip(doc, _service())),
        )
    )


@router.get("/partials/notifications/status", response_class=HTMLResponse)
async def partial_notifications_status(
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    doc = document(settings)
    return _no_store(
        _templates.TemplateResponse(
            request,
            "partials/notifications_status.html",
            _ctx(request, user, strip=_strip(doc, _service())),
        )
    )


@router.get("/partials/notifications/log", response_class=HTMLResponse)
async def partial_notifications_log(
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    """What was sent lately: deliveries only (`notify.*`), not the changes an administrator
    made to the recipients, which the audit log shows."""
    page = await asyncio.to_thread(
        query_entries,
        settings.papaia_config_dir,
        AuditFilter(action="notify."),
        limit=LOG_ROWS,
        offset=0,
    )
    return _no_store(
        _templates.TemplateResponse(
            request,
            "partials/notifications_log.html",
            _ctx(request, user, rows=_log_rows(page.entries), total=page.total),
        )
    )
