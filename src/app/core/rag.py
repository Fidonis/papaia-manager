"""The optional RAG system of the core, as far as the manager shows it.

The core's `rag` Compose profile starts Qdrant, an MCP server in front of it and
the ingest service. The manager learns about it from the core `.env`: whether `rag`
is in `COMPOSE_PROFILES`, and the two browser URLs the core stores next to it. The
Collections page additionally reads the RAG module's own `.env` (see `rag_backend`).

It deliberately does not test whether the URL keys exist. The core derives and keeps
them while the profile is off, so their presence says nothing about whether the
system is installed. A core without the profile (1.4.0 and older) yields nothing,
which keeps the manager independent of the core release.

Three surfaces use this module:

* the dashboard, through `with_rag_tiles`, which adds a computed tile group at
  render time. Nothing is written to `tiles.yaml`: seeding would never reach an
  existing deployment, and a placeholder that cannot be resolved makes every save
  in the tile editor fail validation. The trade-off is that these tiles cannot be
  reordered or removed in the editor.
* the sidebar, through `rag_links` and `rag_active`, behind the `rag_nav` and
  `rag_enabled` template globals.
* the Collections page, through `rag_backend`: the api-key, the two system
  collections and the operator role, read from the RAG module's own `.env`.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from app.core.envfile import load_env_file
from app.core.inventory import profiles_in
from app.core.tiles import Tile, TileGroup, check_value

RAG_PROFILE = "rag"

# Heading of the dashboard group, and of the sidebar category, which the template
# spells out itself.
GROUP_NAME = "RAG"


@dataclass(frozen=True)
class RagLink:
    """One web interface of the RAG system, shown as a tile and as a menu entry."""

    label: str  # sidebar entry
    tile_name: str  # dashboard tile
    href: str
    description: str
    icon: str  # "upload" or "database"; the template draws it


@dataclass(frozen=True)
class _Spec:
    label: str
    tile_name: str
    env_key: str
    path: str
    description: str
    icon: str


# In tile order. The Qdrant dashboard bypasses the MCP server's role checks, which is
# why the tiles are administrator-only.
_SPECS: tuple[_Spec, ...] = (
    _Spec(
        "Qdrant",
        "Qdrant",
        "QDRANT_PUBLIC_URL",
        "/dashboard",
        "Vector database: REST API and dashboard (needs the api-key)",
        "database",
    ),
    _Spec(
        "Ingest",
        "Qdrant Ingest",
        "QDRANT_INGEST_PUBLIC_URL",
        "/ui",
        "Ingestion jobs and database connections: web interface",
        "upload",
    ),
)

# The sidebar leads with ingestion, which is the day-to-day work; the vector
# database dashboard is the rarer visit.
_MENU_ORDER = ("Ingest", "Qdrant")


def _links(env: Mapping[str, str]) -> list[RagLink]:
    if RAG_PROFILE not in profiles_in(env):
        return []
    links: list[RagLink] = []
    for spec in _SPECS:
        base = env.get(spec.env_key, "").strip().rstrip("/")
        if not base:
            continue
        href, problem = check_value(base + spec.path, {})
        if problem is not None:
            # Same rule as every other tile link: http(s) or site-relative. A value
            # the core could not have derived is dropped rather than rendered.
            continue
        links.append(RagLink(spec.label, spec.tile_name, href, spec.description, spec.icon))
    return links


def rag_links(config_dir: str) -> list[RagLink]:
    """The sidebar entries, in menu order; empty unless the `rag` profile is active."""
    links = _links(load_env_file(Path(config_dir) / ".env"))
    return sorted(links, key=lambda link: _MENU_ORDER.index(link.label))


def rag_active(config_dir: str) -> bool:
    """Whether the core runs the RAG system, by its profile and nothing else.

    The sidebar category and the Collections page hang off this rather than off
    `rag_links`: that one drops an entry whose URL key is missing, and a profile that
    is on with a URL key missing must not hide the page that needs neither.
    """
    return RAG_PROFILE in profiles_in(load_env_file(Path(config_dir) / ".env"))


# The RAG module's env file, relative to the config directory. The core renders it
# and hands it to the ingester as its `env_file`, so it is where an operator who
# changes one of these names writes the change.
_MODULE_ENV = Path("ai") / "rag" / ".env"

# Defaults of the services themselves. Both the MCP server and the ingester fall back
# to these when nothing is set, and the core passes none of them on, so they are what
# is in force on a stock install.
DEFAULT_META_COLLECTION = "_collection_meta"
DEFAULT_ACL_COLLECTION = "_rbac_acl"
DEFAULT_OPERATOR_ROLE = "qdrant-ingest-operator"

# The same setting under two names, one per service: the MCP server reads the first
# key of each pair and the ingester the second. The manager reads the MCP server's
# name first, because that is the side that evaluates the access roles it writes.
_META_KEYS = ("EMBEDDING_META_COLLECTION", "QI_EMBED_META_COLLECTION")
_ACL_KEYS = ("RBAC_ACL_COLLECTION", "QI_RBAC_ACL_COLLECTION")
_OPERATOR_KEY = "QI_OIDC_OPERATOR_ROLE"
_API_KEY = "QDRANT_JWT_SECRET"


@dataclass(frozen=True)
class RagBackend:
    """What the Collections page needs to know about the RAG system's Qdrant."""

    api_key: str
    meta_collection: str
    acl_collection: str
    operator_role: str
    # Findings the page shows as a banner, for example two names for one collection
    # that disagree. Never a secret.
    warnings: tuple[str, ...] = ()

    @property
    def system_collections(self) -> frozenset[str]:
        return frozenset({self.meta_collection, self.acl_collection})


