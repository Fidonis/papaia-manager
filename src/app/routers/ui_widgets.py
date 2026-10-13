"""The cards of the dashboard's widgets, one request each.

The dashboard renders a placeholder per widget tile and the browser fetches each card on
its own interval, so a widget that is slow or broken costs that card and nothing else.

Two rules from the tiles still hold here, and this is where they are enforced for a widget:

* A tile a caller may not see is absent, not hidden. The card route answers 404 for a
  widget the caller's role may not see, exactly as for one that does not exist, so the
  route does not tell a plain user which ids are administrators'.
* Nothing is improvised. A tile names a registered type; one that names nothing gets a
  card that says so, and the markup always comes from this manager's templates.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from fastapi.responses import HTMLResponse

from app.auth.deps import AdminUser, AnyUser
from app.auth.oidc import OIDCClaims
from app.auth.roles import is_admin
from app.config import Settings, get_settings
from app.core.tiles import TilesFileError, find_widget, load_tiles
from app.core.widgets import registry, runtime
from app.core.widgets.model import WidgetResult
from app.core.widgets.registry import WidgetContext, WidgetType
from app.core.widgets.view import title_of
from app.routers.ui import _ctx, _no_store, _queue
from app.templating import templates as _templates

router = APIRouter()

_NOT_FOUND = "no such widget"

WidgetId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,40}$")]


async def _read(widget_type: WidgetType, settings: Settings) -> WidgetResult:
    """The widget's reading, or the reason it cannot be taken on this installation."""
    unmet = widget_type.requires(settings)
    if unmet is not None:
        return WidgetResult("unavailable", reason=unmet)
    context = WidgetContext(settings=settings, now=datetime.now(tz=UTC), queue=_queue())
    return await runtime.read(widget_type, context)


def _card(
    request: Request,
    user: OIDCClaims,
    *,
    title: str,
    icon: str,
    result: WidgetResult,
) -> HTMLResponse:
    return _no_store(
        _templates.TemplateResponse(
            request,
            "partials/widgets/card.html",
            _ctx(request, user, title=title, icon=icon, result=result),
        )
    )


@router.get("/partials/widgets/preview/{widget_type_id}", response_class=HTMLResponse)
async def widget_preview(
    widget_type_id: WidgetId,
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
    title: Annotated[str, Query(max_length=80)] = "",
) -> HTMLResponse:
    """A card for a widget type that is not placed yet: the editor's live preview.

    Administrators only, and it reads the same way the dashboard does, so what the editor
    shows is what the dashboard will show.
    """
    widget_type = registry.get(widget_type_id)
    if widget_type is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)
    result = await _read(widget_type, settings)
    return _card(
        request,
        user,
        title=title.strip() or widget_type.name,
        icon=widget_type.icon,
        result=result,
    )


@router.get("/partials/widgets/{widget_id}", response_class=HTMLResponse)
async def widget_card(
    widget_id: WidgetId,
    request: Request,
    user: AnyUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    """One placed widget, as a card."""
    try:
        config = await asyncio.get_running_loop().run_in_executor(
            None, load_tiles, settings.papaia_config_dir
        )
    except TilesFileError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND) from exc

    tile = find_widget(config, widget_id)
    if tile is None or not (is_admin(user, settings) or tile.visibility == "all"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)

    widget_type = registry.get(tile.widget)
    if widget_type is None:
        result = WidgetResult("unavailable", reason=f"No widget called {tile.widget!r} exists.")
        return _card(request, user, title=title_of(tile), icon="alert", result=result)

    return _card(
        request,
        user,
        title=title_of(tile),
        icon=widget_type.icon,
        result=await _read(widget_type, settings),
    )
