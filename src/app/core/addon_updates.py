"""Moving an installed add-on to another version, and back.

The decisions live in `papaia-ctl addon update` and `addon rollback`; this module is what
the manager adds around them:

* a version of an add-on is a directory of its own (`snapshots.managed_snapshot_path` with a
  revision), staged from the catalog before anything is asked of papaia-ctl, so the plan the
  operator reviews and the update that follows are about the same files;
* `--dry-run --json` is how the review page learns what an update would change;
* the values a new version asks for travel to papaia-ctl in a private file, never on a
  command line, and the file does not outlive the job;
* installed.yaml follows what papaia-ctl registered. It is read back from deployment.yaml
  rather than assumed from the exit status, because an update whose containers could not be
  recreated has still moved the registration.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.core.backups import find_restore_point, resolve_backup_dir
from app.core.catalogs import Catalog, catalog_scan_path, load_registry, materialize_local_addon
from app.core.ctl import CtlError, run_addon_verb
from app.core.resolve import outdated_addons
from app.core.semver import compare
from app.core.snapshots import (
    MAX_SNAPSHOTS,
    HistoryEntry,
    InstalledAddon,
    catalog_clone_path,
    head_sha,
    load_installed,
    managed_snapshot_path,
    materialize_snapshot,
    prune_snapshots,
    record_installed,
)
from app.core.state import deployment_addons_by_name, load_deployment_yaml
from app.i18n import gettext as _

logger = logging.getLogger(__name__)

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# What marks a core that has the verbs: the function itself. A version number would be wrong
# for a checkout that carries the change before the release that names it.
_VERBS_MARKER = "cmd_addon_update()"
MIN_CORE_VERSION = "1.8.0"


class UpdateError(Exception):
    """An update cannot be staged, planned or started; `str(exc)` is for the operator."""


@dataclass(frozen=True)
class Staged:
    """A version of an add-on, written to its own directory and ready for papaia-ctl."""

    revision: str
    commit: str
    path: Path
    version: str


@dataclass(frozen=True)
class PreviousVersion:
    """What a rollback goes back to, from the `previous` entry papaia-ctl keeps."""

    version: str
    path: Path
    available: bool
    restore_point: str | None
    restore_point_available: bool


@dataclass(frozen=True)
class ChangelogSection:
    """One version's entry of an add-on's `CHANGELOG.md`."""

    version: str
    date: str
    body: str


@lru_cache(maxsize=4)
def _supports_updates(script: str, mtime_ns: int) -> bool:
    del mtime_ns  # part of the cache key: a core that is upgraded in place is read again
    try:
        return _VERBS_MARKER in Path(script).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def core_supports_updates(workspace_dir: str) -> bool:
    """Whether the papaia core in the workspace has `addon update` and `addon rollback`."""
    script = Path(workspace_dir) / "papaia" / "tools" / "lib" / "sh" / "addon.sh"
    try:
        return _supports_updates(str(script), script.stat().st_mtime_ns)
    except OSError:
        return False


def core_requirement_message() -> str:
    return _(
        "Updating and rolling back add-ons needs papAIa core %(version)s or newer."
    ) % {"version": MIN_CORE_VERSION}


def manifest_version_of(addon_dir: Path) -> str:
    try:
        raw = yaml.safe_load((addon_dir / "papaia-app.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return "unknown"
    return str(raw.get("version", "unknown")) if isinstance(raw, dict) else "unknown"


_CHANGELOG_HEADING = re.compile(r"^##\s+\[(?P<version>[^\]]+)\](?:\s+-\s+(?P<date>\S+))?\s*$")
_CHANGELOG_LIMIT = 6000


def changelog_between(addon_dir: Path, installed: str, candidate: str) -> list[ChangelogSection]:
    """The `CHANGELOG.md` sections for the versions an update brings in, newest first.

    Best effort: an add-on without the file, or one that is not a Keep a Changelog document,
    gives an empty list. A section counts when its version is newer than `installed` and not
    newer than `candidate`; a heading that is not SemVer (`[Unreleased]`) ends the section
    before it and is not shown. The text is plain text for the page and is cut at a limit."""
    try:
        lines = (addon_dir / "CHANGELOG.md").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []

    headings = [(i, m) for i, line in enumerate(lines) if (m := _CHANGELOG_HEADING.match(line))]
    wanted: list[ChangelogSection] = []
    used = 0
    for n, (start, match) in enumerate(headings):
        newer = compare(match["version"], installed)
        upto = compare(match["version"], candidate)
        if newer is None or upto is None or newer <= 0 or upto > 0:
            continue
        end = headings[n + 1][0] if n + 1 < len(headings) else len(lines)
        body = "\n".join(lines[start + 1 : end]).strip()
        if used + len(body) > _CHANGELOG_LIMIT:
            if not wanted:
                wanted.append(
                    ChangelogSection(match["version"], match["date"] or "", body[:_CHANGELOG_LIMIT])
                )
            break
        used += len(body)
        wanted.append(ChangelogSection(match["version"], match["date"] or "", body))
    return wanted


def _staging_failed(name: str, exc: Exception) -> UpdateError:
    detail = " ".join(str(exc).split())[:300]
    return UpdateError(
        _("The new version of %(name)s could not be taken from the catalog: %(detail)s") % {
            "name": name, "detail": detail,
        }
    )


async def stage_revision(
    *,
    catalog: Catalog,
    workspace_dir: str,
    name: str,
    revision: str | None = None,
) -> Staged:
    """Write the catalog's current version of `name` to a directory of its own.

    A directory that exists is not touched: its name is the commit, so its content is
    the same, and the version the add-on runs from may be it. For a local catalog, which is a
    working directory and has no commit, every staging is a new directory.

    `revision` is what the operator reviewed. If the catalog has moved on since, this refuses
    rather than update to something that was not shown."""
    source = catalog_scan_path(catalog, workspace_dir) / name
    if catalog.type == "local":
        if revision:
            path = managed_snapshot_path(workspace_dir, catalog.name, name, revision)
            if (path / "papaia-app.yaml").is_file():
                return Staged(revision, "local", path, manifest_version_of(path))
            raise UpdateError(_("The version that was reviewed is gone. Review the update again."))
        if not (source / "papaia-app.yaml").is_file():
            raise UpdateError(
                _("%(name)s is no longer in catalog %(catalog)s") % {
                    "name": name, "catalog": catalog.name,
                }
            )
        revision = "local-" + datetime.now(tz=UTC).strftime("%Y%m%d%H%M%S")
        path = managed_snapshot_path(workspace_dir, catalog.name, name, revision)
        try:
            await materialize_local_addon(source, path)
        except OSError as exc:
            raise _staging_failed(name, exc) from exc
        return Staged(revision, "local", path, manifest_version_of(path))

    clone = catalog_clone_path(workspace_dir, catalog.name)
    sha = await head_sha(clone)
    if not _SHA_RE.match(sha):
        raise UpdateError(_("The catalog %(catalog)s has no commit to take a version from.") % {
            "catalog": catalog.name,
        })
    current = sha[:12]
    if revision and revision != current:
        raise UpdateError(
            _("The catalog changed since this update was reviewed. Review the update again.")
        )
    path = managed_snapshot_path(workspace_dir, catalog.name, name, current)
    if not (path / "papaia-app.yaml").is_file():
        try:
            await materialize_snapshot(catalog_clone=clone, addon_subdir=name, dest=path)
        except (RuntimeError, OSError) as exc:
            raise _staging_failed(name, exc) from exc
        if not (path / "papaia-app.yaml").is_file():
            raise UpdateError(
                _("%(name)s is not in catalog %(catalog)s at revision %(revision)s") % {
                    "name": name, "catalog": catalog.name, "revision": current,
                }
            )
    return Staged(current, sha, path, manifest_version_of(path))


def parse_plan_output(lines: list[str]) -> dict[str, Any] | None:
    """The plan JSON in the output of `addon update --dry-run --json`, or None.

    stderr is merged into the stream, so the document is looked for rather than assumed to
    be all of it: it starts at the first line that is a lone `{`."""
    try:
        start = lines.index("{")
    except ValueError:
        return None
    try:
        document, _end = json.JSONDecoder().raw_decode("\n".join(lines[start:]))
    except ValueError:
        return None
    return document if isinstance(document, dict) else None


def _tail(lines: list[str], limit: int = 4) -> str:
    kept = [line.strip() for line in lines if line.strip()]
    return " ".join(kept[-limit:])


async def fetch_plan(
    *,
    name: str,
    candidate: Path,
    workspace_dir: str,
    config_dir: str,
    force: bool = False,
) -> dict[str, Any]:
    """What `addon update` would do with `candidate`, as papaia-ctl reports it.

    A candidate the compatibility gate refuses still yields the plan, with `blocked` set.
    Anything that stops papaia-ctl before it has a plan (the add-on is not registered, the
    directory has another name) raises `UpdateError` with its message."""
    flags = ["--dry-run", "--json", f"--path={candidate}"]
    if force:
        flags.append("--force")
    lines: list[str] = []
    failure: CtlError | None = None
    try:
        stream = await run_addon_verb(
            verb="update",
            name=name,
            workspace_dir=workspace_dir,
            config_dir=config_dir,
            extra_flags=flags,
        )
        async for line in stream:
            lines.append(line)
    except CtlError as exc:
        failure = exc
    plan = parse_plan_output(lines)
    if plan is None:
        raise UpdateError(_tail(lines) or (str(failure) if failure else _("no plan was produced")))
    return plan


def answerable_keys(plan: Mapping[str, Any]) -> set[str]:
    """The keys an update may be given a value for: those the plan itself asks about."""
    return {
        str(item["key"])
        for item in (plan.get("env") or {}).get("new", [])
        if item.get("class") in ("change_me", "replace_secret")
    }


def write_answers_file(answers: Mapping[str, str]) -> Path:
    """The values for an update, as KEY=VALUE lines in a file only this process can read."""
    for key, value in answers.items():
        if "\n" in value or "\r" in value or "\n" in key:
            raise UpdateError(_("The value for %(key)s must be a single line.") % {"key": key})
    fd, raw = tempfile.mkstemp(prefix="papaia-answers-")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        for key, value in answers.items():
            handle.write(f"{key}={value}\n")
    return Path(raw)


def deployment_entry(config_dir: str, name: str) -> dict[str, Any] | None:
    return deployment_addons_by_name(load_deployment_yaml(config_dir)).get(name)


def _registered(workspace_dir: str, entry: Mapping[str, Any] | None) -> Path | None:
    raw = str((entry or {}).get("path") or "")
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else Path(workspace_dir) / path


def previous_version(
    config_dir: str, workspace_dir: str, entry: Mapping[str, Any] | None
) -> PreviousVersion | None:
    """The version a rollback would go back to, or None when there is nothing to go back to."""
    previous = (entry or {}).get("previous")
    if not isinstance(previous, dict) or not previous.get("path"):
        return None
    path = Path(str(previous["path"]))
    if not path.is_absolute():
        path = Path(workspace_dir) / path
    point = str(previous.get("restore_point") or "") or None
    available = False
    if point:
        available = find_restore_point(resolve_backup_dir(config_dir), point) is not None
    return PreviousVersion(
        version=str(previous.get("version") or ""),
        path=path,
        available=path.is_dir(),
        restore_point=point,
        restore_point_available=available,
    )


def next_history(inst: InstalledAddon | None, new_commit: str | None = None) -> list[HistoryEntry]:
    """The history after the version in `inst` is replaced by `new_commit`.

    Installing the commit that is already recorded (reactivating an add-on) adds nothing."""
    if inst is None:
        return []
    if new_commit is not None and inst.commit == new_commit:
        return list(inst.history)
    older = HistoryEntry(inst.commit, inst.manifest_version, inst.installed_at)
    return [older, *inst.history][:MAX_SNAPSHOTS]


def version_state(
    config_dir: str,
    workspace_dir: str,
    inst: InstalledAddon | None,
    entry: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """What an add-on's detail page needs to offer an update or a rollback."""
    supported = core_supports_updates(workspace_dir)
    previous = previous_version(config_dir, workspace_dir, entry) if supported else None
    return {
        "updates_supported": supported,
        "core_requirement": "" if supported else core_requirement_message(),
        "previous": None
        if previous is None
        else {
            "version": previous.version,
            "available": previous.available,
            "restore_point": previous.restore_point,
            "restore_point_available": previous.restore_point_available,
        },
        "history": [
            {
                "version": h.manifest_version,
                "commit": h.commit[:12],
                "installed_at": h.installed_at.isoformat(),
            }
            for h in (inst.history if inst else [])
        ],
    }


