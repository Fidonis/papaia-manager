"""REST API -- who is told about what (Mail, Notifications).

Administrator-only and CSRF-checked like every other mutating route. Every change carries the
`revision` of the configuration it was made on and is refused with a 409 when that file has
changed since: two administrators, or one with two tabs, cannot silently undo each other.

A recipient's address or webhook URL is write-only after it is saved. A webhook URL
authorises whoever posts to it, so the API shows where it goes (`https://host/…`) and nothing
more, and a request that leaves `target` out keeps the stored one. The body is parsed by hand
for the same reason as the mail routes: a Pydantic error for a missing field echoes the body.
"""

from __future__ import annotations

import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ValidationError

from app.auth.csrf import verify_csrf
from app.auth.deps import AdminUser
from app.auth.oidc import OIDCClaims
from app.config import Settings, get_settings
from app.core.audit import write_audit_entry
from app.core.mail.settings import load_mail_settings, supported
from app.core.notifications import store
from app.core.notifications.model import (
    CATALOGUE,
    Event,
    EventSettings,
    Kind,
    NotificationsError,
    Recipient,
)
from app.core.notifications.service import send_test

router = APIRouter(prefix="/api/v1/notifications")

# A test notification per recipient every so often: a button held down would fill a mailbox
# or a chat channel.
TEST_INTERVAL_SECONDS = 5.0
_last_test: dict[str, float] = {}


class RecipientBody(BaseModel):
    revision: str
    name: str
    kind: Kind
    target: str
    enabled: bool = True
    events: list[Event] = []


class RecipientChange(BaseModel):
    revision: str
    name: str | None = None
    # Left out (or empty): keep the stored address or URL.
    target: str | None = None
    enabled: bool | None = None
    events: list[Event] | None = None


class EventsBody(BaseModel):
    revision: str
    events: list[Event]


class SettingsBody(BaseModel):
    revision: str
    check_interval_minutes: int
    debounce_minutes: int
    cert_lead_days: int


class RevisionBody(BaseModel):
    revision: str


def _user_id(user: OIDCClaims) -> str:
    return user.preferred_username or user.sub


async def _body[T: BaseModel](request: Request, model: type[T]) -> T:
    """The request body as `model`, with a refusal that never repeats a value."""
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="The body is not valid JSON.") from exc
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        raise HTTPException(status_code=422, detail=problems or "The body is not valid.") from exc


def _stale() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="The notification settings changed since this page was loaded. "
        "Reload and try again.",
    )


def _refused(exc: NotificationsError) -> HTTPException:
    return HTTPException(status_code=422, detail=str(exc))


def _unreadable(exc: store.ConfigUnreadableError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=f"{exc} Repair or remove manager/notifications.yaml first.",
    )


def _gone() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail="That recipient does not exist (any more)."
    )


def recipient_json(recipient: Recipient, state: store.NotifyState) -> dict[str, Any]:
    """A recipient as the page shows it: where it goes is shown, the URL never is."""
    tested = state.tests.get(recipient.id)
    return {
        "id": recipient.id,
        "name": recipient.name,
        "kind": recipient.kind.value,
        "target": recipient.display_target,
        "enabled": recipient.enabled,
        "events": [event.value for event in recipient.events],
        "test": (
            {"state": "passed" if tested.ok else "failed", "at": tested.at, "reason": tested.reason}
            if tested is not None
            else {"state": "untested", "at": "", "reason": ""}
        ),
    }


def document(settings: Settings) -> dict[str, Any]:
    """What the page shows: the configuration without its secrets, and what email can do."""
    config_dir = settings.papaia_config_dir
    load = store.load_config(config_dir)
    state = store.load_state(config_dir)
    mail = load_mail_settings(config_dir)
    is_supported = supported(settings.papaia_workspace_dir)
    return {
        "revision": load.revision,
        "error": load.error,
        "settings": load.config.settings.model_dump(),
        "events": [
            {
                "id": info.event.value,
                "label": info.label,
                "short": info.short,
                "description": info.description,
                "severity": info.severity.value,
            }
            for info in CATALOGUE
        ],
        "recipients": [recipient_json(r, state) for r in load.config.recipients],
        "mail": {
            "supported": is_supported,
            "ready": is_supported and mail.profile_on and mail.configured,
        },
    }


def _audit(
    settings: Settings, user: OIDCClaims, action: str, target: str, params: dict[str, Any]
) -> None:
    write_audit_entry(
        settings.papaia_config_dir,
        user=_user_id(user),
        action=action,
        target=target,
        params=params,
    )


def _require_mail_support(kind: Kind, settings: Settings) -> None:
    if kind is Kind.EMAIL and not supported(settings.papaia_workspace_dir):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This papAIa core has no mail relay (it comes with papAIa 1.6.0), "
            "so there is no way to send email. A webhook works.",
        )


@router.get("")
async def get_notifications(
    user: AdminUser, settings: Annotated[Settings, Depends(get_settings)]
) -> dict[str, Any]:
    return document(settings)


