"""`manager/preferences.yaml`: what each account chose for itself, keyed by its OIDC subject.

Today that is the interface language. The file lives on the server, not in the browser, so the
choice is the same on every computer the account signs in from, and it travels with a backup like
the rest of `manager/`. It is a document of its own, not a section of `settings.yaml`: that file
is checked against one revision for the whole installation, so every account saving its language
would make an administrator's open Settings page stale.

The subject is a pseudonymous id (a UUID in the bundled Keycloak), never a name or an address.
"""
from __future__ import annotations

import contextlib
import logging
import os
import re
import threading
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.i18n import gettext as _

logger = logging.getLogger(__name__)

FILE_NAME = "preferences.yaml"

_HEADER = (
    "# Written by the papAIa manager. Each account's own choices, keyed by its OIDC subject.\n"
)

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_MAX_SUBJECT = 512

# One lock for the read-modify-write; a single process writes the file (one worker), the same
# assumption the other stores make.
_LOCK = threading.Lock()


class PreferencesError(Exception):
    """The file is there but cannot be used, or cannot be written.

    A write over an unreadable file would replace what somebody put there by hand, so there is
    none until it is repaired.
    """


class AccountPreferences(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # None is "automatic": follow the browser, then the installation's default.
    locale: str | None = None


class Preferences(BaseModel):
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)

    users: dict[str, AccountPreferences] = Field(default_factory=dict)


def preferences_path(config_dir: str) -> Path:
    return Path(config_dir) / "manager" / FILE_NAME


def valid_subject(subject: str) -> bool:
    return bool(subject) and len(subject) <= _MAX_SUBJECT and not _CONTROL_CHARS.search(subject)


# path -> (st_mtime_ns, st_size, parsed). The language is looked up on every request, so a
# request must not parse the file again unless it changed.
_CACHE: dict[Path, tuple[int, int, Preferences]] = {}


def _read(path: Path) -> Preferences:
    """The parsed file; raises `PreferencesError` when it exists but is unusable."""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return Preferences()
    except OSError as exc:
        raise PreferencesError(
            _("%(FILE_NAME)s cannot be read.") % {"FILE_NAME": FILE_NAME}
        ) from exc

    cached = _CACHE.get(path)
    if cached is not None and (cached[0], cached[1]) == (stat.st_mtime_ns, stat.st_size):
        return cached[2]

    try:
        raw = yaml.safe_load(path.read_bytes())
        parsed = Preferences.model_validate(raw if raw is not None else {})
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        # The log may name the problem; it repeats no value.
        logger.warning("%s is not usable: %s", path, type(exc).__name__)
        raise PreferencesError(_("%(FILE_NAME)s is not valid.") % {"FILE_NAME": FILE_NAME}) from exc

    _CACHE[path] = (stat.st_mtime_ns, stat.st_size, parsed)
    return parsed


def stored_locale(config_dir: str, subject: str) -> str | None:
    """The language an account chose, or None. Never raises: a missing or damaged file means
    nobody chose anything, and the page must render regardless."""
    if not valid_subject(subject):
        return None
    try:
        entry = _read(preferences_path(config_dir)).users.get(subject)
    except PreferencesError:
        return None
    return entry.locale if entry is not None else None


def _write_private(path: Path, data: bytes) -> None:
    """Replace `path` with `data`, readable by the owner only from the first byte."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    # O_BINARY: on Windows a descriptor is otherwise opened in text mode.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


def _save(path: Path, prefs: Preferences) -> None:
    body = yaml.safe_dump(
        prefs.model_dump(mode="json", exclude_none=True),
        default_flow_style=False,
        sort_keys=True,
        allow_unicode=True,
    )
    try:
        _write_private(path, (_HEADER + body).encode("utf-8"))
    except OSError as exc:
        raise PreferencesError(
            _("%(FILE_NAME)s cannot be written.") % {"FILE_NAME": FILE_NAME}
        ) from exc
    _CACHE.pop(path, None)


def set_locale(config_dir: str, subject: str, locale: str | None) -> None:
    """Remember an account's language; None puts it back to "automatic".

    The caller has checked that `locale` is one that is shipped. Raises `PreferencesError`
    when the file cannot be read or written; nothing is changed then.
    """
    if not valid_subject(subject):
        raise PreferencesError(_("The account has no usable id."))
    path = preferences_path(config_dir)
    with _LOCK:
        prefs = _read(path)
        entry = prefs.users.get(subject, AccountPreferences())
        if entry.locale == locale:
            return
        entry = entry.model_copy(update={"locale": locale})
        if entry.model_dump(exclude_none=True):
            prefs.users[subject] = entry
        else:
            # Nothing but defaults left: no entry at all is the same, and keeps the file small.
            prefs.users.pop(subject, None)
        _save(path, prefs)