def sync_installed_after_update(
    config_dir: str,
    workspace_dir: str,
    *,
    name: str,
    inst: InstalledAddon,
    staged: Staged,
) -> bool:
    """Record the new version in installed.yaml if papaia-ctl registered it.

    Returns whether it did. The exit status of `addon update` is not what decides this: when
    the containers cannot be recreated, the registration has moved all the same, and the manager
    would otherwise keep showing the old version for an add-on that is running the new one."""
    registered = _registered(workspace_dir, deployment_entry(config_dir, name))
    if registered is None or not _same_dir(registered, staged.path):
        return False
    record_installed(
        config_dir,
        name=name,
        catalog=inst.catalog,
        commit=staged.commit,
        manifest_version=staged.version,
        history=next_history(inst, staged.commit),
    )
    return True


def sync_installed_after_rollback(
    config_dir: str,
    workspace_dir: str,
    *,
    name: str,
    inst: InstalledAddon,
    was: Path,
) -> bool:
    """Make installed.yaml say what papaia-ctl registered after a rollback.

    The version is read from the directory it points at; the commit comes from the history
    when an entry for that version is in it."""
    path = _registered(workspace_dir, deployment_entry(config_dir, name))
    if path is None or _same_dir(path, was):
        return False
    version = manifest_version_of(path)
    remaining = list(inst.history)
    match = next((h for h in remaining if h.manifest_version == version), None)
    if match is not None:
        remaining.remove(match)
    record_installed(
        config_dir,
        name=name,
        catalog=inst.catalog,
        commit=match.commit if match else "unknown",
        manifest_version=version,
        history=remaining,
    )
    return True


