"""The Nodes page, listed under System on a distributed installation only.

On a single host the sidebar has no entry and every route here answers 404 once the role
check has passed, so the page does not exist there, and nothing starts a process to find
that out: `nodes.is_distributed` reads `deployment.yaml`.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse

from app.auth.deps import AdminUser, stepped_up
from app.config import Settings, get_settings
from app.core import ctl, nodes
from app.routers.ui import _ctx, _no_store
from app.templating import templates as _templates

router = APIRouter()


def require_distributed(settings: Settings) -> None:
    if not nodes.is_distributed(settings.papaia_config_dir):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)


@router.get("/nodes", response_class=HTMLResponse)
async def nodes_page(
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    require_distributed(settings)
    return _no_store(
        _templates.TemplateResponse(
            request, "nodes.html", _ctx(request, user, stepped_up=stepped_up(request))
        )
    )


@router.get("/partials/nodes", response_class=HTMLResponse)
async def nodes_partial(
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
    fresh: Annotated[bool, Query()] = False,
) -> HTMLResponse:
    """The list. A node that does not answer is a row; a core that cannot say is a message."""
    require_distributed(settings)
    error = ""
    rows: list[nodes.NodeState] = []
    try:
        rows = await nodes.overview(
            workspace_dir=settings.papaia_workspace_dir,
            config_dir=settings.papaia_config_dir,
            fresh=fresh,
        )
    except (nodes.NodeError, ctl.CtlError) as exc:
        error = str(exc)
    return _no_store(
        _templates.TemplateResponse(
            request, "partials/nodes_list.html", _ctx(request, user, rows=rows, error=error)
        )
    )


@router.get("/nodes/{name}", response_class=HTMLResponse)
async def node_detail(
    request: Request,
    name: str,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    """One node: its answer, its links, and what enrolling it still takes."""
    require_distributed(settings)
    config_dir = settings.papaia_config_dir
    if name not in nodes.registry(config_dir):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    error = ""
    node: nodes.NodeState | None = None
    keys: list[nodes.HostKey] = []
    try:
        rows = await nodes.overview(
            workspace_dir=settings.papaia_workspace_dir, config_dir=config_dir
        )
        node = next((row for row in rows if row.name == name), None)
        if node is not None and not node.controller and not node.host_key_pinned:
            keys = await nodes.host_keys(
                name, workspace_dir=settings.papaia_workspace_dir, config_dir=config_dir
            )
    except (nodes.NodeError, ctl.CtlError) as exc:
        error = str(exc)
    return _no_store(
        _templates.TemplateResponse(
            request,
            "node_detail.html",
            _ctx(
                request,
                user,
                name=name,
                node=node,
                host_keys=keys,
                error=error,
                join_commands=nodes.join_commands(config_dir, node) if node else "",
                stepped_up=stepped_up(request),
            ),
        )
    )
