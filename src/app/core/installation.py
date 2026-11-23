"""The installation's name, suffix, logo and contact point, the four-eyes rule and the
delegation lifetime, kept by the core and published in the platform catalog.

The manager stores none of them. Every change goes through the core's own commands
(`papaia-ctl settings set|unset`, `papaia-ctl delegations lifetime`), which check the value
again, apply the security profile (in `kritis` four eyes is always on and a delegation lives
at most 7 days unused and 30 days in all), write one `papaia.audit/1` row of their own and
render the platform catalog. Every module reads the catalog at least every 60 seconds and
serves the logo from its own origin under `/_kit/brand/logo`, so a change reaches all of
them within that time; the manager re-reads it at once.

This module checks every value before the core is asked (the same lengths and patterns as
the kit's `Branding` definition), so a refused value never starts a process, and turns the
core's exit codes into `InstallationError`s with an HTTP status.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core import ctl
from app.core.module_catalog import _reader, platform_catalog
from app.i18n import gettext as _

# Limits of the kit's `Branding` definition (platform-services.v1).
NAME_MAX = 60
SUFFIX_MAX = 60
CONTACT_NAME_MAX = 120
MAIL_MAX = 254
MAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
PHONE_RE = re.compile(r"\+?[0-9 ()/-]{3,40}")
# The core takes PNG, JPEG and WebP of at most 1 MiB; never SVG, which could carry script
# into every module's origin.
LOGO_MAX_BYTES = 1024 * 1024
# The longest answer of `settings-show` or `delegation-lifetime --show` that is read as JSON.
MAX_SHOW_BYTES = 64 * 1024


class InstallationError(Exception):
    """A change that was not made (or not completely): `status` is the HTTP status to answer."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Paths:
    workspace_dir: str
    config_dir: str


def contact_point(config_dir: str) -> dict[str, str] | None:
    """The contact point the platform catalog names (name, and mail or phone), or None.
    Never raises: a denial page must render without it."""
    try:
        catalog = platform_catalog(config_dir)
    except Exception:  # noqa: BLE001 - presentation must not fail on a broken catalog
        return None
    branding = (catalog.branding if catalog is not None else None) or {}
    contact = branding.get("contact")
    # The kit hands nested objects out read-only (a mapping, not a dict).
    if not isinstance(contact, Mapping) or not isinstance(contact.get("name"), str):
        return None
    return {k: v for k in ("name", "mail", "phone") if isinstance(v := contact.get(k), str)}


def _text(label: str, value: str, limit: int) -> str:
    if not 1 <= len(value) <= limit:
        raise InstallationError(
            _("%(label)s must be 1 to %(limit)s characters long.") % {"label": label,
                                                                    "limit": limit},
            422,
        )
    if any(unicodedata.category(ch).startswith("C") for ch in value):
        raise InstallationError(
            _("%(label)s must not contain a control character.") % {"label": label}, 422
        )
    return value


def check_text(key: str, raw: str) -> str:
    """The value of a text setting, without surrounding spaces; InstallationError (422)
    when the core would refuse it."""
    value = raw.strip()
    if key == "branding.name":
        return _text(_("The name"), value, NAME_MAX)
    if key == "branding.suffix":
        return _text(_("The suffix"), value, SUFFIX_MAX)
    if key == "contact.name":
        return _text(_("The contact point"), value, CONTACT_NAME_MAX)
    if key == "contact.mail":
        if len(value) > MAIL_MAX or not MAIL_RE.fullmatch(value):
            raise InstallationError(_("Enter a valid mail address."), 422)
        return value
    if key == "contact.phone":
        if not PHONE_RE.fullmatch(value):
            raise InstallationError(
                _("Enter a phone number of 3 to 40 digits, spaces and ()/-, optionally "
                  "starting with +."),
                422,
            )
        return value
    raise InstallationError(_("Unknown setting."), 422)


def logo_problem(data: bytes) -> str | None:
    """Why `data` is no logo the core takes, or None."""
    if not data:
        return _("The file is empty.")
    if len(data) > LOGO_MAX_BYTES:
        return _("The logo must be at most 1 MiB.")
    if data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff"):
        return None
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return None
    return _("Use a PNG, JPEG or WebP image.")


def _error(code: int, err: str) -> InstallationError:
    # The core's last line says why ("ERROR: ..."); it names the setting, never a secret.
    lines = [line.removeprefix("ERROR: ").strip() for line in err.splitlines() if line.strip()]
    reason = lines[-1][:300] if lines else _("no reason given")
    if code == 1:
        return InstallationError(
            _("Refused by the security profile: %(reason)s") % {"reason": reason}, 409
        )
    if code == 2:
        return InstallationError(_("Refused: %(reason)s") % {"reason": reason}, 422)
    if code == 3:
        return InstallationError(
            _("Stored, but the platform catalog was not rendered: %(reason)s")
            % {"reason": reason},
            502,
        )
    return InstallationError(
        _("The core could not apply the setting: %(reason)s") % {"reason": reason}, 502
    )


