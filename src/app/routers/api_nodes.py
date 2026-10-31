"""REST API -- changing the set of nodes of a distributed installation.

Every route here changes what the installation trusts, so each one takes a fresh sign-in
(`SteppedUpAdmin`) on top of the administrator role and the CSRF token, and each one is
audited, refused attempts included.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from app.auth.csrf import verify_csrf
from app.auth.deps import SteppedUpAdmin
from app.auth.oidc import OIDCClaims
from app.config import Settings, get_settings
from app.core import ctl, nodes
from app.core.audit import write_audit_entry
from app.i18n import gettext as _
from app.routers.ui_nodes import require_distributed

router = APIRouter()


class AddNodeBody(BaseModel):
    name: str
    address: str
    ssh_user: str = ""
    ssh_port: int = 0
    root: str = ""


class PinBody(BaseModel):
    fingerprint: str


def _require_name(name: str) -> None:
    if not nodes.valid_name(name):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=_("A node name is lowercase letters, digits and '-', starting with a letter."),
        )


def _audit(
    settings: Settings,
    user: OIDCClaims,
    action: str,
    name: str,
    *,
    result: str = "ok",
    params: dict[str, Any] | None = None,
) -> None:
    write_audit_entry(
        settings.papaia_config_dir,
        user=user.preferred_username,
        action=action,
        target=name,
        params=params,
        result=result,
    )


def _refused(
    settings: Settings, user: OIDCClaims, action: str, name: str, exc: Exception
) -> HTTPException:
    _audit(settings, user, action, name, result="failed", params={"reason": str(exc)})
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.post("/api/v1/nodes", status_code=status.HTTP_201_CREATED)
async def add_node(
    request: Request,
    body: AddNodeBody,
    user: SteppedUpAdmin,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, str]:
    """Register a node and write its join archive; the core validates the rest."""
    verify_csrf(request)
    require_distributed(settings)
    _require_name(body.name)
    new = nodes.NewNode(
        name=body.name,
        address=body.address.strip(),
        ssh_user=body.ssh_user.strip(),
        ssh_port=body.ssh_port,
        root=body.root.strip(),
    )
    try:
        archive = await nodes.add_node(
            new,
            workspace_dir=settings.papaia_workspace_dir,
            config_dir=settings.papaia_config_dir,
        )
    except (nodes.NodeError, ctl.CtlError) as exc:
        raise _refused(settings, user, "node.add", body.name, exc) from exc
    _audit(settings, user, "node.add", body.name, params={"address": new.address})
    return {"name": body.name, "archive": archive}


@router.delete("/api/v1/nodes/{name}")
async def remove_node(
    request: Request,
    name: str,
    user: SteppedUpAdmin,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, str]:
    verify_csrf(request)
    require_distributed(settings)
    _require_name(name)
    try:
        await nodes.remove_node(
            name,
            workspace_dir=settings.papaia_workspace_dir,
            config_dir=settings.papaia_config_dir,
        )
    except (nodes.NodeError, ctl.CtlError) as exc:
        raise _refused(settings, user, "node.remove", name, exc) from exc
    _audit(settings, user, "node.remove", name)
    return {"name": name}


@router.post("/api/v1/nodes/{name}/pin")
async def pin_host_key(
    request: Request,
    name: str,
    body: PinBody,
    user: SteppedUpAdmin,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, str]:
    """Trust the host key an administrator compared with the install script's output."""
    verify_csrf(request)
    require_distributed(settings)
    _require_name(name)
    if not nodes.valid_fingerprint(body.fingerprint):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=_("That is not a SHA256 host key fingerprint."),
        )
    try:
        await nodes.pin_host_key(
            name,
            body.fingerprint,
            workspace_dir=settings.papaia_workspace_dir,
            config_dir=settings.papaia_config_dir,
        )
    except (nodes.NodeError, ctl.CtlError) as exc:
        raise _refused(settings, user, "node.pin", name, exc) from exc
    _audit(settings, user, "node.pin", name, params={"fingerprint": body.fingerprint})
    return {"name": name}
