"""Read-only access to the papaia-ctl backup catalogue.

`papaia-ctl backup` writes every restore point into `$PAPAIA_BACKUP_DIR`: one
timestamped directory per run, a catalogue (`backup.yaml`) and an operation log
(`backup.log`) next to them. This module reads that catalogue so the Maintenance
page can list restore points; it never writes anything -- creating and consuming
restore points is papaia-ctl's job.

The catalogue is parsed here rather than through `lib.backup` from the mounted
workspace. Nothing under `app/` imports `lib.*` today, and the test suite skips
the lifespan that puts `papaia/tools` on `sys.path`, so a `lib.*` import would
not be exercisable by any test. The cost is knowing two field names
(`backups[].id`, `backups[].result`); the same trade-off is already made for
`deployment.yaml` and `installed.yaml`.

Every read fails soft. A backup directory that does not exist yet, a catalogue
that was never written, and a truncated file all mean "no restore points to
show" -- an operator who has not taken a backup yet must land on an empty page,
not an error.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from app.core.envfile import load_env_file

logger = logging.getLogger(__name__)

INDEX_NAME = "backup.yaml"
MANIFEST_NAME = "manifest.yaml"

# Restore point ids are the local-time stamp papaia-ctl derives from
# "%Y-%m-%d_%H-%M-%S". Anchored and exact: the id reaches both a path join and a
# `--restore-point=` argv, so this is the guard against traversal ("../..") and
# against a value that would be read as another flag ("--restart-clean").
_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$")

# Outcomes papaia-ctl records per run. Anything else is an unknown result and is
# surfaced verbatim rather than coerced -- a catalogue written by a newer core
# must not be silently relabelled.
RESULT_OK = "ok"
RESULT_PARTIAL = "partial"
RESULT_FAILED = "failed"


@dataclass
class RestorePoint:
    """One catalogued restore point, as recorded in backup.yaml."""

    id: str
    created_at: str = ""
    size_mb: float = 0.0
    result: str = ""
    artifacts: int = 0
    addons: list[str] | None = None
    papaia_version: str = ""
    project: str = ""

    @property
    def is_usable(self) -> bool:
        """False for a run where no archive could be written.

        papaia-ctl refuses to pick a `failed` entry implicitly, and restoring
        from one would replace live data with a knowingly empty snapshot. The UI
        offers such an entry for inspection but not for restore.
        """
        return self.result != RESULT_FAILED


def is_valid_restore_point_id(value: str) -> bool:
    """True if `value` is shaped like a papaia-ctl restore point id."""
    return bool(_ID_RE.match(value))


def resolve_backup_dir(config_dir: str) -> Path | None:
    """Return `PAPAIA_BACKUP_DIR` from the config bundle's root .env.

    Same source papaia-ctl resolves it from, so the manager and a shell on the
    host agree on where backups live without the value being duplicated into the
    manager's own settings. None means the variable is absent or empty, which is
    the state of a config directory seeded by a core older than the backup
    commands.
    """
    value = load_env_file(Path(config_dir) / ".env").get("PAPAIA_BACKUP_DIR", "")
    return Path(value) if value else None


def is_reachable(backup_dir: Path | None) -> bool:
    """True if the backup directory is visible to this process.

    Distinct from "no backups yet": the manager container mounts the backup
    directory at its host path, so an unreachable path means the mount is
    missing, and reporting that is more useful than an empty list.
    """
    return backup_dir is not None and backup_dir.is_dir()


def load_restore_points(backup_dir: Path | None) -> list[RestorePoint]:
    """Return the catalogued restore points, newest first."""
    if backup_dir is None:
        return []
    index_path = backup_dir / INDEX_NAME
    if not index_path.is_file():
        return []
    try:
        raw = yaml.safe_load(index_path.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError) as exc:
        logger.warning("cannot read %s: %s", index_path, exc)
        return []
    if not isinstance(raw, dict):
        logger.warning("%s does not contain a mapping; ignoring it", index_path)
        return []

    points = [
        _restore_point_from_dict(entry)
        for entry in raw.get("backups") or []
        if isinstance(entry, dict)
    ]
    # papaia-ctl keeps the catalogue in ascending creation order; the newest
    # restore point is the one an operator reaches for, so invert it here once
    # instead of in every caller and template.
    points.sort(key=lambda p: p.created_at, reverse=True)
    return points


def restore_point_time(point: RestorePoint) -> datetime | None:
    """When the restore point was taken, from the catalogue's UTC `created_at`.

    None for a value that does not parse: such an entry takes part in nothing that
    needs a time, the same way papaia-ctl's own retention leaves it alone.
    """
    if not point.created_at:
        return None
    try:
        taken = datetime.fromisoformat(point.created_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return taken if taken.tzinfo is not None else taken.replace(tzinfo=UTC)


def newest_successful(points: Iterable[RestorePoint]) -> RestorePoint | None:
    """The newest restore point whose run finished `ok`.

    `partial` is a usable restore point but not a successful backup: some archive
    failed, so it must not be what tells a schedule that all is well.
    """
    dated = [
        (taken, point)
        for point in points
        if point.result == RESULT_OK and (taken := restore_point_time(point)) is not None
    ]
    return max(dated, key=lambda pair: pair[0])[1] if dated else None


def last_successful_backup(backup_dir: Path | None) -> RestorePoint | None:
    """The newest successfully completed restore point in the catalogue, or None."""
    return newest_successful(load_restore_points(backup_dir))


def last_backup_age(config_dir: str, *, now: datetime | None = None) -> timedelta | None:
    """How long ago the newest successful backup was taken, or None if there is none.

    One function for every consumer -- the Backup page's status strip, a dashboard
    widget, an alert rule -- so that none of them computes it a little differently.
    """
    newest = last_successful_backup(resolve_backup_dir(config_dir))
    taken = restore_point_time(newest) if newest is not None else None
    if taken is None:
        return None
    return (now or datetime.now(tz=UTC)) - taken


def count_older_than(points: Iterable[RestorePoint], days: int, *, now: datetime) -> int:
    """How many restore points papaia-ctl's retention would delete at `days`.

    The same rule as the core's `prune`: strictly older than the cutoff, and an
    entry whose time cannot be read is kept.
    """
    cutoff = now - timedelta(days=days)
    return sum(1 for p in points if (taken := restore_point_time(p)) is not None and taken < cutoff)


def find_restore_point(backup_dir: Path | None, restore_point_id: str) -> RestorePoint | None:
    """Return one catalogued restore point by id, or None."""
    if not is_valid_restore_point_id(restore_point_id):
        return None
    return next(
        (p for p in load_restore_points(backup_dir) if p.id == restore_point_id),
        None,
    )


def snapshot_manifest(backup_dir: Path | None, restore_point_id: str) -> dict[str, Any] | None:
    """Return a snapshot's self-describing manifest, or None if unreadable.

    The manifest lists only the artifacts that actually succeeded, so it -- not
    the plan -- is what tells an operator what a restore would put back.
    """
    if backup_dir is None or not is_valid_restore_point_id(restore_point_id):
        return None
    manifest_path = backup_dir / restore_point_id / MANIFEST_NAME
    if not manifest_path.is_file():
        return None
    try:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError) as exc:
        logger.warning("cannot read %s: %s", manifest_path, exc)
        return None
    return raw if isinstance(raw, dict) else None


def _restore_point_from_dict(raw: dict[str, Any]) -> RestorePoint:
    addons = [str(a) for a in raw.get("addons") or []]
    return RestorePoint(
        id=str(raw.get("id", "")),
        created_at=str(raw.get("created_at", "")),
        size_mb=_as_float(raw.get("size_mb")),
        result=str(raw.get("result", "")),
        artifacts=_as_int(raw.get("artifacts")),
        addons=addons,
        papaia_version=str(raw.get("papaia_version", "")),
        project=str(raw.get("project", "")),
    )


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def restore_point_to_dict(point: RestorePoint) -> dict[str, Any]:
    """JSON shape for the API and the templates."""
    return {
        "id": point.id,
        "created_at": point.created_at,
        "size_mb": point.size_mb,
        "result": point.result,
        "artifacts": point.artifacts,
        "addons": point.addons or [],
        "papaia_version": point.papaia_version,
        "project": point.project,
        "usable": point.is_usable,
    }
