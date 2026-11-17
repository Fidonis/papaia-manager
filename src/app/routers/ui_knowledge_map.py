"""The knowledge-map page: what the map holds, how it is built, how well it routes.

Rendered once and then driven by the JSON API (`api_knowledge_map`). The page exists while
the core runs the `knowledge-map` profile; otherwise it says how to switch it on.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.auth.deps import AdminUser
from app.config import Settings, get_settings
from app.core import knowledge_map as km
from app.routers.ui import _ctx, _no_store
from app.templating import templates as _templates

router = APIRouter()


@router.get("/knowledge-map", response_class=HTMLResponse)
async def knowledge_map_page(
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    return _no_store(
        _templates.TemplateResponse(
            request,
            "knowledge_map.html",
            _ctx(request, user, km_active=km.active(settings.papaia_config_dir)),
        )
    )
