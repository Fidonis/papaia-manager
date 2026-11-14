"""The add-ons an add-on requires (`requires.addons`, add-on contract generation 4).

papaia-ctl refuses `addon install`/`start` while a required add-on is not active or too old;
the manager shows the same verdict before anyone clicks, so the operator knows what to install
first. papaia-ctl stays the gate: anything the manager cannot read is shown as "not checked"
and does not disable a button.
"""
from __future__ import annotations

import os
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

# papaia-ctl's compat modes and their synonyms (`compat.resolve_mode` in the core). In warn
# mode papaia-ctl installs despite an unmet requirement and only warns.
ENFORCE = "enforce"
_ENFORCE = ENFORCE
_WARN = "warn"
_MODE_SYNONYMS = {
    "enforce": _ENFORCE,
    "production": _ENFORCE,
    "prod": _ENFORCE,
    "warn": _WARN,
    "dev": _WARN,
    "development": _WARN,
}


def compat_mode(deployment: Mapping[str, Any] | None) -> str:
    """papaia-ctl's gate policy: `PAPAIA_COMPAT_MODE`, then deployment.yaml's `mode:`, then
    enforce. Unrecognised values fall through to the next source, as in the core."""
    for candidate in (os.environ.get("PAPAIA_COMPAT_MODE"), (deployment or {}).get("mode")):
        if isinstance(candidate, str) and candidate.lower() in _MODE_SYNONYMS:
            return _MODE_SYNONYMS[candidate.lower()]
    return _ENFORCE


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


def _registered_manifest(entry: Mapping[str, Any], workspace_dir: str) -> Mapping[str, Any] | None:
    """The manifest at the add-on's registered path, None when it cannot be read. Relative
    paths are relative to the core checkout, as in papaia-ctl."""
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
    return manifest if isinstance(manifest, Mapping) else None


def _deployed_version(entry: Mapping[str, Any], workspace_dir: str) -> str | None:
    """The `version` of the manifest at the add-on's registered path, falling back to the
    `version` deployment.yaml records, the order papaia-ctl reads them in."""
    recorded = entry.get("version")
    fallback = str(recorded) if recorded is not None else None
    manifest = _registered_manifest(entry, workspace_dir)
    version = manifest.get("version") if manifest is not None else None
    return str(version) if version else fallback


def dependents(
    name: str, deployment_addons: Mapping[str, Mapping[str, Any]], workspace_dir: str
) -> list[str]:
    """The other active add-ons whose `requires.addons` names `name`, as papaia-ctl's
    `dependent_addons` finds them: papaia-ctl refuses `remove` and `uninstall` of `name` while
    one is active. A manifest that cannot be read, or whose field papaia-ctl would reject,
    counts as requiring nothing."""
    found: list[str] = []
    for other, entry in deployment_addons.items():
        if other == name or not entry.get("active"):
            continue
        manifest = _registered_manifest(entry, workspace_dir) or {}
        requires = manifest.get("requires")
        entries = requires.get("addons") if isinstance(requires, Mapping) else None
        if not isinstance(entries, list) or not all(_well_formed(e) for e in entries):
            continue
        if any(required == name for required, _ in required_addons(manifest)):
            found.append(other)
    return sorted(found)


def _well_formed(entry: object) -> bool:
    """Whether papaia-ctl accepts one `requires.addons` entry: a name, or a mapping with a
    name and an optional range it can parse. It rejects the whole field otherwise."""
    if isinstance(entry, str):
        return bool(entry)
    if not isinstance(entry, Mapping) or not isinstance(entry.get("name"), str):
        return False
    version = entry.get("version")
    if version is None:
        return bool(entry["name"])
    # Any valid version will do: only whether the range can be read matters here.
    return bool(entry["name"]) and isinstance(version, str) and (
        semver.satisfies("0.0.0", version) is not None
    )


def requirement_states(
    manifest: Mapping[str, Any],
    deployment_addons: Mapping[str, Mapping[str, Any]],
    installed: Mapping[str, Any],
    workspace_dir: str,
) -> list[dict[str, Any]]:
    """One entry per required add-on: name, range, state and the active version.

    `deployment_addons` is deployment.yaml's add-ons by name, `installed` installed.yaml's
    records by name. The registration is the truth: installed.yaml's `manifest_version` is
    only read when neither the registered manifest nor deployment.yaml states a version,
    since an `addon upgrade` or `rollback` on the host leaves installed.yaml behind."""
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
            version = _deployed_version(entry, workspace_dir) or getattr(
                record, "manifest_version", None
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
    deployment: Mapping[str, Any] | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    """The `requirements`, `requirements_met`, `requirements_enforced` and `required_by`
    keys of an add-on's view model. `requirements_enforced` is False in warn mode, where
    papaia-ctl installs despite an unmet requirement and removes a required add-on anyway:
    the badges still show, the buttons stay usable. `required_by` lists the active add-ons
    that require this one (`name`, or the manifest's name)."""
    states = requirement_states(manifest, deployment_addons, installed, workspace_dir)
    own = name or manifest.get("name")
    return {
        "requirements": states,
        "requirements_met": requirements_met(states),
        "requirements_enforced": compat_mode(deployment) == _ENFORCE,
        "required_by": (
            dependents(own, deployment_addons, workspace_dir) if isinstance(own, str) else []
        ),
    }
