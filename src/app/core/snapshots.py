"""Addon snapshot materialization (git-archive) and installed.yaml bookkeeping."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


@dataclass
class InstalledAddon:
    catalog: str
    commit: str
    manifest_version: str
    installed_at: datetime
    managed: bool = True


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
            )
        except (KeyError, ValueError) as exc:
            logger.warning("skipping malformed installed.yaml entry %r: %s", name, exc)
    return result


def record_installed(
    config_dir: str,
    *,
    name: str,
    catalog: str,
    commit: str,
    manifest_version: str,
    managed: bool = True,
) -> None:
    """Write or update one entry in installed.yaml (atomic replace)."""
    path = _installed_path(config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    raw: dict[str, Any] = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw.setdefault("version", 1)
    raw.setdefault("addons", {})
    raw["addons"][name] = {
        "catalog": catalog,
        "commit": commit,
        "manifest_version": manifest_version,
        "installed_at": datetime.now(tz=UTC).isoformat(),
        "managed": managed,
    }
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


def managed_snapshot_path(workspace_dir: str, catalog_name: str, addon_name: str) -> Path:
    return Path(workspace_dir) / "addons" / "_managed" / catalog_name / addon_name
