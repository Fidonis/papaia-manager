"""Sets the language of each request before anything that could produce a text runs."""
from __future__ import annotations

import logging
from typing import Any

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import Settings, get_settings
from app.core import user_prefs
from app.i18n.catalog import get_catalogs
from app.i18n.negotiate import LocaleState, resolve
from app.i18n.runtime import reset_state, set_state

logger = logging.getLogger(__name__)


def _settings_for(scope: Scope) -> Settings | None:
    """The settings the app serves with, honouring a test's dependency override."""
    app: Any = scope.get("app")
    overrides = getattr(app, "dependency_overrides", None) or {}
    resolver = overrides.get(get_settings, get_settings)
    try:
        found = resolver()
    except Exception:  # noqa: BLE001 -- no settings means no preference; the request goes on
        logger.warning("settings unavailable while choosing the language", exc_info=True)
        return None
    return found if isinstance(found, Settings) else None


def state_for_scope(scope: Scope) -> LocaleState:
    """The language of a request: the account's own choice, else the browser, else the default.

    Reads the account from the session that `SessionMiddleware` has already put in the scope.
    The session is signed, so the subject in it is the one that signed in; whether the session
    is still valid is for the auth dependency to decide, a language needs no more than a name.
    """
    catalogs = get_catalogs()
    settings = _settings_for(scope)
    session = scope.get("session")
    user = session.get("user") if isinstance(session, dict) else None
    subject = user.get("sub") if isinstance(user, dict) else None

    stored: str | None = None
    if settings is not None and isinstance(subject, str):
        stored = user_prefs.stored_locale(settings.papaia_config_dir, subject)

    return resolve(
        stored=stored,
        accept_language=Headers(scope=scope).get("accept-language"),
        default=settings.manager_default_locale if settings is not None else "en",
        catalogs=catalogs,
    )


class LocaleMiddleware:
    """Pure ASGI, so the language is in place for the exception handlers as well.

    It has to sit inside `SessionMiddleware`: it reads the account from the session.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        state = state_for_scope(scope)
        token = set_state(state)

        async def send_with_language(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if "content-language" not in headers:
                    headers["Content-Language"] = state.locale.replace("_", "-")
            await send(message)

        try:
            await self.app(scope, receive, send_with_language)
        finally:
            reset_state(token)
