"""Merged addon status: catalog × deployment.yaml × live Docker containers."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from app.core.snapshots import InstalledAddon


class AddonStatus(StrEnum):
    AVAILABLE = "available"
    INSTALLED = "installed"
    RUNNING = "running"
    INACTIVE = "inactive"
    UNMANAGED = "unmanaged"


@dataclass
class AddonInfo:
    name: str
    status: AddonStatus
    description: str = ""
    catalog: str | None = None
    installed_version: str | None = None
    catalog_version: str | None = None
    update_available: bool = False
    managed: bool = True
    deployment_path: str | None = None


def compute_status(
    *,
    name: str,
    deployment_entry: dict[str, Any] | None,
    installed: InstalledAddon | None,
    catalog_version: str | None,
    running_projects: set[str],
    workspace_dir: str,
) -> AddonStatus:
    """Return the merged status for one addon.

    ``running_projects`` used to come from a second, independent ``docker ps``
    in this module. It now arrives as ``services.StackSnapshot.running_projects``
    -- one reading of the host, shared with the services page, which is what
    keeps the two surfaces from disagreeing about whether an addon is up.
    """
    if deployment_entry is None:
        return AddonStatus.AVAILABLE

    active = bool(deployment_entry.get("active", False))
    if not active:
        return AddonStatus.INACTIVE

    deploy_path = str(deployment_entry.get("path", ""))
    managed_prefix = str(Path(workspace_dir) / "addons" / "_managed")
    if installed and not deploy_path.startswith(managed_prefix):
        return AddonStatus.UNMANAGED

    compose_project = Path(deploy_path).name if deploy_path else name
    if compose_project in running_projects:
        return AddonStatus.RUNNING

    return AddonStatus.INSTALLED


def load_deployment_yaml(config_dir: str) -> dict[str, Any]:
    """Load deployment.yaml (read-only; owned by lib/deployment.py)."""
    path = Path(config_dir) / "deployment.yaml"
    if not path.exists():
        return {}
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return raw


def deployment_addons_by_name(deployment: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Deployment ``addons`` entries indexed by name.

    ``deployment["addons"]`` is a list of entry dicts (see
    ``lib/deployment.py``'s ``active_addons()``), not a mapping — index it
    here once so callers can do ``.get(name)`` / ``.items()`` as if it were.
    """
    return {
        entry["name"]: entry
        for entry in (deployment.get("addons") or [])
        if entry.get("name")
    }
