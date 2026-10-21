"""The signed-in account's own settings. Today: the interface language."""
from __future__ import annotations

import logging
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse

from app.auth.csrf import verify_csrf
from app.auth.deps import AnyUser
from app.config import Settings, get_settings
from app.core import user_prefs
from app.i18n import get_catalogs
from app.i18n import gettext as _
from app.routers.auth import _safe_next

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/account")

# The form value for "follow the browser".
AUTOMATIC = "auto"


def _return_path(request: Request, requested: str) -> str:
    """Where to send the account after the change: the page it came from, else the dashboard.

    A path the form named, else the path (and query) of the `Referer`. Only that part is taken,
    never a host, and `_safe_next` refuses anything that is not a plain path on this origin, so
    nothing a page or another site puts in either can send the account elsewhere.
    """
    candidate = requested
    if not candidate:
        referer = urlsplit(request.headers.get("referer", ""))
        candidate = referer.path + (f"?{referer.query}" if referer.query else "")
    return _safe_next(candidate) or "/"


@router.post("/language")
async def set_language(
    request: Request,
    user: AnyUser,
    settings: Annotated[Settings, Depends(get_settings)],
    locale: Annotated[str, Form()] = AUTOMATIC,
    csrf_token: Annotated[str | None, Form()] = None,
    next_: Annotated[str, Form(alias="next")] = "",
) -> RedirectResponse:
    """Remember the language the signed-in account wants, then go back to where it was.

    Open to every role that may use the manager: it changes nothing but the account's own
    interface. The choice is kept on the server by the account's subject, so it applies on every
    computer. A plain form post, so it works before any script has loaded.
    """
    verify_csrf(request, csrf_token)

    chosen: str | None = None
    if locale != AUTOMATIC:
        chosen = get_catalogs().match(locale)
        if chosen is None:
            raise HTTPException(
                status_code=422,
                detail=_("This language is not available."),
            )

    try:
        user_prefs.set_locale(settings.papaia_config_dir, user.sub, chosen)
    except user_prefs.PreferencesError as exc:
        logger.warning("language not saved: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_("The language could not be saved. Check the manager's log."),
        ) from exc

    return RedirectResponse(
        url=_return_path(request, next_), status_code=status.HTTP_303_SEE_OTHER
    )
