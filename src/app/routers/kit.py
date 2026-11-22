"""Routes the platform's shared frame expects of every module (papaia-kit shell contract 2, 4, 7).

- `GET /_kit/<kit version>/{css,js,icons,fonts}/<file>`: stylesheet, script, symbols and
  fonts, served from the vendored kit wheel; no copy of them lives in this repository.
- `GET /_kit/brand/logo`: the installation's logo `platform/brand-logo` next to the platform
  catalog, only while its SHA-256 equals `branding.logo_sha256` of the catalog.
- `POST /_kit/sidebar`, `/_kit/appearance`, `/_kit/language`: the footer and account menu
  forms. Each needs the session and its CSRF token (`pk_csrf`), accepts exactly its one field
  with one of its values, writes one audit entry and answers `303` back to the page it came
  from, if that page is on this origin, else to the overview.
- `GET /help` and `GET /me`: the account menu's "Help", and "My area" for a manager that runs
  without a platform catalog (with one, the entry leads to Mein Bereich).
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

import papaia_kit
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from papaia_kit import ui_dist_dir
from papaia_kit.ui.glossary import load_glossary

from app import shell
from app.auth.csrf import get_csrf_token, verify_csrf
from app.auth.deps import AnyUser
from app.auth.oidc import OIDCClaims
from app.auth.roles import is_admin
from app.config import Settings, get_settings
from app.core import user_prefs
from app.core.audit import write_audit_entry
from app.core.module_catalog import platform_catalog
from app.i18n import gettext as _
from app.i18n import runtime as i18n_runtime
from app.routers.auth import _safe_next
from app.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()

KIT_VERSION = papaia_kit.__version__
SIDEBAR_MAX_AGE = 365 * 24 * 3600
MAX_LOGO_BYTES = 512 * 1024
MAX_FIELD = 64
# Folders of the kit's dist directory a page may load, and the file types in each.
ASSETS: dict[str, dict[str, str]] = {
    "css": {".css": "text/css"},
    "js": {".js": "text/javascript"},
    "icons": {".svg": "image/svg+xml"},
    "fonts": {".woff2": "font/woff2"},
}
Kind = Literal["sidebar", "appearance", "language"]
# The terms of the help page, in this order; the texts are the kit's glossary.
HELP_TERMS = (
    "kernfunktion",
    "add_on",
    "add_on_katalog",
    "dienst",
    "job",
    "ki_landkarte",
    "vektordatenbank",
    "backup",
    "upgrade",
    "audit_log",
    "benutzer",
    "rolle",
)


def _asset(version: str, folder: str, name: str) -> tuple[Path, str] | None:
    types = ASSETS.get(folder)
    if version != KIT_VERSION or types is None:
        return None
    if "/" in name or "\\" in name or name.startswith(".") or Path(name).suffix not in types:
        return None
    path = ui_dist_dir() / folder / name
    return (path, types[path.suffix]) if path.is_file() else None


@router.get("/_kit/{version}/{folder}/{name}", include_in_schema=False)
async def kit_asset(version: str, folder: str, name: str) -> Response:
    found = _asset(version, folder, name)
    if found is None:
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    path, media = found
    # The path names the kit version: the bytes never change under it.
    return Response(
        path.read_bytes(),
        media_type=media,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


def _logo_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:256].lstrip().startswith((b"<svg", b"<?xml")):
        return "image/svg+xml"
    return None


@router.get("/_kit/brand/logo", include_in_schema=False)
async def brand_logo(settings: Annotated[Settings, Depends(get_settings)]) -> Response:
    catalog = platform_catalog(settings.papaia_config_dir)
    branding = catalog.branding if catalog is not None else None
    digest = str((branding or {}).get("logo_sha256") or "").removeprefix("sha256:")
    if not digest:
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    path = Path(settings.papaia_config_dir) / "platform" / "brand-logo"
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_LOGO_BYTES + 1)
    except OSError:
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    media = _logo_type(data)
    if len(data) > MAX_LOGO_BYTES or media is None or hashlib.sha256(data).hexdigest() != digest:
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    # A logo is shown, never run: no script in an SVG gets to execute.
    return Response(
        data,
        media_type=media,
        headers={
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _back(request: Request) -> str:
    """The page the form was sent from, if it is a page of this origin; else the overview."""
    parts = urlsplit(request.headers.get("referer") or "")
    origins = {(request.url.scheme, request.url.netloc)}
    own = urlsplit(request.app.dependency_overrides.get(get_settings, get_settings)().manager_host)
    origins.add((own.scheme, own.netloc))
    if (parts.scheme, parts.netloc) not in origins:
        return "/"
    return _safe_next(parts.path + (f"?{parts.query}" if parts.query else "")) or "/"


async def _choice(request: Request, kind: Kind, allowed: tuple[str, ...]) -> str:
    """The one value of a frame form, after the session's CSRF token; 400 for anything else."""
    form = await request.form()
    token = form.get("pk_csrf")
    verify_csrf(request, token if isinstance(token, str) else None)
    values = form.getlist(kind)
    value = values[0] if len(values) == 1 else None
    if (
        set(form.keys()) - {"pk_csrf", kind}
        or not isinstance(value, str)
        or len(value) > MAX_FIELD
        or value not in allowed
    ):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_("Invalid value."))
    return value


