"""The software an add-on bundles, as its manifest declares it.

An add-on's `version` counts changes to the bundle; it does not say that
`paperless` runs Paperless-ngx 3.0.5. The catalog states that in a `components`
list of the manifest, one entry per image, and this module turns it into what
the add-on pages show:

* the catalog's version of each component next to the one the installed
  snapshot carries, so an update reads "3.0.5 -> 3.1.0" instead of a bare
  "update available";
* the tag an operator pinned by hand in the add-on's `.env` (`image_var`), which
  replaces the catalog default without any trace in the manifest.

The manifest may come from a catalog that is not Fidonis's, so nothing here
trusts its shape: an entry that is not usable is skipped, text is cut to a
length a card can show, and only an `https://` source becomes a link. A catalog
without `components` simply has nothing to show.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.core.envfile import load_env_file
from app.core.snapshots import InstalledAddon, installed_addon_dir

logger = logging.getLogger(__name__)

ROLES = ("primary", "sidecar")

# A catalog lists a handful of images per add-on; the cap keeps a hostile one
# from building a page out of its manifest.
_MAX_COMPONENTS = 32
_MAX_TEXT = 80
_MAX_REF = 200

_VAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SOURCE_RE = re.compile(r'^https://[^\s"<>]{1,200}$')


@dataclass(frozen=True)
class Component:
    id: str
    name: str
    role: str
    kind: str
    version: str
    image_var: str | None
    source: str | None
    floating: bool


def image_tag(ref: str) -> str:
    """Tag of an image reference (`registry/name:tag[@digest]`); `latest` without one."""
    last = ref.split("@", 1)[0].rsplit("/", 1)[-1]
    return last.split(":", 1)[1] if ":" in last else "latest"


def _text(value: object, limit: int = _MAX_TEXT) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned[:limit] or None


def parse_components(manifest: Mapping[str, Any] | None) -> list[Component]:
    """The usable `components` entries of a manifest, in manifest order."""
    declared = (manifest or {}).get("components")
    if not isinstance(declared, list):
        return []
    result: list[Component] = []
    seen: set[str] = set()
    for entry in declared[:_MAX_COMPONENTS]:
        if not isinstance(entry, dict):
            continue
        ident = _text(entry.get("id"))
        name = _text(entry.get("name"))
        version = _text(entry.get("version"))
        if ident is None or name is None or version is None or ident in seen:
            continue
        seen.add(ident)
        role = _text(entry.get("role"))
        var = entry.get("image_var")
        source = entry.get("source")
        result.append(
            Component(
                id=ident,
                name=name,
                # An unknown role must not promote an image to the card.
                role=role if role in ROLES else "sidecar",
                kind=_text(entry.get("kind")) or "upstream",
                version=version,
                image_var=var if isinstance(var, str) and _VAR_RE.match(var) else None,
                source=source if isinstance(source, str) and _SOURCE_RE.match(source) else None,
                floating=entry.get("pinned") is False,
            )
        )
    return result


def read_manifest(addon_dir: Path | None) -> dict[str, Any] | None:
    """`papaia-app.yaml` of an add-on directory, or None when it cannot be read."""
    if addon_dir is None:
        return None
    path = addon_dir / "papaia-app.yaml"
    if not path.is_file():
        return None
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("cannot read the manifest %s: %s", path, exc)
        return None
    return loaded if isinstance(loaded, dict) else None


def component_views(
    catalog_manifest: Mapping[str, Any] | None,
    installed_manifest: Mapping[str, Any] | None,
    bundle_env: Mapping[str, str] | None,
) -> list[dict[str, Any]]:
    """Components as the add-on pages show them.

    `catalog_manifest` decides what is listed. An add-on the catalogs no longer
    offer is listed from its installed manifest instead, so it does not lose its
    components. `installed_version` is set only for an installed add-on whose
    snapshot carries the same component; `changed` then says the catalog differs.

    `override_version` is the tag `bundle_env` sets for the component's image
    variable when it differs from the version in use. A different registry with
    the same tag is not told apart, because the manifest records no repository.
    """
    listed = parse_components(catalog_manifest)
    installed = {c.id: c for c in parse_components(installed_manifest)}
    if not listed:
        listed = list(installed.values())
    views: list[dict[str, Any]] = []
    for component in listed:
        before = installed.get(component.id)
        installed_version = before.version if before else None
        in_use = installed_version or component.version
        override_ref = None
        if component.image_var and bundle_env:
            ref = bundle_env.get(component.image_var, "").strip()
            if ref and image_tag(ref) != in_use:
                override_ref = ref[:_MAX_REF]
        views.append(
            {
                "id": component.id,
                "name": component.name,
                "role": component.role,
                "kind": component.kind,
                "version": component.version,
                "source": component.source,
                "image_var": component.image_var,
                "floating": component.floating,
                "installed_version": installed_version,
                "changed": installed_version is not None and installed_version != component.version,
                "override_version": image_tag(override_ref) if override_ref else None,
                "override_ref": override_ref,
            }
        )
    return views


def addon_components(
    catalog_manifest: Mapping[str, Any] | None,
    installed_dir: Path | None,
    bundle_env_file: Path | None,
) -> list[dict[str, Any]]:
    """`component_views` for an add-on, reading the installed side from disk.

    `installed_dir` is the add-on's managed snapshot and `bundle_env_file` its
    `.env` in the config directory; both are None for an add-on that is not
    installed, which then has neither an installed version nor an override.
    """
    installed_manifest = read_manifest(installed_dir)
    env = load_env_file(bundle_env_file) if bundle_env_file is not None else None
    return component_views(catalog_manifest, installed_manifest, env)


def components_for(
    config_dir: str,
    workspace_dir: str,
    name: str,
    catalog_manifest: Mapping[str, Any] | None,
    installed: InstalledAddon | None,
    deployment_entry: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """The components of one add-on as its gallery card and detail page show them.

    `deployment_entry` is the add-on's entry in deployment.yaml: the installed side is read from
    the directory it is registered at, which an update moves."""
    if installed is None:
        return addon_components(catalog_manifest, None, None)
    return addon_components(
        catalog_manifest,
        installed_addon_dir(deployment_entry, workspace_dir, installed.catalog, name),
        Path(config_dir) / "addons" / name / ".env",
    )
