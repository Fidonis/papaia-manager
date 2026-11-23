"""REST API -- the installation's settings, kept by the core (Settings -> Installation).

Name, suffix, logo and contact point of the installation, the four-eyes rule
(`approvals.four_eyes`) and the delegation lifetime. The manager stores none of them: each
route checks the request, then hands the values to the core's own command, which checks them
again, applies the security profile, writes its own `papaia.audit/1` row and renders the
platform catalog every module reads (`app.core.installation`).

Every route needs the manager's administrator role and the platform's administration role
(`PlatformAdmin`), and every write the session's CSRF token. Each write leaves one entry in
the manager's audit log and one row in its outbox naming the person who asked, under the
request's correlation id; the entries name the settings, never the contact point's values.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field

from app.auth.csrf import verify_csrf
from app.auth.deps import PlatformAdmin
from app.auth.oidc import OIDCClaims
from app.config import Settings, get_settings
from app.core import installation, nodes
from app.core.audit import write_audit_entry
from app.i18n import gettext as _

router = APIRouter()

BASE = "/api/v1/settings/installation"
# Longer than any value the core takes, so the core's own message is never the first to
# refuse a value that is merely long; the exact limits are checked in `installation`.
_TEXT_MAX = 300


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class BrandBody(_Strict):
    name: str | None = Field(default=None, max_length=_TEXT_MAX)
    suffix: str | None = Field(default=None, max_length=_TEXT_MAX)


class ContactBody(_Strict):
    name: str | None = Field(default=None, max_length=_TEXT_MAX)
    mail: str | None = Field(default=None, max_length=_TEXT_MAX)
    phone: str | None = Field(default=None, max_length=_TEXT_MAX)


class FourEyesBody(_Strict):
    four_eyes: Literal["on", "off"]


class LifetimeBody(_Strict):
    idle_days: int = Field(ge=1, le=365)
    max_days: int = Field(ge=1, le=365)


def _paths(settings: Settings) -> installation.Paths:
    return installation.Paths(settings.papaia_workspace_dir, settings.papaia_config_dir)


def _blank(value: str | None) -> bool:
    return value is None or not value.strip()


def _audit(
    settings: Settings,
    user: OIDCClaims,
    action: str,
    setting: str,
    params: dict[str, Any],
    result: str,
) -> None:
    write_audit_entry(
        settings.papaia_config_dir,
        user=user.preferred_username,
        action=action,
        target=f"installation:{setting}",
        params=params,
        result=result,
        event="config_changed",
        object_ref={"type": "setting", "id": setting},
    )


def _result(exc: installation.InstallationError) -> str:
    return "refused" if exc.status in (409, 422) else "failed"


async def _run(
    settings: Settings,
    user: OIDCClaims,
    action: str,
    setting: str,
    params: dict[str, Any],
    change: Any,
) -> Any:
    """Run one change and audit it, whatever its outcome."""
    try:
        outcome = await change
    except installation.InstallationError as exc:
        _audit(settings, user, action, setting, params, _result(exc))
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    _audit(settings, user, action, setting, params, "ok")
    return outcome


async def _shown(settings: Settings) -> dict[str, Any]:
    try:
        return await installation.show(_paths(settings))
    except installation.InstallationError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


def _checked(key: str, value: str | None) -> str | None:
    if _blank(value):
        return None
    assert value is not None
    try:
        return installation.check_text(key, value)
    except installation.InstallationError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


@router.get(BASE)
async def get_installation(
    user: PlatformAdmin, settings: Annotated[Settings, Depends(get_settings)]
) -> dict[str, Any]:
    return await _shown(settings)


@router.put(f"{BASE}/brand")
async def put_brand(
    request: Request,
    body: BrandBody,
    user: PlatformAdmin,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Name and suffix; an empty field removes the value (the product name, no suffix)."""
    verify_csrf(request)
    changes = [
        ("branding.name", _checked("branding.name", body.name)),
        ("branding.suffix", _checked("branding.suffix", body.suffix)),
    ]
    await _run(
        settings, user, "settings.installation.brand", "branding",
        {"keys": [key for key, _v in changes]},
        installation.apply(_paths(settings), changes),
    )
    return await _shown(settings)


@router.put(f"{BASE}/contact")
async def put_contact(
    request: Request,
    body: ContactBody,
    user: PlatformAdmin,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """The contact point: a name, and a mail address or phone number. An empty name removes
    the contact point; modules then say that none is on record."""
    verify_csrf(request)
    name = _checked("contact.name", body.name)
    mail = _checked("contact.mail", body.mail)
    phone = _checked("contact.phone", body.phone)
    changes: list[tuple[str, str | None]]
    if name is None:
        if mail is not None or phone is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=_("Name the contact point first; mail and phone belong to it."),
            )
        changes = [("contact", None)]
    else:
        changes = [("contact.name", name), ("contact.mail", mail), ("contact.phone", phone)]
    # The values are a person's; the entry names the fields only.
    await _run(
        settings, user, "settings.installation.contact", "contact",
        {"keys": [key for key, _v in changes]},
        installation.apply(_paths(settings), changes),
    )
    return await _shown(settings)


@router.post(f"{BASE}/logo")
async def post_logo(
    request: Request,
    file: UploadFile,
    user: PlatformAdmin,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    verify_csrf(request)
    # One byte past the cap: an oversized body is refused without being read in full.
    data = await file.read(installation.LOGO_MAX_BYTES + 1)
    await _run(
        settings, user, "settings.installation.logo", "branding.logo", {"bytes": len(data)},
        installation.set_logo(_paths(settings), data),
    )
    return await _shown(settings)


@router.delete(f"{BASE}/logo")
async def delete_logo(
    request: Request, user: PlatformAdmin, settings: Annotated[Settings, Depends(get_settings)]
) -> dict[str, Any]:
    verify_csrf(request)
    await _run(
        settings, user, "settings.installation.logo.delete", "branding.logo", {},
        installation.apply(_paths(settings), [("branding.logo", None)]),
    )
    return await _shown(settings)


@router.put(f"{BASE}/four-eyes")
async def put_four_eyes(
    request: Request,
    body: FourEyesBody,
    user: PlatformAdmin,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Switch the four-eyes rule of every module. In profile `kritis` the core refuses `off`
    (and audits the refusal); the page shows the rule as fixed there."""
    verify_csrf(request)
    await _run(
        settings, user, "settings.installation.four_eyes", "approvals.four_eyes",
        {"four_eyes": body.four_eyes},
        installation.apply(_paths(settings), [("approvals.four_eyes", body.four_eyes)]),
    )
    return await _shown(settings)


@router.put(f"{BASE}/delegations")
async def put_delegations(
    request: Request,
    body: LifetimeBody,
    user: PlatformAdmin,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """How long a delegation lives: days without use and days in all (1 to 365; in profile
    `kritis` at most 7 and 30, which the core enforces)."""
    verify_csrf(request)
    params = {"idle_days": body.idle_days, "max_days": body.max_days}
    if body.idle_days > body.max_days:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=_("The days without use cannot be more than the days in all."),
        )
    if nodes.is_distributed(settings.papaia_config_dir):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_("The delegation lifetime cannot be set in a distributed installation yet."),
        )
    synced = await _run(
        settings, user, "settings.installation.delegations", "delegations", params,
        installation.set_lifetime(
            _paths(settings), body.idle_days, body.max_days,
            sync_realm=settings.is_internal_keycloak,
        ),
    )
    return {**await _shown(settings), "realm_synced": synced}
