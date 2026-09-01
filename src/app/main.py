"""FastAPI application factory for papaia-manager."""
from __future__ import annotations

import logging
import logging.config

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.auth import roles
from app.auth.csrf import get_csrf_token
from app.auth.oidc import OIDCClaims
from app.config import get_settings
from app.core.jobs import JobQueue
from app.core.papaia_lib import bootstrap
from app.routers import (
    api_addons,
    api_catalogs,
    api_jobs,
    api_maintenance,
    api_stack,
    api_tiles,
    auth,
    health,
    ui,
)
from app.templating import templates

_job_queue: JobQueue | None = None


def create_app() -> FastAPI:
    settings = get_settings()

    _configure_logging(settings.log_level)

    app = FastAPI(
        title="papaia manager",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.manager_session_secret,
        session_cookie="papaia_manager_session",
        same_site="lax",
        https_only=settings.manager_host.startswith("https://"),
        max_age=28_800,
    )

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(ui.router)
    app.include_router(api_catalogs.router)
    app.include_router(api_addons.router)
    app.include_router(api_jobs.router)
    app.include_router(api_maintenance.router)
    app.include_router(api_stack.router)
    app.include_router(api_tiles.router)

    app.mount("/static", StaticFiles(directory="app/static"), name="static")

    @app.exception_handler(401)
    async def _redirect_to_login(request: Request, exc: Exception) -> RedirectResponse:
        return RedirectResponse(url="/auth/login")

    @app.exception_handler(status.HTTP_403_FORBIDDEN)
    async def _forbidden(request: Request, exc: Exception) -> Response:
        """Render a denial as a page for navigations, as JSON under /api/.

        Dashboard-only accounts can now reach the application, so a 403 is a
        state a browser lands on rather than an API-only condition. The path
        split keeps the JSON contract intact for the fetch() callers and for
        CSRF rejections, which surface on the same status code.
        """
        detail = str(getattr(exc, "detail", "") or "You do not have access to this page.")
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": detail}, status_code=status.HTTP_403_FORBIDDEN)

        claims = _session_claims(request)
        return templates.TemplateResponse(
            request,
            "error.html",
            {
                "status_code": status.HTTP_403_FORBIDDEN,
                "heading": "Access denied",
                "message": detail,
                "csrf_token": get_csrf_token(request),
                "user": claims,
                "is_admin": claims is not None and roles.is_admin(claims, settings),
            },
            status_code=status.HTTP_403_FORBIDDEN,
        )

    @app.on_event("startup")
    async def _startup() -> None:
        global _job_queue  # noqa: PLW0603

        logger = logging.getLogger(__name__)

        try:
            bootstrap(settings.papaia_workspace_dir)
        except RuntimeError as exc:
            logger.warning("papaia workspace bootstrap skipped: %s", exc)

        _job_queue = JobQueue(settings.papaia_config_dir)
        _job_queue.start()
        logger.info("papaia-manager started (host=%s)", settings.manager_host)

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        if _job_queue is not None:
            _job_queue.stop()

    return app


def _session_claims(request: Request) -> OIDCClaims | None:
    """Best-effort read of the session user, for error pages only.

    Never raises: an error page must render even when the session is the
    thing that is broken.
    """
    raw = request.session.get("user")
    if not raw:
        return None
    try:
        return OIDCClaims.from_dict(raw)
    except (KeyError, ValueError, TypeError):
        return None


def _configure_logging(level: str) -> None:
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {
                    "format": "%(asctime)s %(levelname)-8s %(name)s %(message)s",
                    "datefmt": "%Y-%m-%dT%H:%M:%S",
                }
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                }
            },
            "root": {"level": level.upper(), "handlers": ["console"]},
        }
    )
