"""FastAPI application factory for papaia-manager."""
from __future__ import annotations

import logging
import logging.config
from urllib.parse import quote

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app import csp
from app.auth import roles
from app.auth.csrf import get_csrf_token
from app.auth.oidc import OIDCClaims
from app.config import get_settings
from app.core import installation, kit_audit, prefs
from app.core.ingest.watcher import IngestWatcher
from app.core.jobs import JobQueue
from app.core.notifications.service import NotificationService
from app.core.papaia_lib import bootstrap
from app.core.scheduler import BackupScheduler
from app.core.vectordb.service import ConnectionService
from app.i18n import gettext as _
from app.i18n.middleware import LocaleMiddleware, settings_for_scope
from app.routers import (
    account,
    api_addons,
    api_audit,
    api_catalogs,
    api_collections,
    api_config,
    api_connections,
    api_ingest,
    api_ingest_jobs,
    api_installation,
    api_inventory,
    api_jobs,
    api_knowledge_map,
    api_knowledge_map_governance,
    api_mail,
    api_maintenance,
    api_models,
    api_modules,
    api_nodes,
    api_notifications,
    api_roles,
    api_settings,
    api_stack,
    api_tiles,
    api_upgrade,
    api_users,
    auth,
    health,
    kit,
    ui,
    ui_config,
    ui_ingest,
    ui_knowledge_map,
    ui_mail,
    ui_models,
    ui_nodes,
    ui_notifications,
    ui_widgets,
)
from app.templating import templates

_job_queue: JobQueue | None = None
_backup_scheduler: BackupScheduler | None = None
_ingest_watcher: IngestWatcher | None = None
_notification_service: NotificationService | None = None
_audit_delivery: kit_audit.Delivery | None = None