def _json(out: str) -> dict[str, Any]:
    if len(out) > MAX_SHOW_BYTES:
        raise InstallationError(_("The core's answer is too long."), 502)
    try:
        doc = json.loads(out)
    except ValueError as exc:
        raise InstallationError(_("The core's answer is not JSON."), 502) from exc
    if not isinstance(doc, dict):
        raise InstallationError(_("The core's answer is not JSON."), 502)
    return doc


def _part(doc: dict[str, Any], key: str) -> dict[str, Any]:
    value = doc.get(key)
    return value if isinstance(value, dict) else {}


async def show(paths: Paths) -> dict[str, Any]:
    """The settings in effect: brand, contact point, four eyes and delegation lifetime."""
    try:
        code, out, err = await ctl.run_py_cli(
            command="settings-show",
            workspace_dir=paths.workspace_dir,
            config_dir=paths.config_dir,
            extra_flags=["--json"],
            limit=ctl.SETUP_TIMEOUT_SECONDS,
        )
        if code != 0:
            raise _error(4, err)
        shown = _json(out)
        code, out, err = await ctl.run_delegation_lifetime(
            idle_days=None, max_days=None, workspace_dir=paths.workspace_dir,
            config_dir=paths.config_dir,
        )
    except ctl.CtlError as exc:
        raise InstallationError(str(exc), 502) from exc
    # Exit 1 with JSON: stored values above the profile's bound (shown, marked).
    if code not in (0, 1) or not out.strip():
        raise _error(4, err)
    lifetime = _json(out)
    branding = _part(shown, "branding")
    approvals = _part(shown, "approvals")
    contact = _part(shown, "contact") or None
    bound = _part(lifetime, "upper_bound")
    return {
        "security_profile": str(shown.get("security_profile") or "standard"),
        "branding": {
            "name": branding.get("name"),
            "suffix": branding.get("suffix"),
            "logo": "logo_sha256" in branding,
        },
        "contact": contact,
        "four_eyes": approvals.get("four_eyes") == "on",
        "four_eyes_fixed": approvals.get("fixed") is True,
        "delegations": {
            "idle_days": lifetime.get("idle_days"),
            "max_days": lifetime.get("max_days"),
            "source": lifetime.get("source"),
            "within_bound": lifetime.get("within_bound") is not False,
            "idle_limit": bound.get("idle_days"),
            "max_limit": bound.get("max_days"),
        },
    }


def _refreshed(paths: Paths) -> None:
    """Read the catalog the core just rendered, so the manager's own pages show it at once."""
    _reader(paths.config_dir).refresh()


async def _change(paths: Paths, run: Any, *, refused: int = 1) -> None:
    """Await one core command; `refused` is its exit code for "not allowed here"."""
    try:
        code, _out, err = await run
    except ctl.CtlError as exc:
        raise InstallationError(str(exc), 502) from exc
    if code == 0:
        return
    if code == refused:
        raise _error(1, err)
    raise _error(code if code in (2, 3) else 4, err)


async def apply(paths: Paths, changes: list[tuple[str, str | None]]) -> list[str]:
    """Set (value) or unset (None) each key in order; returns the keys asked for. Stops at
    the first refusal: the keys before it are stored and audited by the core."""
    try:
        for key, value in changes:
            if value is None:
                run = ctl.run_installation_unset(
                    key, workspace_dir=paths.workspace_dir, config_dir=paths.config_dir
                )
            else:
                run = ctl.run_installation_set(
                    key, value, workspace_dir=paths.workspace_dir, config_dir=paths.config_dir
                )
            await _change(paths, run)
    finally:
        _refreshed(paths)
    return [key for key, _value in changes]


async def set_logo(paths: Paths, data: bytes) -> None:
    """Hand an uploaded logo to the core through a private file next to the settings."""
    problem = logo_problem(data)
    if problem is not None:
        raise InstallationError(problem, 422)
    folder = Path(paths.config_dir) / "manager"
    folder.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".brand-logo-", dir=folder)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        await apply(paths, [("branding.logo", name)])
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


async def set_lifetime(
    paths: Paths, idle_days: int, max_days: int, *, sync_realm: bool
) -> bool | None:
    """Set the delegation lifetime, render the catalog that publishes it and hand it to the
    realm (`keycloak-role-sync`, which runs the core's realm sync). Returns whether the realm
    took it, None when the realm is not this installation's to sync."""
    try:
        # The core's exit 2 is the refusal (a value above the profile's bound, audited);
        # exit 1 is a stored setting it cannot read.
        await _change(paths, ctl.run_delegation_lifetime(
            idle_days=idle_days, max_days=max_days, workspace_dir=paths.workspace_dir,
            config_dir=paths.config_dir,
        ), refused=-1)
        code, _out, err = await ctl.run_catalog_render(
            workspace_dir=paths.workspace_dir, config_dir=paths.config_dir
        )
    except ctl.CtlError as exc:
        raise InstallationError(str(exc), 502) from exc
    finally:
        _refreshed(paths)
    if code not in (0, 3):
        raise _error(4, err)
    if not sync_realm:
        return None
    try:
        lines = await ctl.run_core_verb(
            verb="keycloak-role-sync",
            workspace_dir=paths.workspace_dir,
            config_dir=paths.config_dir,
        )
        async for _line in lines:
            pass
    except ctl.CtlError:
        return False
    return True
