"""Maintenance actions an add-on declares (`actions`, add-on contract generation 4).

papaia-ctl runs one with `papaia-ctl addon run <addon> <action>`: a service of the add-on's own
compose file with fixed arguments. The manager offers the declared actions as buttons and only
ever passes an action name; it never builds a command. A malformed block yields no actions
here -- papaia-ctl reports it.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

ACTION_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


def addon_actions(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The declared actions as [{name, description, destructive}], sorted by name."""
    block = manifest.get("actions")
    if not isinstance(block, Mapping):
        return []
    actions: list[dict[str, Any]] = []
    for name, spec in block.items():
        if not isinstance(name, str) or not ACTION_NAME_RE.match(name):
            continue
        if not isinstance(spec, Mapping) or not isinstance(spec.get("service"), str):
            continue
        description = spec.get("description")
        actions.append(
            {
                "name": name,
                "description": description if isinstance(description, str) else "",
                "destructive": spec.get("destructive") is True,
            }
        )
    return sorted(actions, key=lambda a: str(a["name"]))


def actions_at(addon_dir: Path | None) -> list[dict[str, Any]]:
    """The actions of the manifest in `addon_dir`; [] when there is none to read."""
    if addon_dir is None:
        return []
    try:
        manifest = yaml.safe_load((addon_dir / "papaia-app.yaml").read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return []
    return addon_actions(manifest) if isinstance(manifest, Mapping) else []