def create_app() -> FastAPI:
    settings = get_settings()

    _configure_logging(settings.log_level)

    app = FastAPI(
        title="papaia manager",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    # Registered before the session middleware, so it runs inside it: the language of a request
    # comes from the account in the session, and the exception handlers below run inside it too.
    app.add_middleware(LocaleMiddleware)
    # Between the two: with Mein Bereich on the platform, the language and the appearance come
    # from the preference service `prefs`, read before the language is chosen (app.core.prefs).
    app.add_middleware(prefs.PreferencesMiddleware, settings=settings_for_scope)
    # Inside the session middleware as well: every papaia.audit/1 row of the request names the
    # signed-in person and the request's correlation id (app.core.kit_audit).
    app.add_middleware(
        kit_audit.AuditContextMiddleware, azp=lambda: settings.manager_oidc_client_id
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.manager_session_secret,
        session_cookie="papaia_manager_session",
        same_site="lax",
        https_only=settings.manager_host.startswith("https://"),
        max_age=28_800,
    )
    # Outermost, so every HTML answer gets the page policy, the error pages of the
    # middleware above included (app/csp.py).
    app.add_middleware(csp.ReportOnlyPolicyMiddleware)

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(account.router)
    app.include_router(kit.router)
    app.include_router(ui.router)
    app.include_router(ui_config.router)
    app.include_router(ui_knowledge_map.router)
    app.include_router(ui_ingest.router)
    app.include_router(ui_mail.router)
    app.include_router(ui_models.router)
    app.include_router(ui_nodes.router)
    app.include_router(ui_notifications.router)
    app.include_router(ui_widgets.router)
    app.include_router(api_audit.router)
    app.include_router(api_inventory.router)
    app.include_router(api_catalogs.router)
    app.include_router(api_collections.router)
    app.include_router(api_config.router)
    app.include_router(api_connections.router)
    app.include_router(api_ingest.router)
    app.include_router(api_ingest_jobs.router)
    app.include_router(api_addons.router)
    app.include_router(api_jobs.router)
    app.include_router(api_knowledge_map.router)
    app.include_router(api_knowledge_map_governance.router)
    app.include_router(api_mail.router)
    app.include_router(api_maintenance.router)
    app.include_router(api_models.router)
    app.include_router(api_modules.router)
    app.include_router(api_nodes.router)
    app.include_router(api_notifications.router)
    app.include_router(api_roles.router)
    app.include_router(api_settings.router)
    app.include_router(api_installation.router)
    app.include_router(api_stack.router)
    app.include_router(api_tiles.router)
    app.include_router(api_upgrade.router)
    app.include_router(api_users.router)

    app.mount("/static", StaticFiles(directory="app/static"), name="static")

    @app.exception_handler(401)
    async def _handle_unauthorized(request: Request, exc: Exception) -> Response:
        """Send XHR callers a bare 401; navigations get the login redirect.

        HTMX and fetch() cannot usefully follow a cross-origin redirect to
        Keycloak -- the request dies at the CORS boundary and the caller sees
        nothing. A 401 they can act on (reload, which re-authenticates in the
        top-level context) is the useful answer. Only a real navigation gets
        the 307, and it carries where to return afterwards.
        """
        if request.headers.get("HX-Request") or request.url.path.startswith("/api/"):
            return JSONResponse({"detail": _("session expired")}, status_code=401)
        return RedirectResponse(url="/auth/login?next=" + quote(request.url.path, safe="/"))

    @app.exception_handler(status.HTTP_403_FORBIDDEN)
    async def _forbidden(request: Request, exc: Exception) -> Response:
        """Render a denial as a page for navigations, as JSON under /api/.

        Dashboard-only accounts can now reach the application, so a 403 is a
        state a browser lands on rather than an API-only condition. The path
        split keeps the JSON contract intact for the fetch() callers and for
        CSRF rejections, which surface on the same status code.
        """
        detail = str(getattr(exc, "detail", "") or _("You do not have access to this page."))
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": detail}, status_code=status.HTTP_403_FORBIDDEN)

        claims = _session_claims(request)
        current = request.app.dependency_overrides.get(get_settings, get_settings)()
        return templates.TemplateResponse(
            request,
            "error.html",
            {
                "status_code": status.HTTP_403_FORBIDDEN,
                "heading": _("Access denied"),
                "message": detail,
                # Whom to ask, worded as every module words it (UXA-N04).
                "contact": installation.contact_point(current.papaia_config_dir),
                "show_contact": True,
                "csrf_token": get_csrf_token(request),
                "user": claims,
                "is_admin": claims is not None and roles.is_admin(claims, settings),
            },
            status_code=status.HTTP_403_FORBIDDEN,
        )

    @app.on_event("startup")
    async def _startup() -> None:
        global _job_queue, _backup_scheduler, _ingest_watcher, _notification_service  # noqa: PLW0603
        global _audit_delivery  # noqa: PLW0603

        logger = logging.getLogger(__name__)

        # Rows wait in the outbox until papaia-audit acknowledges them; without the client
        # certificate of the platform PKI they wait until it is mounted.
        kit_audit.configure(settings)
        transport = kit_audit.ingest_transport(settings)
        if transport is None:
            logger.info("papaia-audit delivery is off: no client certificate; rows stay queued")
        else:
            _audit_delivery = kit_audit.start_delivery(settings.papaia_config_dir, transport)

        try:
            bootstrap(settings.papaia_workspace_dir)
        except RuntimeError as exc:
            logger.warning("papaia workspace bootstrap skipped: %s", exc)

        _job_queue = JobQueue(settings.papaia_config_dir)
        _job_queue.start()

        # After the queue: a scheduled run is enqueued onto it. Built here, inside
        # the running event loop, because AsyncIOScheduler binds to the loop it is
        # created in. A scheduler that cannot start costs the schedule, not the
        # manager -- everything else on the panel works without it.
        scheduler: BackupScheduler | None = None
        try:
            scheduler = BackupScheduler(settings, _job_queue)
            scheduler.start()
            _backup_scheduler = scheduler
        except Exception:
            logger.exception("backup scheduler failed to start; continuing without it")
            if scheduler is not None:
                scheduler.shutdown()
            _backup_scheduler = None

        # Cleans up after embedding runs when nobody has the Embedding page open. Built here
        # for the same reason as the scheduler: it needs the running event loop. It does
        # nothing without the RAG profile, and a failure costs the clean-up, not the manager.
        try:
            watcher = IngestWatcher(settings)
            watcher.start()
            _ingest_watcher = watcher
        except Exception:
            logger.exception("the embedding clean-up failed to start; continuing without it")
            _ingest_watcher = None

        # Tells the recipients of Mail > Notifications what happened. It listens to the job
        # queue for failed jobs and looks for the rest every few minutes, so it needs the
        # queue and the running event loop. A failure costs the notifications, not the manager.
        try:
            service = NotificationService(settings, _job_queue)
            _job_queue.add_finished_listener(service.on_job_finished)
            service.start()
            _notification_service = service
        except Exception:
            logger.exception("notifications failed to start; continuing without them")
            _notification_service = None

        # The ingester needs a connection before anybody opens a page of the manager.
        # `ensure_default` never raises and does nothing without the RAG profile.
        try:
            if ConnectionService(settings).ensure_default() == "seeded":
                logger.info("created the default connection to the integrated Qdrant")
        except Exception:
            logger.exception("the default connection could not be checked; continuing")
        logger.info("papaia-manager started (host=%s)", settings.manager_host)

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        if _notification_service is not None:
            _notification_service.shutdown()
        if _ingest_watcher is not None:
            _ingest_watcher.shutdown()
        if _backup_scheduler is not None:
            _backup_scheduler.shutdown()
        if _job_queue is not None:
            _job_queue.stop()
        if _audit_delivery is not None:
            _audit_delivery.stop()

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
