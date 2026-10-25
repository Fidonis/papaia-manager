"""Addon snapshot materialization (git-archive) and installed.yaml bookkeeping."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shutil
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# How many versions of one add-on stay on disk: the one that runs, the one a rollback
# goes back to, and one more.
MAX_SNAPSHOTS = 3

# A directory level of `_managed/<catalog>/` that holds versions, as opposed to the
# directory of an add-on in the layout before updates existed.
_REVISION_RE = re.compile(r"^(?:[0-9a-f]{12}|local-\d{14})$")


@dataclass(frozen=True)
class HistoryEntry:
    """A version the add-on ran before. Provenance only: where it can be rolled back to is
    in deployment.yaml (`previous`), which is what papaia-ctl acts on."""

    commit: str
    manifest_version: str
    installed_at: datetime


@dataclass
class InstalledAddon:
    catalog: str
    commit: str
    manifest_version: str
    installed_at: datetime
    managed: bool = True
    history: list[HistoryEntry] = field(default_factory=list)


def load_installed(config_dir: str) -> dict[str, InstalledAddon]:
    """Return all entries from installed.yaml keyed by addon name."""
    path = _installed_path(config_dir)
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    result: dict[str, InstalledAddon] = {}
    for name, entry in raw.get("addons", {}).items():
        try:
            result[str(name)] = InstalledAddon(
                catalog=str(entry["catalog"]),
                commit=str(entry["commit"]),
                manifest_version=str(entry["manifest_version"]),
                installed_at=datetime.fromisoformat(str(entry["installed_at"])),
                managed=bool(entry.get("managed", True)),
                history=_load_history(entry.get("history")),
            )
        except (KeyError, ValueError) as exc:
            logger.warning("skipping malformed installed.yaml entry %r: %s", name, exc)
    return result


def _load_history(raw: object) -> list[HistoryEntry]:
    if not isinstance(raw, list):
        return []
    found: list[HistoryEntry] = []
    for item in raw[:MAX_SNAPSHOTS]:
        try:
            found.append(
                HistoryEntry(
                    commit=str(item["commit"]),
                    manifest_version=str(item["manifest_version"]),
                    installed_at=datetime.fromisoformat(str(item["installed_at"])),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return found


def record_installed(
    config_dir: str,
    *,
    name: str,
    catalog: str,
    commit: str,
    manifest_version: str,
    managed: bool = True,
    history: Sequence[HistoryEntry] | None = None,
) -> None:
    """Write or update one entry in installed.yaml (atomic replace).

    `history` is the list of versions the add-on ran before, newest first. Left out, the
    entry keeps the history it has; an empty list clears it."""
    path = _installed_path(config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    raw: dict[str, Any] = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw.setdefault("version", 1)
    raw.setdefault("addons", {})
    kept = (raw["addons"].get(name) or {}).get("history")
    entry: dict[str, Any] = {
        "catalog": catalog,
        "commit": commit,
        "manifest_version": manifest_version,
        "installed_at": datetime.now(tz=UTC).isoformat(),
        "managed": managed,
    }
    if history is not None:
        entry["history"] = [
            {
                "commit": h.commit,
                "manifest_version": h.manifest_version,
                "installed_at": h.installed_at.isoformat(),
            }
            for h in history[:MAX_SNAPSHOTS]
        ]
    elif kept:
        entry["history"] = kept
    raw["addons"][name] = entry
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.dump(raw, default_flow_style=False, sort_keys=False), encoding="utf-8")
    tmp.replace(path)


def remove_installed(config_dir: str, name: str) -> None:
    """Remove an addon entry from installed.yaml."""
    path = _installed_path(config_dir)
    if not path.exists():
        return
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw.get("addons", {}).pop(name, None)
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.dump(raw, default_flow_style=False, sort_keys=False), encoding="utf-8")
    tmp.replace(path)


async def materialize_snapshot(
    *,
    catalog_clone: Path,
    addon_subdir: str,
    dest: Path,
) -> str:
    """Extract addon files from the catalog clone into dest via git-archive.

    Returns the HEAD commit SHA. dest is replaced atomically via a sibling
    temp directory so partially-written snapshots are never visible.
    """
    sha = await _get_head_sha(catalog_clone)

    staging = dest.parent / f"_{dest.name}.staging"
    if staging.exists():
        await _rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)

    archive_cmd = [
        "git",
        "-c", f"safe.directory={catalog_clone}",
        "-C",
        str(catalog_clone),
        "archive",
        "HEAD",
        "--",
        addon_subdir,
    ]
    # git archive prefixes every entry with the pathspec (addon_subdir/...),
    # but dest is the addon root itself -- strip that leading component so
    # papaia-app.yaml lands at dest/ instead of dest/<addon_subdir>/.
    tar_cmd = ["tar", "-x", "--touch", "--strip-components=1", "-C", str(staging)]

    proc_git = await asyncio.create_subprocess_exec(
        *archive_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    git_data, git_err = await proc_git.communicate()
    if proc_git.returncode != 0:
        raise RuntimeError(f"git archive failed: {git_err.decode(errors='replace')}")

    proc_tar = await asyncio.create_subprocess_exec(
        *tar_cmd,
        stdin=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, tar_err = await proc_tar.communicate(input=git_data)
    if proc_tar.returncode != 0:
        raise RuntimeError(f"tar extract failed: {tar_err.decode(errors='replace')}")

    prev = dest.parent / f"_{dest.name}.prev"
    if prev.exists():
        await _rmtree(prev)
    if dest.exists():  # noqa: ASYNC240
        dest.rename(prev)  # noqa: ASYNC240

    staging.rename(dest)
    logger.info("snapshot materialized at %s (commit %s)", dest, sha)
    return sha


async def _get_head_sha(repo: Path) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", "-c", f"safe.directory={repo}", "-C", str(repo), "rev-parse", "HEAD",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    return out.decode().strip()


async def _rmtree(path: Path) -> None:
    await asyncio.get_event_loop().run_in_executor(
        None, lambda: __import__("shutil").rmtree(path, ignore_errors=True)
    )


def _installed_path(config_dir: str) -> Path:
    return Path(config_dir) / "manager" / "installed.yaml"


def catalog_clone_path(workspace_dir: str, catalog_name: str) -> Path:
    return Path(workspace_dir) / "addons" / "_catalogs" / catalog_name


def managed_snapshot_path(
    workspace_dir: str, catalog_name: str, addon_name: str, revision: str | None = None
) -> Path:
    """Where a snapshot of an add-on lives.

    With a `revision` (twelve hex digits of the catalog commit, or `local-<timestamp>`) it is
    `_managed/<catalog>/<revision>/<add-on>`: a directory that is written once and never
    again, so the version an add-on ran before is still there to go back to. The add-on's own
    name stays the last component because Compose names the project after it. Without one it
    is the single directory the layout before updates used, which an install made then is
    still registered at."""
    base = Path(workspace_dir) / "addons" / "_managed" / catalog_name
    return base / revision / addon_name if revision else base / addon_name


def installed_addon_dir(
    deployment_entry: Mapping[str, Any] | None,
    workspace_dir: str,
    catalog_name: str,
    addon_name: str,
) -> Path:
    """The directory an installed add-on runs from.

    papaia-ctl's registration (`path` in deployment.yaml) is the truth, because an update or a
    rollback repoints it; the legacy directory is the answer for an add-on that deployment.yaml
    does not list."""
    raw = str((deployment_entry or {}).get("path") or "")
    if raw:
        path = Path(raw)
        return path if path.is_absolute() else Path(workspace_dir) / path
    return managed_snapshot_path(workspace_dir, catalog_name, addon_name)


async def head_sha(repo: Path) -> str:
    """HEAD of a catalog clone, the commit `materialize_snapshot` records."""
    return await _get_head_sha(repo)


def _same(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return a == b


def prune_snapshots(
    workspace_dir: str,
    catalog_name: str,
    addon_name: str,
    *,
    keep: Iterable[Path],
    limit: int = MAX_SNAPSHOTS,
) -> list[Path]:
    """Delete snapshots of an add-on that nothing needs.

    `keep` are the directories that must stay: the registered one, the one a rollback goes
    back to, one just staged. Of the rest the newest fill up to `limit` in all. The directory
    of the layout before updates goes as soon as it is not kept, together with the leftovers
    of its atomic swap. Returns what was removed."""
    protected = list(keep)
    base = Path(workspace_dir) / "addons" / "_managed" / catalog_name
    removed: list[Path] = []
    if not base.is_dir():
        return removed

    candidates: list[tuple[float, Path]] = []
    for revision_dir in base.iterdir():
        # An add-on called like a revision keeps its files directly in this directory.
        if not _REVISION_RE.match(revision_dir.name) or (revision_dir / "papaia-app.yaml").exists():
            continue
        snapshot = revision_dir / addon_name
        if snapshot.is_dir():
            candidates.append((snapshot.stat().st_mtime, snapshot))
    candidates.sort(key=lambda item: item[0], reverse=True)

    room = max(limit - len(protected), 0)
    for _mtime, snapshot in candidates:
        if any(_same(snapshot, p) for p in protected):
            continue
        if room > 0:
            room -= 1
            continue
        _remove_snapshot(snapshot)
        removed.append(snapshot)

    legacy = base / addon_name
    if legacy.is_dir() and not any(_same(legacy, p) for p in protected):
        _remove_snapshot(legacy)
        removed.append(legacy)
    return removed


def _remove_snapshot(snapshot: Path) -> None:
    shutil.rmtree(snapshot, ignore_errors=True)
    parent = snapshot.parent
    for leftover in (parent / f"_{snapshot.name}.prev", parent / f"_{snapshot.name}.staging"):
        if leftover.exists():
            shutil.rmtree(leftover, ignore_errors=True)
    if _REVISION_RE.match(parent.name):
        # Not empty while a snapshot of another add-on from the same revision is in it.
        with contextlib.suppress(OSError):
            parent.rmdir()
