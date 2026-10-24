"""REST API -- the mail relay (core profile `smtp`).

Administrator-only and CSRF-checked like every other mutating route. A change is a queued
job (the core's `setup`, then `start --profiles=smtp` while the relay is on), so it is
single-flight with everything else the manager does to the stack and its log can be
followed; testing the connection and sending a test mail are answered at once.

The relay is switched on and off in Settings > Modules, not here: this API stores the mail
server's settings, whether the relay is on or off, and applies them when it is on.

The password is write-only. It is read from the body by hand, not through a Pydantic
model: a validation error of a model echoes the offending input, and for a missing field
that is the whole body. Nothing here returns it, logs it or puts it in an audit entry
(`login_change` says only whether it was set, kept or removed).
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
from app.core.config.modules import core_manages_modules
from app.core.jobs import JobQueue
from app.core.mail import probe as mail_probe
from app.core.mail import service as mail_service
from app.core.mail import status as mail_status
from app.core.mail.settings import (
    DEFAULT_PORTS,
    MailError,
    MailSettings,
    load_mail_settings,
    read_password,
    settings_json,
    stored_secret_applies,
    supported,
    validate_host,
    validate_input,
    validate_password,
    validate_port,
    validate_security,
    validate_user,
)
from app.i18n import gettext as _
from app.routers.stack_guard import require_idle_stack

router = APIRouter(prefix="/api/v1/mail")

# One test mail every so often per process: the relay accepts anything its sender
# domain allows, and a button held down would fill a mailbox.
TEST_MESSAGE_INTERVAL_SECONDS = 10.0
_last_test_message = 0.0


class MailBody(BaseModel):
    revision: str
    host: str
    port: int | None = None
    security: str = "starttls"
    user: str | None = None
    from_address: str
    from_name: str | None = None
    # Declared so the schema documents it; read from the raw body, see `_body`.
    password: str | None = None


class ProbeBody(BaseModel):
    host: str | None = None
    port: int | None = None
    security: str | None = None
    user: str | None = None
    password: str | None = None


class TestMessageBody(BaseModel):
    to: str


def _queue() -> JobQueue:
    from app.main import _job_queue  # noqa: PLC0415

    if _job_queue is None:
        raise HTTPException(status_code=503, detail=_("job queue not initialized"))
    return _job_queue


def _user_id(user: OIDCClaims) -> str:
    return user.preferred_username or user.sub


async def _body[T: BaseModel](request: Request, model: type[T]) -> T:
    """The request body as `model`, with a refusal that never repeats a value."""
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=_("The body is not valid JSON.")) from exc
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        raise HTTPException(
            status_code=422, detail=problems or _("The body is not valid.")
        ) from exc


def _require_supported(settings: Settings) -> None:
    if not supported(settings.papaia_workspace_dir):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_("This papAIa core has no mail relay (it comes with papAIa 1.6.0)."),
        )


def _require_current(current: MailSettings, revision: str) -> None:
    if revision != current.revision:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_("The mail settings changed since this page was loaded. Reload and try again."),
        )


def _refused(exc: MailError) -> HTTPException:
    return HTTPException(status_code=422, detail=str(exc))


async def document(settings: Settings, *, fresh: bool = False) -> dict[str, Any]:
    """What the page shows: the stored settings and, while the relay is on, its status."""
    current = load_mail_settings(settings.papaia_config_dir)
    reading = None
    if supported(settings.papaia_workspace_dir) and current.profile_on:
        reading = (
            await mail_status.load_status(
                config_dir=settings.papaia_config_dir,
                workspace_dir=settings.papaia_workspace_dir,
                force=fresh,
            )
        ).as_json()
    return {
        "supported": supported(settings.papaia_workspace_dir),
        "settings": settings_json(current),
        "status": reading,
    }


@router.get("")
async def get_mail(
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
    fresh: bool = False,
) -> dict[str, Any]:
    return await document(settings, fresh=fresh)


@router.put("", status_code=status.HTTP_202_ACCEPTED)
async def put_mail(
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, str]:
    """Store the mail server's settings and, when the relay is on, apply them to it. Answers
    with the job to follow."""
    verify_csrf(request)
    body = await _body(request, MailBody)
    _require_supported(settings)
    current = load_mail_settings(settings.papaia_config_dir)
    _require_current(current, body.revision)
    if not current.profile_on and not core_manages_modules(settings.papaia_workspace_dir):
        # An older core would read the values as "switch it on".
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                _(
                    "This papAIa core can't store the settings with the relay off (it needs "
                    "1.6.0). Switch the relay on with papaia-ctl, or upgrade the core."
                )
            ),
        )
    try:
        data = validate_input(
            host=body.host,
            port=body.port,
            security=body.security,
            user=body.user,
            from_address=body.from_address,
            from_name=body.from_name,
            password=body.password,
            current=current,
        )
    except MailError as exc:
        raise _refused(exc) from exc
    await require_idle_stack()

    username = _user_id(user)
    job = await _queue().enqueue(
        action=mail_service.APPLY_ACTION,
        target=mail_service.TARGET,
        user=username,
        params={"host": data.host, "security": data.security},
        callback=mail_service.apply_callback(
            data=data,
            current=current,
            user=username,
            workspace_dir=settings.papaia_workspace_dir,
            config_dir=settings.papaia_config_dir,
        ),
    )
    return {"job_id": job.id, "status": "queued"}


@router.post("/probe")
async def probe_mail(
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Talk to the mail server with the values in the form (or the stored ones for what is
    left out), without sending a mail and without saving. A refusal by the server is the
    answer, not an error of this route."""
    verify_csrf(request)
    body = await _body(request, ProbeBody)
    current = load_mail_settings(settings.papaia_config_dir)
    try:
        host = validate_host(body.host if body.host is not None else current.host)
        mode = validate_security(body.security if body.security is not None else current.security)
        # Left out, the port is the stored one, or the default of a security that differs
        # from the stored one: 587, 465 and 25 belong to their mode.
        port = validate_port(
            body.port
            if body.port is not None
            else (current.effective_port if mode == current.security else DEFAULT_PORTS[mode])
        )
        login = (
            validate_user(body.user)
            if body.user is not None and body.user.strip()
            else (current.user if body.user is None else "")
        )
        if login and mode == "none":
            raise MailError(_("The login would be sent unencrypted: use starttls or tls."))
        password = _probe_password(
            body.password,
            current,
            config_dir=settings.papaia_config_dir,
            host=host,
            port=port,
            user=login,
        )
    except MailError as exc:
        raise _refused(exc) from exc

    result = await mail_probe.probe(host, port, mode, login, password)
    write_audit_entry(
        settings.papaia_config_dir,
        user=_user_id(user),
        action="mail.probe",
        target=mail_service.TARGET,
        params={"host": host, "port": port, "security": mode, "stage": result.stage},
        result="ok" if result.ok else "failed",
    )
    return result.as_json()


