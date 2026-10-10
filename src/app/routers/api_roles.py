"""REST API -- the realm's roles: list, create, change and delete.

Like the accounts, they are managed with the signed-in account's own Keycloak rights (see
`users_deps`), and the identity role is what may use it. Every mutating route is CSRF-checked
and writes an audit entry with the role as its target. Creating, changing and deleting a role
needs Keycloak's `manage-realm`, which `papaia-admin` carries; where it does not, the answer
says so.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from pydantic import BaseModel, Field

from app.auth.csrf import verify_csrf
from app.config import Settings, get_settings
from app.core.roles_service import RolePartiallyCreated
from app.routers.users_deps import RolesServiceDep, UsersAdmin, audit, translated

router = APIRouter(prefix="/api/v1/roles")

SettingsDep = Annotated[Settings, Depends(get_settings)]
RoleName = Annotated[str, Path(min_length=1, max_length=255)]

_WRITE_HINT = (
    "Keycloak refuses this for your account: creating, changing and deleting roles needs the "
    "realm-management role manage-realm. The role {role} has to carry it. Add it in the Keycloak "
    "Admin Console under Realm roles > {role} > Associated roles (filter by clients), or use a "
    "papAIa release whose realm gives {role} the right: `papaia-ctl start` then applies it to an "
    "existing installation. Sign in again afterwards."
)


class CreateBody(BaseModel):
    name: str = Field(max_length=255)
    description: str = Field(default="", max_length=2000)
    members: list[str] = Field(default_factory=list, max_length=500)


class UpdateBody(BaseModel):
    description: str = Field(default="", max_length=2000)
    members: list[str] = Field(default_factory=list, max_length=500)


def _hint(settings: Settings) -> str:
    return _WRITE_HINT.format(role=settings.manager_identity_admin_role)


@router.get("")
async def list_roles(user: UsersAdmin, service: RolesServiceDep) -> dict[str, Any]:
    with translated():
        view = await service.snapshot()
    if view.state == "forbidden":
        raise HTTPException(status_code=403, detail=view.reason)
    if view.state == "unavailable":
        raise HTTPException(status_code=503, detail=view.reason)
    return view.as_dict()


@router.get("/{name}")
async def get_role(name: RoleName, user: UsersAdmin, service: RolesServiceDep) -> dict[str, Any]:
    """One role with the accounts that hold it: what the delete dialog tells the person."""
    with translated(f"role {name}"):
        detail = await service.detail(name)
    return detail.as_dict()


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_role(
    request: Request,
    body: CreateBody,
    user: UsersAdmin,
    service: RolesServiceDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    verify_csrf(request)
    with translated(forbidden=_hint(settings)):
        try:
            row = await service.create(
                name=body.name, description=body.description, members=body.members
            )
        except RolePartiallyCreated as partial:
            # The role exists, so the change is recorded, as the part-way it is.
            audit(settings, user, "role.create", partial.name, {"members": body.members}, "partial")
            raise HTTPException(status_code=502, detail=partial.message) from partial
    audit(
        settings,
        user,
        "role.create",
        row.name,
        {"described": bool(row.description), "members": row.members},
    )
    return row.as_dict()


@router.put("/{name}")
async def update_role(
    name: RoleName,
    request: Request,
    body: UpdateBody,
    user: UsersAdmin,
    service: RolesServiceDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    verify_csrf(request)
    with translated(f"role {name}", forbidden=_hint(settings)):
        change = await service.update(name, description=body.description, members=body.members)
    if change.changed:
        audit(
            settings,
            user,
            "role.update",
            name,
            {
                "description_changed": change.description_changed,
                "added": change.added,
                "removed": change.removed,
            },
        )
    return {
        "name": name,
        "description_changed": change.description_changed,
        "added": change.added,
        "removed": change.removed,
    }


@router.delete("/{name}")
async def delete_role(
    name: RoleName,
    request: Request,
    user: UsersAdmin,
    service: RolesServiceDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    verify_csrf(request)
    with translated(f"role {name}", forbidden=_hint(settings)):
        users, more = await service.delete(name)
    audit(settings, user, "role.delete", name, {"accounts": users, "more": more})
    return {"name": name, "accounts": users, "more": more}
