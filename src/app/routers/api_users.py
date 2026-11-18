"""REST API -- the accounts of the realm: users, their roles, passwords and sessions.

Every route needs the identity-admin role and acts with the signed-in account's own Keycloak
rights (see `users_deps`). Every mutating route is CSRF-checked and writes an audit entry
with the username as its target. A password is never part of an audit entry or a log line;
a temporary one is returned once, by the route that makes it, and marked `no-store`.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.auth.csrf import verify_csrf
from app.config import Settings, get_settings
from app.core import chat_history
from app.core.ctl import CtlError, clear_chat_history
from app.core.jobs import JobContext, JobQueue, JobStatus
from app.core.keycloak_users import KeycloakRejectedError
from app.core.users_service import MAX_PAGE_SIZE, PAGE_SIZE, PartiallyCreated
from app.i18n import gettext as _
from app.routers.stack_guard import refuse_while_locked
from app.routers.users_deps import UsersAdmin, UsersServiceDep, translated
from app.routers.users_deps import audit as _audit

router = APIRouter(prefix="/api/v1/users")

SettingsDep = Annotated[Settings, Depends(get_settings)]

# What Keycloak's ids look like: a user's is a UUID, a session's is a 24-character base64url
# string. Anything else never reaches a URL.
UserId = Annotated[str, Path(pattern=r"^[0-9A-Fa-f-]{8,64}$")]
SessionId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{8,128}$")]
JobId = Annotated[str, Path(pattern=r"^[0-9a-f-]{36}$")]


class CreateBody(BaseModel):
    username: str = Field(max_length=255)
    email: str = Field(default="", max_length=255)
    first_name: str = Field(default="", max_length=255)
    last_name: str = Field(default="", max_length=255)
    credential: Literal["temporary", "email", "none"] = "temporary"
    roles: list[str] = Field(default_factory=list, max_length=500)


class EnabledBody(BaseModel):
    enabled: bool


class RolesBody(BaseModel):
    roles: list[str] = Field(max_length=500)


class ClearHistoryBody(BaseModel):
    # The deletion cannot be undone: the page shows the dry run's counts first, and a request
    # that skipped that question must not delete anything.
    confirmed: bool = False


def _secret(content: dict[str, Any], status_code: int = status.HTTP_200_OK) -> Response:
    """A JSON answer that carries a password: it must not be stored by anything on the way."""
    return JSONResponse(content, status_code=status_code, headers={"Cache-Control": "no-store"})


@router.get("")
async def list_users(
    user: UsersAdmin,
    service: UsersServiceDep,
    search: Annotated[str, Query(max_length=100)] = "",
    first: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = PAGE_SIZE,
) -> dict[str, Any]:
    with translated():
        view = await service.snapshot(search=search, first=first, limit=limit)
    # The page shows a refusal or a Keycloak that is down as a state; the API says it with
    # the status code the other routes use.
    if view.state == "forbidden":
        raise HTTPException(status_code=403, detail=view.reason)
    if view.state == "unavailable":
        raise HTTPException(status_code=503, detail=view.reason)
    return view.as_dict()


@router.get("/roles")
async def assignable_roles(user: UsersAdmin, service: UsersServiceDep) -> dict[str, Any]:
    """The roles a new account can be offered, before there is an account to ask about."""
    with translated():
        options = await service.assignable_roles()
    return {"roles": [option.as_dict() for option in options]}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_user(
    request: Request,
    body: CreateBody,
    user: UsersAdmin,
    service: UsersServiceDep,
    settings: SettingsDep,
) -> Response:
    verify_csrf(request)
    with translated():
        try:
            created = await service.create(
                username=body.username,
                email=body.email,
                first_name=body.first_name,
                last_name=body.last_name,
                credential=body.credential,
                roles=body.roles,
            )
        except PartiallyCreated as partial:
            # The account exists, so the change is recorded, as the part-way it is.
            _audit(
                settings,
                user,
                "user.create",
                partial.username,
                {"first_login": body.credential, "has_email": bool(body.email.strip())},
                result="partial",
            )
            raise HTTPException(
                status_code=422 if isinstance(partial.cause, KeycloakRejectedError) else 502,
                detail=partial.message,
            ) from partial
    _audit(
        settings,
        user,
        "user.create",
        created.username,
        {
            # Not named "credential": the audit log masks values under keys that sound like
            # secrets, and this is only how the first sign-in is arranged.
            "first_login": body.credential,
            "has_email": bool(body.email.strip()),
            "email_sent": created.email_sent,
        },
    )
    if created.roles:
        _audit(settings, user, "user.role.assign", created.username, {"roles": created.roles})
    return _secret(
        {
            "id": created.id,
            "username": created.username,
            "temporary_password": created.temporary_password,
            "email_sent": created.email_sent,
            "email_error": created.email_error,
            "roles": created.roles,
        },
        status.HTTP_201_CREATED,
    )


@router.put("/{user_id}/enabled")
async def set_enabled(
    user_id: UserId,
    request: Request,
    body: EnabledBody,
    user: UsersAdmin,
    service: UsersServiceDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    verify_csrf(request)
    with translated():
        username, ended = await service.set_enabled(user_id, body.enabled)
    _audit(
        settings,
        user,
        "user.enable" if body.enabled else "user.disable",
        username,
        None if body.enabled else {"sessions_ended": ended},
    )
    return {"username": username, "enabled": body.enabled, "sessions_ended": ended}


@router.get("/{user_id}/roles")
async def get_roles(
    user_id: UserId,
    user: UsersAdmin,
    service: UsersServiceDep,
) -> dict[str, Any]:
    with translated():
        username, options = await service.role_options(user_id)
    return {"username": username, "roles": [option.as_dict() for option in options]}


@router.put("/{user_id}/roles")
async def set_roles(
    user_id: UserId,
    request: Request,
    body: RolesBody,
    user: UsersAdmin,
    service: UsersServiceDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    verify_csrf(request)
    with translated():
        change = await service.set_roles(user_id, body.roles)
    if change.added:
        _audit(settings, user, "user.role.assign", change.username, {"roles": change.added})
    if change.removed:
        _audit(settings, user, "user.role.revoke", change.username, {"roles": change.removed})
    return {"username": change.username, "added": change.added, "removed": change.removed}


@router.post("/{user_id}/password/temporary")
async def temporary_password(
    user_id: UserId,
    request: Request,
    user: UsersAdmin,
    service: UsersServiceDep,
    settings: SettingsDep,
) -> Response:
    verify_csrf(request)
    with translated():
        username, password = await service.temporary_password(user_id)
    _audit(settings, user, "user.password.temporary", username)
    return _secret({"username": username, "temporary_password": password})


@router.post("/{user_id}/password/email")
async def password_email(
    user_id: UserId,
    request: Request,
    user: UsersAdmin,
    service: UsersServiceDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    verify_csrf(request)
    with translated():
        username = await service.password_email(user_id)
    _audit(settings, user, "user.password.reset-email", username)
    return {"username": username, "email_sent": True}


@router.get("/{user_id}/sessions")
async def list_sessions(
    user_id: UserId,
    user: UsersAdmin,
    service: UsersServiceDep,
) -> dict[str, Any]:
    with translated():
        username, rows = await service.sessions(user_id)
    return {
        "username": username,
        "is_self": user_id == user.sub,
        "sessions": [row.as_dict() for row in rows],
    }


@router.delete("/{user_id}/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def end_session(
    user_id: UserId,
    session_id: SessionId,
    request: Request,
    user: UsersAdmin,
    service: UsersServiceDep,
    settings: SettingsDep,
) -> Response:
    verify_csrf(request)
    with translated(_("that session was not found")):
        username = await service.end_session(user_id, session_id)
    _audit(settings, user, "user.session.revoke", username, {"session": session_id[:8]})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/{user_id}/sessions")
async def end_all_sessions(
    user_id: UserId,
    request: Request,
    user: UsersAdmin,
    service: UsersServiceDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    verify_csrf(request)
    with translated():
        username, count = await service.end_all_sessions(user_id)
    _audit(settings, user, "user.session.revoke-all", username, {"sessions": count})
    return {"username": username, "ended": count}


# -- chat history ---------------------------------------------------------------------


def _job_queue() -> JobQueue:
    from app.main import _job_queue as queue  # noqa: PLC0415

    if queue is None:
        raise HTTPException(status_code=503, detail=_("job queue not initialized"))
    return queue


def _require_clear_history(settings: Settings) -> None:
    """A core without `librechat clear-history` (older than 1.9.0) gets a refusal, not a job."""
    if not chat_history.core_supports_clear_history(settings.papaia_workspace_dir):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=chat_history.core_requirement_message()
        )


@router.get("/{user_id}/chat-history")
async def preview_chat_history(
    user_id: UserId,
    user: UsersAdmin,
    service: UsersServiceDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    """What clearing the account's LibreChat history would remove: the core's dry run.

    Nothing is changed. A reason the core gives (no such LibreChat user, LibreChat not
    running) is a 422 with that reason, so the dialog can show it."""
    _require_clear_history(settings)
    with translated():
        username, email = await service.chat_identity(user_id)
    try:
        counts = await chat_history.preview(
            email,
            workspace_dir=settings.papaia_workspace_dir,
            config_dir=settings.papaia_config_dir,
        )
    except chat_history.ChatHistoryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"username": username, "email": email, "counts": counts.as_dict()}


@router.post("/{user_id}/chat-history/clear", status_code=status.HTTP_202_ACCEPTED)
async def clear_history(
    user_id: UserId,
    request: Request,
    user: UsersAdmin,
    service: UsersServiceDep,
    settings: SettingsDep,
    body: ClearHistoryBody | None = None,
) -> dict[str, Any]:
    """Remove the account's LibreChat conversations, messages, files and shared links.

    Needs `confirmed: true` (422 otherwise), because the deletion cannot be undone. Runs as a
    queued job, so it never overlaps a backup that pauses LibreChat's container; the outcome is
    read from `GET .../chat-history/jobs/{job_id}`. Audited as `user.chat-history.clear` with
    the counts, `partial` when some files stayed in storage; a failure is the queue's own
    `error` entry under the same action."""
    verify_csrf(request)
    _require_clear_history(settings)
    if not (body and body.confirmed):
        raise HTTPException(
            status_code=422,
            detail=_("Clearing the chats cannot be undone and has to be confirmed."),
        )
    refuse_while_locked(settings)
    with translated():
        username, email = await service.chat_identity(user_id)
    queue = _job_queue()
    actor = user.preferred_username or user.sub

    async def _callback(ctx: JobContext) -> None:
        ctx.log(f"[ctl] papaia-ctl librechat clear-history {email} -y")
        lines: list[str] = []
        try:
            gen = await clear_chat_history(
                email=email,
                workspace_dir=settings.papaia_workspace_dir,
                config_dir=settings.papaia_config_dir,
            )
            async for line in gen:
                lines.append(line)
                ctx.log(line)
        except CtlError:
            # Exit 1 with counts is the core saying that some files stayed in storage: the
            # rest is gone, so it is recorded as the part-way it is before the job fails.
            counts = chat_history.parse_counts("\n".join(lines))
            if counts is not None:
                _audit(
                    settings,
                    user,
                    chat_history.AUDIT_ACTION,
                    username,
                    _audit_counts(counts),
                    result="partial",
                    job_id=ctx.job.id,
                )
            raise
        counts = chat_history.parse_counts("\n".join(lines)) or chat_history.HistoryCounts()
        _audit(
            settings,
            user,
            chat_history.AUDIT_ACTION,
            username,
            _audit_counts(counts),
            job_id=ctx.job.id,
        )

    job = await queue.enqueue(
        action=chat_history.AUDIT_ACTION,
        target=username,
        user=actor,
        params={"user_id": user_id},
        callback=_callback,
    )
    return {"job_id": job.id, "status": "queued", "username": username}


def _audit_counts(counts: chat_history.HistoryCounts) -> dict[str, int]:
    params = counts.as_dict()
    params.pop("empty", None)
    return params


@router.get("/{user_id}/chat-history/jobs/{job_id}")
async def clear_history_job(
    user_id: UserId,
    job_id: JobId,
    user: UsersAdmin,
) -> dict[str, Any]:
    """How a deletion started from this page is going, and what it removed.

    Read from the job and its log rather than from the job API, which is the admin role's:
    this page is the identity role's. Only a deletion of this account is found."""
    queue = _job_queue()
    job = queue.get_job(job_id)
    if (
        job is None
        or job.action != chat_history.AUDIT_ACTION
        or job.params.get("user_id") != user_id
    ):
        raise HTTPException(status_code=404, detail=_("that job was not found"))
    done = job.status in (JobStatus.SUCCEEDED, JobStatus.FAILED)
    counts = chat_history.parse_counts(queue.read_log(job_id)) if done else None
    error = None
    if job.status == JobStatus.FAILED:
        error = chat_history.core_error(queue.read_log(job_id)) or _(
            "The chats could not be cleared. The job log has the details."
        )
    return {
        "job_id": job.id,
        "username": job.target,
        "status": job.status.value,
        "counts": counts.as_dict() if counts is not None else None,
        "error": error,
    }
