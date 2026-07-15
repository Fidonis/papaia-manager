"""Server-rendered HTML pages (Jinja2 + HTMX)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.auth.csrf import get_csrf_token
from app.auth.deps import CurrentUser
from app.auth.oidc import OIDCClaims

router = APIRouter()

_templates = Jinja2Templates(directory="app/templates")


def _ctx(request: Request, user: OIDCClaims, **extra: Any) -> dict[str, Any]:
    return {
        "request": request,
        "user": user,
        "csrf_token": get_csrf_token(request),
        **extra,
    }


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    user: CurrentUser,
) -> HTMLResponse:
    return _templates.TemplateResponse(request, "dashboard.html", _ctx(request, user))


@router.get("/addons/{name}", response_class=HTMLResponse)
async def addon_detail(
    name: str,
    request: Request,
    user: CurrentUser,
) -> HTMLResponse:
    return _templates.TemplateResponse(
        request, "addon_detail.html", _ctx(request, user, addon_name=name)
    )


@router.get("/catalogs", response_class=HTMLResponse)
async def catalogs_page(
    request: Request,
    user: CurrentUser,
) -> HTMLResponse:
    return _templates.TemplateResponse(request, "catalogs.html", _ctx(request, user))


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_log_page(
    job_id: str,
    request: Request,
    user: CurrentUser,
) -> HTMLResponse:
    return _templates.TemplateResponse(
        request, "job_log.html", _ctx(request, user, job_id=job_id)
    )