@router.post("/test-message")
async def send_test_message(
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Hand a test mail to the relay. Accepted means queued, not delivered."""
    global _last_test_message  # noqa: PLW0603
    verify_csrf(request)
    body = await _body(request, TestMessageBody)
    _require_supported(settings)
    try:
        recipient = mail_probe.validate_recipient(body.to)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    current = load_mail_settings(settings.papaia_config_dir)
    if not (current.profile_on and current.configured):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_("The mail relay is not switched on and configured."),
        )
    waited = time.monotonic() - _last_test_message
    if waited < TEST_MESSAGE_INTERVAL_SECONDS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=_("A test mail was sent a moment ago. Wait a few seconds."),
        )
    _last_test_message = time.monotonic()

    result = await mail_probe.send(
        to=recipient, from_address=current.from_address, from_name=current.from_name
    )
    write_audit_entry(
        settings.papaia_config_dir,
        user=_user_id(user),
        action="mail.test-message",
        target=mail_service.TARGET,
        result="ok" if result.ok else "failed",
    )
    return result.as_json()


def _probe_password(
    typed: str | None, current: MailSettings, *, config_dir: str, host: str, port: int, user: str
) -> str:
    """The password to try: the typed one, else the stored one for the address it was stored
    with. Anything else is a refusal rather than a quiet attempt without it."""
    if not user:
        if typed:
            raise MailError(_("A password needs a login: enter the login, or clear the password."))
        return ""
    if typed:
        return validate_password(typed)
    if not stored_secret_applies(current, host=host, port=port, user=user):
        raise MailError(
            _("There is no stored password for this mail server and login: enter the password.")
        )
    password = read_password(config_dir)
    if not password:
        raise MailError(_("The stored password cannot be read: enter the password."))
    return password
