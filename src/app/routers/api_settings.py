"""REST API -- manager settings.

One route group per settings section, all administrator-only and all taking
the `revision` of the document they were built on, so a save from a stale tab
is refused instead of silently undoing a newer one. The logo upload is the
exception: it is a whole-file replacement with nothing to merge, so it carries
no revision.

`GET /brand/logo` serves the stored logo. It is open to any signed-in user
because the sidebar of every page renders it; it is not an administrator
surface.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, UploadFile, status
from pydantic import BaseModel

from app.auth.csrf import verify_csrf
from app.auth.deps import AdminUser, AnyUser
from app.config import Settings, get_settings
from app.core.audit import write_audit_entry
from app.core.settings_store import (
    MAX_LOGO_BYTES,
    BrandingSettings,
    SettingsError,
    effective_branding,
    load_settings,
    logo_media_type,
    logo_path,
    remove_logo_files,
    save_logo,
    save_settings,
    settings_revision,
    settings_to_json,
    validate_branding,
    validate_refresh_seconds,
)

router = APIRouter()


class BrandingBody(BaseModel):
    revision: str
    name: str | None = None
    tagline: str | None = None


class HostBody(BaseModel):
    revision: str
    refresh_seconds: int


class RevisionBody(BaseModel):
    revision: str


@router.get("/api/v1/settings")
async def get_all_settings(
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    return _document(settings)


@router.put("/api/v1/settings/branding")
async def put_branding(
    request: Request,
    body: BrandingBody,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Set name and tagline.

    A blank name falls back to the default; a blank tagline hides the second
    line, which is not the same as "use the default".
    """
    verify_csrf(request)
    _require_current(settings, body.revision)

    try:
        name, tagline = validate_branding(body.name, body.tagline)
    except SettingsError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    current = load_settings(settings.papaia_config_dir)
    current.branding.name = name
    current.branding.tagline = tagline
    save_settings(settings.papaia_config_dir, current)
    _audit(
        settings,
        user.preferred_username,
        "settings.branding.update",
        {"name": name, "tagline": tagline},
    )
    return _document(settings)


@router.post("/api/v1/settings/branding/logo")
async def upload_logo(
    request: Request,
    file: UploadFile,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    verify_csrf(request)

    # One byte past the cap, so an oversized body is rejected without being
    # buffered in full.
    data = await file.read(MAX_LOGO_BYTES + 1)
    try:
        filename = save_logo(settings.papaia_config_dir, data)
    except SettingsError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    current = load_settings(settings.papaia_config_dir)
    current.branding.logo = filename
    save_settings(settings.papaia_config_dir, current)
    _audit(
        settings,
        user.preferred_username,
        "settings.branding.logo.upload",
        {"logo": filename, "bytes": len(data)},
    )
    return _document(settings)


@router.delete("/api/v1/settings/branding/logo")
async def delete_logo(
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    verify_csrf(request)

    current = load_settings(settings.papaia_config_dir)
    current.branding.logo = None
    save_settings(settings.papaia_config_dir, current)
    remove_logo_files(settings.papaia_config_dir)
    _audit(settings, user.preferred_username, "settings.branding.logo.delete", {})
    return _document(settings)


@router.post("/api/v1/settings/branding/reset")
async def reset_branding(
    request: Request,
    body: RevisionBody,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    verify_csrf(request)
    _require_current(settings, body.revision)

    current = load_settings(settings.papaia_config_dir)
    current.branding = BrandingSettings()
    save_settings(settings.papaia_config_dir, current)
    remove_logo_files(settings.papaia_config_dir)
    _audit(settings, user.preferred_username, "settings.branding.reset", {})
    return _document(settings)


@router.put("/api/v1/settings/host")
async def put_host(
    request: Request,
    body: HostBody,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Set how often the host is measured again, in seconds.

    The page shows seconds or minutes; the API only knows seconds. Out of range is
    refused here, where a hand-edited file is merely clamped on read.
    """
    verify_csrf(request)
    _require_current(settings, body.revision)

    try:
        seconds = validate_refresh_seconds(body.refresh_seconds)
    except SettingsError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    current = load_settings(settings.papaia_config_dir)
    current.host.refresh_seconds = seconds
    save_settings(settings.papaia_config_dir, current)
    _audit(settings, user.preferred_username, "settings.host.update", {"refresh_seconds": seconds})
    return _document(settings)


@router.get("/brand/logo")
async def get_logo(
    user: AnyUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    path = logo_path(settings.papaia_config_dir)
    if path is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no logo set")
    return Response(
        content=path.read_bytes(),
        media_type=logo_media_type(path),
        headers={
            # The URL carries the stored filename, which changes with the
            # content, so a long lifetime is safe.
            "Cache-Control": "private, max-age=86400",
            "X-Content-Type-Options": "nosniff",
            # Opened directly by URL, an SVG must not be able to run anything.
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
        },
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _document(settings: Settings) -> dict[str, Any]:
    config_dir = settings.papaia_config_dir
    effective = effective_branding(config_dir)
    return {
        "revision": settings_revision(config_dir),
        **settings_to_json(load_settings(config_dir)),
        "effective": {
            "name": effective.name,
            "tagline": effective.tagline,
            "logo_url": effective.logo_url,
        },
    }


def _require_current(settings: Settings, revision: str) -> None:
    if revision != settings_revision(settings.papaia_config_dir):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The settings changed since this page was loaded. Reload and try again.",
        )


def _audit(settings: Settings, user: str, action: str, params: dict[str, Any]) -> None:
    write_audit_entry(
        settings.papaia_config_dir,
        user=user,
        action=action,
        target="settings",
        params=params,
    )
