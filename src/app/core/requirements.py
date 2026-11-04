"""The add-ons an add-on requires (`requires.addons`, add-on contract generation 4).

papaia-ctl refuses `addon install`/`start` while a required add-on is not active or too old;
the manager shows the same verdict before anyone clicks, so the operator knows what to install
first. papaia-ctl stays the gate: anything the manager cannot read is shown as "not checked"
and does not disable a button.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from app.core import semver

ACTIVE = "active"
INACTIVE = "inactive"
MISSING = "missing"
TOO_OLD = "too_old"
UNCHECKED = "unchecked"

# States that do not block an install.
_MET = frozenset({ACTIVE, UNCHECKED})


def required_addons(manifest: Mapping[str, Any]) -> list[tuple[str, str | None]]:
    """`requires.addons` as (name, version range or None). Malformed entries are skipped:
    papaia-ctl reports them when the install runs."""
    requires = manifest.get("requires")
    entries = requires.get("addons") if isinstance(requires, Mapping) else None
    if not isinstance(entries, list):
        return []
    result: list[tuple[str, str | None]] = []
    for entry in entries:
        if isinstance(entry, str) and entry:
            result.append((entry, None))
        elif isinstance(entry, Mapping) and isinstance(entry.get("name"), str) and entry["name"]:
            version = entry.get("version")
            result.append((entry["name"], version if isinstance(version, str) else None))
    return result


def _deployed_version(entry: Mapping[str, Any], workspace_dir: str) -> str | None:
    """The `version` of an add-on's manifest at its deployment path, for add-ons installed
    without the manager. Relative paths are relative to the core checkout, as in papaia-ctl."""
    raw = entry.get("path")
    if not isinstance(raw, str) or not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = Path(workspace_dir) / "papaia" / path
    try:
        manifest = yaml.safe_load((path / "papaia-app.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    version = manifest.get("version") if isinstance(manifest, Mapping) else None
    return str(version) if version is not None else None


def requirement_states(
    manifest: Mapping[str, Any],
    deployment_addons: Mapping[str, Mapping[str, Any]],
    installed: Mapping[str, Any],
    workspace_dir: str,
) -> list[dict[str, Any]]:
    """One entry per required add-on: name, range, state and the active version.

    `deployment_addons` is deployment.yaml's add-ons by name, `installed` installed.yaml's
    records by name (their `manifest_version` is preferred over reading the manifest)."""
    states: list[dict[str, Any]] = []
    for name, version_range in required_addons(manifest):
        entry = deployment_addons.get(name)
        version: str | None = None
        if entry is None:
            state = MISSING
        elif not entry.get("active"):
            state = INACTIVE
        else:
            record = installed.get(name)
            version = getattr(record, "manifest_version", None) or _deployed_version(
                entry, workspace_dir
            )
            if version_range is None:
                state = ACTIVE
            else:
                verdict = semver.satisfies(version, version_range) if version else None
                state = UNCHECKED if verdict is None else ACTIVE if verdict else TOO_OLD
        states.append({"name": name, "range": version_range, "state": state, "version": version})
    return states


def requirements_met(states: list[dict[str, Any]]) -> bool:
    """Whether nothing in `states` would make papaia-ctl refuse the install."""
    return all(s["state"] in _MET for s in states)


def view(
    manifest: Mapping[str, Any],
    deployment_addons: Mapping[str, Mapping[str, Any]],
    installed: Mapping[str, Any],
    workspace_dir: str,
) -> dict[str, Any]:
    """The `requirements` / `requirements_met` keys of an add-on's view model."""
    states = requirement_states(manifest, deployment_addons, installed, workspace_dir)
    return {"requirements": states, "requirements_met": requirements_met(states)}
