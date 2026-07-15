"""Catalog registry: catalogs.yaml CRUD and git clone/fetch operations."""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

logger = logging.getLogger(__name__)

_CATALOG_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_REF_RE = re.compile(r"^[A-Za-z0-9._/\-]{1,100}$")
_HTTPS_RE = re.compile(r"^https://")

# Default Fidonis catalog seeded on first startup when no catalogs.yaml exists.
_DEFAULT_CATALOG: dict[str, Any] = {
    "name": "fidonis",
    "type": "git",
    "url": "https://github.com/Fidonis/papaia-addons.git",
    "ref": "main",
    "enabled": True,
}


@dataclass
class CatalogAuth:
    token_env: str
    username: str = "x-access-token"


@dataclass
class Catalog:
    name: str
    type: Literal["git", "local"]
    enabled: bool = True
    # git catalogs
    url: str | None = None
    ref: str = "main"
    auth: CatalogAuth | None = None
    # local catalogs
    path: str | None = None


@dataclass
class CatalogRegistry:
    version: int = 1
    catalogs: list[Catalog] = field(default_factory=list)


def load_registry(config_dir: str) -> CatalogRegistry:
    """Load catalogs.yaml; seed the default Fidonis catalog on first run."""
    registry_path = _registry_path(config_dir)
    if not registry_path.exists():
        logger.info("catalogs.yaml not found; seeding default fidonis catalog")
        registry = CatalogRegistry(catalogs=[_catalog_from_dict(_DEFAULT_CATALOG)])
        save_registry(config_dir, registry)
        return registry

    raw = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
    catalogs = [_catalog_from_dict(c) for c in raw.get("catalogs", [])]
    return CatalogRegistry(version=int(raw.get("version", 1)), catalogs=catalogs)


def save_registry(config_dir: str, registry: CatalogRegistry) -> None:
    """Atomically write catalogs.yaml."""
    registry_path = _registry_path(config_dir)
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = registry_path.with_suffix(".yaml.tmp")
    data = _registry_to_dict(registry)
    tmp.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8")
    tmp.replace(registry_path)


def validate_catalog_name(name: str) -> None:
    if not _CATALOG_NAME_RE.match(name):
        raise ValueError(
            f"catalog name {name!r} must match ^[a-z0-9][a-z0-9-]{{0,31}}$"
        )


def validate_catalog_url(url: str) -> None:
    if not _HTTPS_RE.match(url):
        raise ValueError(f"catalog URL must start with https://; got {url!r}")


def validate_ref(ref: str) -> None:
    if not _REF_RE.match(ref):
        raise ValueError(f"catalog ref {ref!r} contains invalid characters")


def validate_local_path(path: str, workspace_dir: str) -> None:
    resolved = Path(path).resolve()
    workspace = Path(workspace_dir).resolve()
    if not str(resolved).startswith(str(workspace)):
        raise ValueError(
            f"local catalog path must reside under PAPAIA_WORKSPACE_DIR "
            f"({workspace}); got {resolved}"
        )


def scan_catalog_addons(clone_path: Path) -> list[tuple[str, dict[str, Any]]]:
    """Return (addon_name, manifest) for every addon found in a catalog clone dir."""
    results: list[tuple[str, dict[str, Any]]] = []
    if not clone_path.exists() or not clone_path.is_dir():
        return results
    for entry in sorted(clone_path.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        manifest_file = entry / "papaia-app.yaml"
        if not manifest_file.exists():
            continue
        try:
            manifest: dict[str, Any] = yaml.safe_load(manifest_file.read_text(encoding="utf-8")) or {}
            results.append((entry.name, manifest))
        except Exception as exc:  # noqa: BLE001
            logger.warning("skipping addon %r in %s: %s", entry.name, clone_path, exc)
    return results


async def clone_catalog_clone(catalog: Catalog, workspace_dir: str) -> list[str]:
    """Shallow-clone a git catalog into the workspace browse area."""
    dest = Path(workspace_dir) / "addons" / "_catalogs" / catalog.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "git", "clone", "--depth", "1",
        "--branch", catalog.ref,
        catalog.url or "",
        str(dest),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    lines = out.decode(errors="replace").splitlines()
    if proc.returncode != 0:
        raise RuntimeError(f"git clone failed ({proc.returncode}): " + "\n".join(lines))
    return lines


async def refresh_catalog_clone(catalog: Catalog, workspace_dir: str) -> list[str]:
    """Fetch + reset the catalog clone; clones from scratch if not present."""
    dest = Path(workspace_dir) / "addons" / "_catalogs" / catalog.name
    if not dest.exists():
        return await clone_catalog_clone(catalog, workspace_dir)
    lines: list[str] = []
    for cmd in (
        ["git", "-C", str(dest), "fetch", "--depth", "1", "origin", catalog.ref],
        ["git", "-C", str(dest), "reset", "--hard", "FETCH_HEAD"],
    ):
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        lines.extend(out.decode(errors="replace").splitlines())
        if proc.returncode != 0:
            raise RuntimeError(f"{cmd[2]} failed ({proc.returncode}): " + "\n".join(lines))
    return lines


def _registry_path(config_dir: str) -> Path:
    return Path(config_dir) / "manager" / "catalogs.yaml"


def _catalog_from_dict(d: dict[str, Any]) -> Catalog:
    auth_raw = d.get("auth")
    auth = (
        CatalogAuth(
            token_env=str(auth_raw["token_env"]),
            username=str(auth_raw.get("username", "x-access-token")),
        )
        if auth_raw
        else None
    )
    return Catalog(
        name=str(d["name"]),
        type=d.get("type", "git"),
        enabled=bool(d.get("enabled", True)),
        url=d.get("url"),
        ref=str(d.get("ref", "main")),
        auth=auth,
        path=d.get("path"),
    )


def _registry_to_dict(registry: CatalogRegistry) -> dict[str, Any]:
    catalogs = []
    for c in registry.catalogs:
        entry: dict[str, Any] = {"name": c.name, "type": c.type, "enabled": c.enabled}
        if c.type == "git":
            entry["url"] = c.url
            entry["ref"] = c.ref
            if c.auth:
                entry["auth"] = {"token_env": c.auth.token_env, "username": c.auth.username}
        else:
            entry["path"] = c.path
        catalogs.append(entry)
    return {"version": registry.version, "catalogs": catalogs}
