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
from app.auth.oidc import OIDCClaims
from app.config import Settings, get_settings
from app.core.audit import redact_params, write_audit_entry
from app.core.keycloak_users import KeycloakRejectedError
from app.core.users_service import MAX_PAGE_SIZE, PAGE_SIZE, PartiallyCreated
from app.routers.users_deps import UsersAdmin, UsersServiceDep, translated

router = APIRouter(prefix="/api/v1/users")

SettingsDep = Annotated[Settings, Depends(get_settings)]

# What Keycloak's ids look like: a user's is a UUID, a session's is a 24-character base64url
# string. Anything else never reaches a URL.
UserId = Annotated[str, Path(pattern=r"^[0-9A-Fa-f-]{8,64}$")]
SessionId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{8,128}$")]


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


def _user_id(user: OIDCClaims) -> str:
    return user.preferred_username or user.sub


def _audit(
    settings: Settings,
    user: OIDCClaims,
    action: str,
    target: str,
    params: dict[str, Any] | None = None,
    result: str = "ok",
) -> None:
    write_audit_entry(
        settings.papaia_config_dir,
        user=_user_id(user),
        action=action,
        target=target,
        params=redact_params(params) if params else None,
        result=result,
    )


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
    with translated("that session"):
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
