"""REST API -- the module catalog and the administration overview of delegations.

`GET /api/v1/modules` needs the admin role and only reads (see `app.core.module_catalog`).
The delegation routes need the identity-admin role and act with the signed-in account's own
Keycloak rights (see `users_deps`). The overview carries counts only; revoking is CSRF-checked
and leaves one entry in the audit log and one `delegation_revoked` row of `papaia.audit/1`,
also when Keycloak refuses or finds nothing to revoke.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response, status

from app.auth.csrf import verify_csrf
from app.auth.deps import AdminUser
from app.config import Settings, get_settings
from app.core import delegations
from app.core.audit import write_audit_entry
from app.core.keycloak_users import (
    KeycloakError,
    KeycloakForbiddenError,
    KeycloakNotFoundError,
)
from app.core.module_catalog import module_json, read_catalog
from app.i18n import gettext as _
from app.routers.users_deps import KeycloakDep, UsersAdmin, translated

router = APIRouter(prefix="/api/v1")

SettingsDep = Annotated[Settings, Depends(get_settings)]
UserId = Annotated[str, Path(pattern=r"^[0-9A-Fa-f-]{8,64}$")]


@router.get("/modules")
async def list_modules(user: AdminUser, settings: SettingsDep) -> dict[str, Any]:
    view = read_catalog(settings.papaia_workspace_dir, settings.papaia_config_dir)
    return {
        "bom": {"suite": view.bom.suite, "verified": view.bom.verified, "error": view.bom.error},
        "catalog_error": view.catalog_error,
        "modules": [module_json(m) for m in view.modules],
    }


@router.get("/delegations")
async def delegation_overview(user: UsersAdmin, client: KeycloakDep) -> dict[str, Any]:
    with translated():
        return {"modules": await delegations.overview(client)}


@router.delete(
    "/delegations/{module}/persons/{user_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def revoke_delegations(
    module: str,
    user_id: UserId,
    request: Request,
    user: UsersAdmin,
    client: KeycloakDep,
    settings: SettingsDep,
) -> Response:
    verify_csrf(request)
    holder = delegations.HOLDER_BY_MODULE.get(module)
    if holder is None:
        raise HTTPException(status_code=404, detail=_("This module holds no delegations."))

    def audit(result: str) -> None:
        write_audit_entry(
            settings.papaia_config_dir,
            user=user.preferred_username or user.sub,
            action="delegation.revoke",
            target=f"{holder.module}:{user_id}",
            result=result,
            event="delegation_revoked",
            object_ref={"type": "person", "id": user_id},
            context={"delegation": holder.client_id},
        )

    try:
        await client.revoke_consent(user_id, holder.client_id)
    except KeycloakError as exc:
        if isinstance(exc, KeycloakNotFoundError):
            audit("not_found")
        elif isinstance(exc, KeycloakForbiddenError):
            audit("denied")
        else:
            audit("error")
        with translated(not_found=_("This person has no delegation at this module.")):
            raise
    audit("ok")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