def _audit(settings: Settings, user: OIDCClaims, kind: Kind) -> None:
    # The value is a choice of presentation, not content; the entry records that it changed.
    write_audit_entry(
        settings.papaia_config_dir,
        user=user.preferred_username,
        action=f"kit.{kind}",
        target=f"/_kit/{kind}",
    )


@router.post("/_kit/sidebar", include_in_schema=False)
async def sidebar(
    request: Request, user: AnyUser, settings: Annotated[Settings, Depends(get_settings)]
) -> Response:
    value = await _choice(request, "sidebar", shell.SIDEBARS)
    _audit(settings, user, "sidebar")
    response = RedirectResponse(_back(request), status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        shell.SIDEBAR_COOKIE,
        value,
        max_age=SIDEBAR_MAX_AGE,
        path="/",
        httponly=True,
        secure=settings.manager_host.startswith("https://"),
        samesite="lax",
    )
    return response


@router.post("/_kit/appearance", include_in_schema=False)
async def appearance(
    request: Request, user: AnyUser, settings: Annotated[Settings, Depends(get_settings)]
) -> Response:
    value = await _choice(request, "appearance", shell.APPEARANCES)
    # Kept in the signed session until the preference service holds it for every module.
    request.session[shell.APPEARANCE_KEY] = value
    _audit(settings, user, "appearance")
    return RedirectResponse(_back(request), status_code=status.HTTP_303_SEE_OTHER)


@router.post("/_kit/language", include_in_schema=False)
async def language(
    request: Request, user: AnyUser, settings: Annotated[Settings, Depends(get_settings)]
) -> Response:
    value = await _choice(request, "language", shell.LANGUAGE_SETTINGS)
    try:
        user_prefs.set_locale(
            settings.papaia_config_dir, user.sub, None if value == "browser" else value
        )
    except user_prefs.PreferencesError as exc:
        logger.warning("language not saved: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_("The language could not be saved. Check the manager's log."),
        ) from exc
    _audit(settings, user, "language")
    return RedirectResponse(_back(request), status_code=status.HTTP_303_SEE_OTHER)


def _page(request: Request, user: OIDCClaims, name: str, **extra: object) -> HTMLResponse:
    settings = request.app.dependency_overrides.get(get_settings, get_settings)()
    return templates.TemplateResponse(
        request,
        name,
        {
            "user": user,
            "csrf_token": get_csrf_token(request),
            "is_admin": is_admin(user, settings),
            **extra,
        },
    )


@router.get("/help", response_class=HTMLResponse, include_in_schema=False)
async def help_page(
    request: Request, user: AnyUser, settings: Annotated[Settings, Depends(get_settings)]
) -> HTMLResponse:
    """The manager's terms from the kit's glossary, and whom to ask at this installation."""
    lang = shell.frame_language(i18n_runtime.current_locale())
    glossary = load_glossary()
    terms = [glossary.by_id[term].lang(lang) for term in HELP_TERMS]
    catalog = platform_catalog(settings.papaia_config_dir)
    branding = (catalog.branding if catalog is not None else None) or {}
    contact = branding.get("contact")
    if not isinstance(contact, dict):
        contact = {}
    return _page(request, user, "help.html", terms=terms, contact=contact)


@router.get("/me", response_class=HTMLResponse, include_in_schema=False)
async def my_area(
    request: Request, user: AnyUser, settings: Annotated[Settings, Depends(get_settings)]
) -> Response:
    """The account as the manager knows it; with a platform, Mein Bereich is the place."""
    catalog = platform_catalog(settings.papaia_config_dir)
    me = catalog.surface("me") if catalog is not None else None
    if me is not None:
        return RedirectResponse(me.public_url, status_code=status.HTTP_303_SEE_OTHER)
    return _page(request, user, "me.html", roles=shell.roles_of(user.roles))