@router.post("/recipients", status_code=status.HTTP_201_CREATED)
async def create_recipient(
    request: Request, user: AdminUser, settings: Annotated[Settings, Depends(get_settings)]
) -> dict[str, Any]:
    verify_csrf(request)
    body = await _body(request, RecipientBody)
    _require_mail_support(body.kind, settings)
    try:
        loaded, recipient = store.add_recipient(
            settings.papaia_config_dir,
            body.revision,
            name=body.name,
            kind=body.kind,
            target=body.target,
            enabled=body.enabled,
            events=body.events,
        )
    except store.StaleRevisionError as exc:
        raise _stale() from exc
    except store.ConfigUnreadableError as exc:
        raise _unreadable(exc) from exc
    except NotificationsError as exc:
        raise _refused(exc) from exc
    _audit(
        settings,
        user,
        "notifications.recipient.create",
        recipient.name,
        {
            "kind": recipient.kind.value,
            "enabled": recipient.enabled,
            "events": [e.value for e in recipient.events],
        },
    )
    return {
        "revision": loaded.revision,
        "recipient": recipient_json(recipient, store.load_state(settings.papaia_config_dir)),
    }


@router.put("/recipients/{recipient_id}")
async def update_recipient(
    recipient_id: str,
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    verify_csrf(request)
    body = await _body(request, RecipientChange)
    try:
        loaded, recipient = store.update_recipient(
            settings.papaia_config_dir,
            body.revision,
            recipient_id,
            name=body.name,
            target=body.target,
            enabled=body.enabled,
            events=body.events,
        )
    except store.StaleRevisionError as exc:
        raise _stale() from exc
    except store.ConfigUnreadableError as exc:
        raise _unreadable(exc) from exc
    except LookupError as exc:
        raise _gone() from exc
    except NotificationsError as exc:
        raise _refused(exc) from exc
    _audit(
        settings,
        user,
        "notifications.recipient.update",
        recipient.name,
        {
            "enabled": recipient.enabled,
            "events": [e.value for e in recipient.events],
            "target_changed": bool(body.target),
        },
    )
    return {
        "revision": loaded.revision,
        "recipient": recipient_json(recipient, store.load_state(settings.papaia_config_dir)),
    }


@router.put("/recipients/{recipient_id}/events")
async def set_events(
    recipient_id: str,
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """The matrix: replace what one recipient receives."""
    verify_csrf(request)
    body = await _body(request, EventsBody)
    try:
        loaded, recipient = store.update_recipient(
            settings.papaia_config_dir, body.revision, recipient_id, events=body.events
        )
    except store.StaleRevisionError as exc:
        raise _stale() from exc
    except store.ConfigUnreadableError as exc:
        raise _unreadable(exc) from exc
    except LookupError as exc:
        raise _gone() from exc
    except NotificationsError as exc:
        raise _refused(exc) from exc
    _audit(
        settings,
        user,
        "notifications.recipient.update",
        recipient.name,
        {"events": [e.value for e in recipient.events]},
    )
    return {"revision": loaded.revision, "events": [e.value for e in recipient.events]}


@router.delete("/recipients/{recipient_id}")
async def delete_recipient(
    recipient_id: str,
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    verify_csrf(request)
    body = await _body(request, RevisionBody)
    try:
        loaded, name = store.delete_recipient(
            settings.papaia_config_dir, body.revision, recipient_id
        )
    except store.StaleRevisionError as exc:
        raise _stale() from exc
    except store.ConfigUnreadableError as exc:
        raise _unreadable(exc) from exc
    except LookupError as exc:
        raise _gone() from exc
    _audit(settings, user, "notifications.recipient.delete", name, {})
    return {"revision": loaded.revision}


@router.put("/settings")
async def put_settings(
    request: Request, user: AdminUser, settings: Annotated[Settings, Depends(get_settings)]
) -> dict[str, Any]:
    verify_csrf(request)
    body = await _body(request, SettingsBody)
    try:
        values = EventSettings(
            check_interval_minutes=body.check_interval_minutes,
            debounce_minutes=body.debounce_minutes,
            cert_lead_days=body.cert_lead_days,
        )
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise HTTPException(status_code=422, detail=problems) from exc
    try:
        loaded = store.save_settings(settings.papaia_config_dir, body.revision, values)
    except store.StaleRevisionError as exc:
        raise _stale() from exc
    except store.ConfigUnreadableError as exc:
        raise _unreadable(exc) from exc
    _audit(settings, user, "notifications.settings", "notifications", values.model_dump())
    return {"revision": loaded.revision, "settings": values.model_dump()}


@router.post("/recipients/{recipient_id}/test")
async def test_recipient(
    recipient_id: str,
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Send a test notification to one recipient. Delivered or refused is the answer, not an
    error of this route; the result is remembered and shown next to the recipient."""
    verify_csrf(request)
    waited = time.monotonic() - _last_test.get(recipient_id, 0.0)
    if waited < TEST_INTERVAL_SECONDS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="A test was sent a moment ago. Wait a few seconds.",
        )
    _last_test[recipient_id] = time.monotonic()
    outcome = await send_test(settings, recipient_id, actor=_user_id(user))
    if outcome is None:
        raise _gone()
    return {"ok": outcome.ok, "reason": outcome.reason}