def _first_set(
    env: Mapping[str, str], keys: tuple[str, ...], default: str
) -> tuple[str, str | None]:
    """The first non-empty value among `keys`, and a warning if a later one differs."""
    values = [(key, env.get(key, "").strip()) for key in keys]
    set_values = [(key, value) for key, value in values if value]
    if not set_values:
        return default, None
    chosen_key, chosen = set_values[0]
    clash = next(((key, value) for key, value in set_values[1:] if value != chosen), None)
    if clash is None:
        return chosen, None
    return chosen, (
        f"{chosen_key} is {chosen!r} but {clash[0]} is {clash[1]!r}. The MCP server and the "
        "ingester must use the same name; the manager follows the first."
    )


def rag_backend(config_dir: str) -> RagBackend:
    """The RAG module's Qdrant settings, read from its `.env` at request time.

    A missing file or key is a normal state: every name has the services' own default,
    and a missing api-key is reported by the page rather than raised here.
    """
    env = load_env_file(Path(config_dir) / _MODULE_ENV)
    meta, meta_warning = _first_set(env, _META_KEYS, DEFAULT_META_COLLECTION)
    acl, acl_warning = _first_set(env, _ACL_KEYS, DEFAULT_ACL_COLLECTION)
    operator, _ = _first_set(env, (_OPERATOR_KEY,), DEFAULT_OPERATOR_ROLE)
    return RagBackend(
        api_key=env.get(_API_KEY, "").strip(),
        meta_collection=meta,
        acl_collection=acl,
        operator_role=operator,
        warnings=tuple(w for w in (meta_warning, acl_warning) if w),
    )


def _same_target(a: str, b: str) -> bool:
    return a.rstrip("/") == b.rstrip("/")


def with_rag_tiles(groups: list[TileGroup], env: Mapping[str, str]) -> list[TileGroup]:
    """`groups` plus the computed RAG tiles, when the `rag` profile is active.

    A tile the operator already created for the same service wins, matched by name
    or by link, so nothing is shown twice. Tiles go into a group the operator named
    "RAG" if there is one, otherwise into a group of their own at the end. The input
    is not modified.
    """
    links = _links(env)
    if not links:
        return groups

    shown = [tile for group in groups for tile in group.tiles]
    names = {tile.name.casefold() for tile in shown}
    tiles = [
        Tile(name=link.tile_name, href=link.href, description=link.description, visibility="admin")
        for link in links
        if link.tile_name.casefold() not in names
        and not any(_same_target(link.href, tile.href) for tile in shown)
    ]
    if not tiles:
        return groups

    for index, group in enumerate(groups):
        if group.name.casefold() == GROUP_NAME.casefold():
            merged = TileGroup(name=group.name, tiles=[*group.tiles, *tiles])
            return [*groups[:index], merged, *groups[index + 1 :]]
    return [*groups, TileGroup(name=GROUP_NAME, tiles=tiles)]