def prune_after(
    config_dir: str, workspace_dir: str, *, name: str, catalog: str, extra: Path | None = None
) -> list[Path]:
    """Delete the snapshots of `name` that nothing needs any more.

    What stays is read from deployment.yaml at this moment: the registered directory, the one a
    rollback goes back to, and `extra` (a version staged for a review)."""
    entry = deployment_entry(config_dir, name) or {}
    keep: list[Path] = []
    for holder in (entry, entry.get("previous")):
        registered = _registered(workspace_dir, holder if isinstance(holder, dict) else None)
        if registered is not None:
            keep.append(registered)
    if extra is not None:
        keep.append(extra)
    return prune_snapshots(workspace_dir, catalog, name, keep=keep)


# The nav dot asks on every page of every open tab. Reading the catalogs is cheap but not free, and
# an answer that is a minute old is as good as a fresh one: a catalog changes when somebody
# refreshes it, and that (and an update) clears this.
_OUTDATED_TTL = 60.0
_outdated_cache: dict[str, tuple[float, int]] = {}


def outdated_count(config_dir: str, workspace_dir: str) -> int:
    """How many installed add-ons the catalogs on disk offer a higher version of."""
    key = f"{config_dir}|{workspace_dir}"
    cached = _outdated_cache.get(key)
    now = time.monotonic()
    if cached is not None and now - cached[0] < _OUTDATED_TTL:
        return cached[1]
    count = len(
        outdated_addons(load_registry(config_dir), workspace_dir, load_installed(config_dir))
    )
    _outdated_cache[key] = (now, count)
    return count


def invalidate_outdated_count() -> None:
    _outdated_cache.clear()


def _same_dir(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return a == b
