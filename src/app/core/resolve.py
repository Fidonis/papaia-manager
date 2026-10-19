"""Cross-catalog add-on resolution: dedup by name+version, surface shadowing.

Two or more enabled catalogs may ship an add-on subdirectory with the same
name (the add-on's identity is the directory name, see
``core.catalogs.scan_catalog_addons``). This module groups those hits per
name: identical manifest versions collapse into a single entry with a
``shadowed_by`` list, while differing versions are kept as separate entries
so neither is silently dropped.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.catalogs import Catalog, CatalogRegistry, catalog_scan_path, scan_catalog_addons
from app.core.snapshots import InstalledAddon

logger = logging.getLogger(__name__)

_UNKNOWN_VERSION = "unknown"


@dataclass(frozen=True)
class ResolvedAddon:
    """One dashboard-visible entry for a given add-on name.

    ``key`` is ``name`` for the primary entry and ``name@<catalog>`` for any
    additional version found in another catalog, so routes can address a
    specific variant without disturbing the name-keyed installed/deployment
    state.
    """

    key: str
    name: str
    catalog: str
    manifest: dict[str, Any]
    clone: Path
    shadowed_by: list[str] = field(default_factory=list)
    is_variant: bool = False


def update_available(installed: InstalledAddon | None, catalog_version: object) -> bool:
    """Whether the catalog offers a version other than the installed one.

    The rule behind the "update" marker of the add-ons page, and behind the notification
    about it, so the two cannot disagree. Only an add-on the manager installed and manages
    counts, and only against a catalog that states a version; "other than" rather than
    "newer than" because a manifest version is not required to be a semantic version.
    """
    return (
        installed is not None
        and installed.managed
        and catalog_version is not None
        and installed.manifest_version != catalog_version
    )


@dataclass(frozen=True)
class AddonUpdate:
    name: str
    installed: str
    available: str
    catalog: str


def outdated_addons(
    registry: CatalogRegistry,
    workspace_dir: str,
    installed_map: dict[str, InstalledAddon],
) -> list[AddonUpdate]:
    """The installed add-ons for which a catalog on disk offers another version.

    Reads the catalogs as they are: they are refreshed by hand (a catalog refresh), not by
    this, so an update shows up here after the refresh that fetched it."""
    found: list[AddonUpdate] = []
    for resolved in resolve_catalog_addons(registry, workspace_dir, installed_map):
        installed = installed_map.get(resolved.name)
        # A variant is the same add-on from another catalog; it says something about the
        # installed one only when that is where the add-on came from.
        if installed is None or (resolved.is_variant and installed.catalog != resolved.catalog):
            continue
        version = resolved.manifest.get("version")
        if update_available(installed, version):
            found.append(
                AddonUpdate(
                    name=resolved.name,
                    installed=installed.manifest_version,
                    available=str(version),
                    catalog=resolved.catalog,
                )
            )
    return found


def resolve_catalog_addons(
    registry: CatalogRegistry,
    workspace_dir: str,
    installed_map: dict[str, InstalledAddon],
) -> list[ResolvedAddon]:
    """Scan every enabled catalog and group hits by add-on name + version.

    Registry order determines both the default (no-installed-record)
    representative and the ordering of extra version groups. If the add-on
    is installed, the group matching ``installed_map[name].catalog`` is
    promoted to the primary entry regardless of registry order.
    """
    # name -> version -> list of (catalog, manifest, clone_path), in scan order
    hits: dict[str, dict[str, list[tuple[Catalog, dict[str, Any], Path]]]] = {}

    for catalog in registry.catalogs:
        if not catalog.enabled:
            continue
        clone = catalog_scan_path(catalog, workspace_dir)
        for addon_name, manifest in scan_catalog_addons(clone):
            version = str(manifest.get("version") or _UNKNOWN_VERSION)
            hits.setdefault(addon_name, {}).setdefault(version, []).append(
                (catalog, manifest, clone)
            )

    results: list[ResolvedAddon] = []
    for addon_name, by_version in hits.items():
        installed = installed_map.get(addon_name)
        groups = list(by_version.items())

        if len(groups) > 1:
            all_catalogs = [c.name for entries in by_version.values() for c, _m, _clone in entries]
            logger.info(
                "addon %r: %d distinct version(s) across catalogs %s",
                addon_name,
                len(groups),
                all_catalogs,
            )

        primary_index = 0
        if installed is not None:
            for idx, (_version, entries) in enumerate(groups):
                if any(c.name == installed.catalog for c, _m, _clone in entries):
                    primary_index = idx
                    break

        # Primary group (installed catalog, or the precedence winner) first,
        # remaining version groups follow in registry-encounter order.
        ordered = [groups[primary_index], *groups[:primary_index], *groups[primary_index + 1 :]]

        for idx, (_version, entries) in enumerate(ordered):
            first_catalog, manifest, clone = entries[0]
            is_variant = idx != 0
            key = addon_name if not is_variant else f"{addon_name}@{first_catalog.name}"
            results.append(
                ResolvedAddon(
                    key=key,
                    name=addon_name,
                    catalog=first_catalog.name,
                    manifest=manifest,
                    clone=clone,
                    shadowed_by=[c.name for c, _m, _clone in entries[1:]],
                    is_variant=is_variant,
                )
            )

    return results
