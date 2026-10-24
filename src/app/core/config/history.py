"""The previous versions of an overlay file, kept so that a change can be taken back.

Before the manager replaces or removes an overlay file it copies the old bytes here:

    $PAPAIA_CONFIG_DIR/manager/overlay-history/<key>/<UTC stamp>-<event>.bak

`event` says what is about to happen to the file (`apply`, `discard`, `rollback`). The copy
is byte for byte, so restoring it gives back exactly what was there. Only the latest `KEEP`
per target are kept. The directory sits in the configuration directory, so `papaia-ctl
backup` carries it along, and it is created readable by its owner only: an overlay can hold
an API key.

A version is named by its id, and the id is checked against a fixed pattern before it is
joined to a path, so nothing a request says can leave the directory.
"""

from __future__ import annotations

import contextlib
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.core.config.overlay import OverlayError, revision_of, target
from app.i18n import gettext as _

KEEP = 20
EVENTS = ("apply", "discard", "rollback")

_ID_RE = re.compile(r"^(\d{8}T\d{6}Z)-(apply|discard|rollback)(?:-(\d{1,3}))?$")
_SUFFIX = ".bak"


@dataclass(frozen=True)
class Version:
    id: str
    when: datetime
    event: str
    size: int
    revision: str

    def as_json(self) -> dict[str, object]:
        return {
            "id": self.id,
            "when": self.when.isoformat(),
            "event": self.event,
            "size": self.size,
            "revision": self.revision[:7],
        }


def history_dir(config_dir: str, key: str) -> Path:
    target(key)  # refuses a key that is not a target
    return Path(config_dir) / "manager" / "overlay-history" / key


def valid_id(version_id: str) -> bool:
    return _ID_RE.match(version_id) is not None


def _path(config_dir: str, key: str, version_id: str) -> Path:
    if not valid_id(version_id):
        raise OverlayError(_("Unknown version."))
    return history_dir(config_dir, key) / f"{version_id}{_SUFFIX}"


def save(config_dir: str, key: str, data: bytes, event: str, *, now: datetime | None = None) -> str:
    """Copy `data` into the history and return its id. Raises OverlayError when it can't."""
    if event not in EVENTS:
        raise ValueError(_("unknown event %(event)r") % {"event": event})
    directory = history_dir(config_dir, key)
    stamp = (now or datetime.now(tz=UTC)).strftime("%Y%m%dT%H%M%SZ")
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        version_id = f"{stamp}-{event}"
        counter = 0
        while (directory / f"{version_id}{_SUFFIX}").exists():
            counter += 1
            version_id = f"{stamp}-{event}-{counter}"
        final = directory / f"{version_id}{_SUFFIX}"
        staged = directory / f".{version_id}.tmp"
        fd = os.open(
            staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600
        )
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staged, final)
    except OSError as exc:
        raise OverlayError(
            _(
                "The previous version can't be saved (%(value)s); nothing was changed."
            ) % {"value": exc.strerror or exc}
        ) from exc
    prune(config_dir, key)
    return version_id


def _parse(path: Path) -> Version | None:
    match = _ID_RE.match(path.name.removesuffix(_SUFFIX))
    if match is None or not path.name.endswith(_SUFFIX):
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    when = datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    return Version(
        id=path.name.removesuffix(_SUFFIX),
        when=when,
        event=match.group(2),
        size=len(data),
        revision=revision_of(data),
    )


def _ordered(config_dir: str, key: str) -> list[Path]:
    directory = history_dir(config_dir, key)
    if not directory.is_dir():
        return []
    files = [
        p for p in directory.iterdir() if p.is_file() and valid_id(p.name.removesuffix(_SUFFIX))
    ]

    # The stamp sorts as time; the counter breaks a tie within a second.
    def order(path: Path) -> tuple[str, int]:
        match = _ID_RE.match(path.name.removesuffix(_SUFFIX))
        assert match is not None
        return match.group(1), int(match.group(3) or 0)

    return sorted(files, key=order, reverse=True)


def versions(config_dir: str, key: str) -> list[Version]:
    """The saved versions of a target, newest first."""
    found = (_parse(p) for p in _ordered(config_dir, key))
    return [v for v in found if v is not None]


def read(config_dir: str, key: str, version_id: str) -> bytes:
    path = _path(config_dir, key, version_id)
    try:
        return path.read_bytes()
    except FileNotFoundError as exc:
        raise OverlayError(_("Unknown version.")) from exc
    except OSError as exc:
        raise OverlayError(_(
            "The version can't be read (%(value)s)."
        ) % {"value": exc.strerror or exc}) from exc


def prune(config_dir: str, key: str, *, keep: int = KEEP) -> None:
    """Drop everything but the newest `keep`. Best effort: a version that can't be removed
    stays, which costs space and nothing else."""
    for path in _ordered(config_dir, key)[keep:]:
        with contextlib.suppress(OSError):
            path.unlink()
