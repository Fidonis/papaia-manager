"""The Models & Usage page: which models LiteLLM serves, and what they consumed.

Read-only. The page is a shell that loads its body (`/partials/models`) from the same query
string it was asked with, so the window, the metric, the tab and the model in detail survive a
reload and can be shared. Everything on it is read from LiteLLM; nothing can be changed from here.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from app.auth.deps import AdminUser
from app.config import Settings, get_settings
from app.core import usage_view
from app.core.litellm import public_ui_url
from app.core.usage_view import Query as ViewQuery
from app.routers.models_deps import LitellmTransport
from app.routers.ui import _ctx, _no_store
from app.templating import templates as _templates

router = APIRouter()

_MAX = 64
_Text = Annotated[str, Query(max_length=_MAX)]


def _query(
    range_: str, from_: str, to: str, metric: str, view: str, model: str
) -> ViewQuery:
    return ViewQuery.parse(
        range_=range_, start=from_, end=to, metric=metric, view=view, model=model
    )


@router.get("/models", response_class=HTMLResponse)
async def models_page(
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
    range: _Text = "",  # noqa: A002 - the name of the query parameter
    from_: Annotated[str, Query(alias="from", max_length=_MAX)] = "",
    to: _Text = "",
    metric: _Text = "",
    view: _Text = "",
    model: _Text = "",
) -> HTMLResponse:
    """The shell. The body is fetched by the browser, so a slow LiteLLM shows a spinner in a
    page that is already there instead of a blank tab."""
    query = _query(range, from_, to, metric, view, model)
    return _no_store(
        _templates.TemplateResponse(
            request,
            "models.html",
            _ctx(
                request,
                user,
                first=query.link(),
                litellm_ui_url=public_ui_url(settings.papaia_config_dir),
            ),
        )
    )


@router.get("/partials/models", response_class=HTMLResponse)
async def partial_models(
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
    transport: LitellmTransport,
    range: _Text = "",  # noqa: A002 - the name of the query parameter
    from_: Annotated[str, Query(alias="from", max_length=_MAX)] = "",
    to: _Text = "",
    metric: _Text = "",
    view: _Text = "",
    model: _Text = "",
    fresh: bool = False,
) -> HTMLResponse:
    """The body: controls, figures, charts and the table, or why there is nothing to show."""
    query = _query(range, from_, to, metric, view, model)
    context = await usage_view.build(settings, query, fresh=fresh, transport=transport)
    return _no_store(
        _templates.TemplateResponse(
            request,
            "partials/models_body.html",
            _ctx(request, user, **context),
        )
    )
