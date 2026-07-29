"""Unit tests for dashboard tile configuration, resolution and visibility."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.core.tiles import (
    Tile,
    TileGroup,
    TilesConfig,
    load_tiles,
    resolve_placeholders,
    save_tiles,
    visible_groups,
)


def _config(*groups: TileGroup) -> TilesConfig:
    return TilesConfig(groups=list(groups))


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_seeds_default_tiles_on_first_run(tmp_path: Path) -> None:
    config = load_tiles(str(tmp_path))

    assert (tmp_path / "manager" / "tiles.yaml").exists()
    names = [tile.name for group in config.groups for tile in group.tiles]
    assert "LibreChat" in names
    assert "LiteLLM" in names


def test_seeded_infrastructure_tiles_are_admin_only(tmp_path: Path) -> None:
    config = load_tiles(str(tmp_path))
    infra = next(g for g in config.groups if g.name == "Infrastructure")
    assert {tile.visibility for tile in infra.tiles} == {"admin"}


def test_seed_is_idempotent(tmp_path: Path) -> None:
    first = load_tiles(str(tmp_path))
    second = load_tiles(str(tmp_path))
    assert len(first.groups) == len(second.groups)


def test_roundtrip_preserves_all_fields(tmp_path: Path) -> None:
    save_tiles(
        str(tmp_path),
        _config(
            TileGroup(
                name="Tools",
                tiles=[
                    Tile(
                        name="Thing",
                        href="https://thing.example",
                        description="A thing",
                        icon="https://thing.example/logo.svg",
                        visibility="admin",
                    )
                ],
            )
        ),
    )

    loaded = load_tiles(str(tmp_path))
    tile = loaded.groups[0].tiles[0]
    assert tile.name == "Thing"
    assert tile.href == "https://thing.example"
    assert tile.description == "A thing"
    assert tile.icon == "https://thing.example/logo.svg"
    assert tile.visibility == "admin"


def test_atomic_write_leaves_no_tmp_file(tmp_path: Path) -> None:
    save_tiles(str(tmp_path), _config())
    assert list((tmp_path / "manager").glob("*.tmp")) == []


def test_missing_optional_fields_get_defaults(tmp_path: Path) -> None:
    tiles_file = tmp_path / "manager" / "tiles.yaml"
    tiles_file.parent.mkdir(parents=True)
    tiles_file.write_text(
        yaml.dump(
            {"version": 1, "groups": [{"name": "G", "tiles": [{"name": "T", "href": "/x"}]}]}
        ),
        encoding="utf-8",
    )

    tile = load_tiles(str(tmp_path)).groups[0].tiles[0]
    assert tile.description == ""
    assert tile.icon is None
    # Omitting visibility is the documented way to say "everyone".
    assert tile.visibility == "all"


def test_unrecognised_visibility_fails_closed(tmp_path: Path) -> None:
    tiles_file = tmp_path / "manager" / "tiles.yaml"
    tiles_file.parent.mkdir(parents=True)
    tiles_file.write_text(
        yaml.dump(
            {
                "version": 1,
                "groups": [
                    {"name": "G", "tiles": [{"name": "T", "href": "/x", "visibility": "everyone"}]}
                ],
            }
        ),
        encoding="utf-8",
    )

    tile = load_tiles(str(tmp_path)).groups[0].tiles[0]
    assert tile.visibility == "admin", "a typo must restrict a tile, never widen it"


# ---------------------------------------------------------------------------
# Placeholder resolution
# ---------------------------------------------------------------------------


def test_resolves_known_placeholders() -> None:
    resolved = resolve_placeholders("{{PAPAIA_HOST}}:8000", {"PAPAIA_HOST": "https://papaia.test"})
    assert resolved == "https://papaia.test:8000"


def test_tolerates_whitespace_inside_placeholders() -> None:
    assert resolve_placeholders("{{ HOST }}/ui", {"HOST": "https://h"}) == "https://h/ui"


def test_leaves_unknown_placeholders_verbatim() -> None:
    assert resolve_placeholders("{{NOPE}}:8000", {}) == "{{NOPE}}:8000"


def test_tile_with_an_unresolved_placeholder_is_dropped() -> None:
    """An unresolvable link is removed rather than rendered half-substituted."""
    groups = visible_groups(
        _config(TileGroup(name="G", tiles=[Tile(name="T", href="{{MISSING}}:8000")])),
        is_admin=True,
        env={},
    )
    assert groups == []


def test_resolution_is_applied_to_hrefs_and_icons() -> None:
    groups = visible_groups(
        _config(
            TileGroup(
                name="G",
                tiles=[Tile(name="T", href="{{H}}:80", icon="{{H}}/logo.png")],
            )
        ),
        is_admin=False,
        env={"H": "https://h.test"},
    )
    tile = groups[0].tiles[0]
    assert tile.href == "https://h.test:80"
    assert tile.icon == "https://h.test/logo.png"


# ---------------------------------------------------------------------------
# Visibility filtering
# ---------------------------------------------------------------------------


def _mixed_config() -> TilesConfig:
    return _config(
        TileGroup(
            name="Apps",
            tiles=[
                Tile(name="Open", href="https://open.test", visibility="all"),
                Tile(name="Restricted", href="https://restricted.test", visibility="admin"),
            ],
        ),
        TileGroup(
            name="Infrastructure",
            tiles=[Tile(name="Keycloak", href="https://kc.test", visibility="admin")],
        ),
    )


def test_user_sees_only_unrestricted_tiles() -> None:
    groups = visible_groups(_mixed_config(), is_admin=False)

    assert [g.name for g in groups] == ["Apps"]
    assert [t.name for t in groups[0].tiles] == ["Open"]


def test_admin_sees_every_tile() -> None:
    groups = visible_groups(_mixed_config(), is_admin=True)

    assert [g.name for g in groups] == ["Apps", "Infrastructure"]
    assert [t.name for t in groups[0].tiles] == ["Open", "Restricted"]


def test_groups_left_empty_by_filtering_are_dropped() -> None:
    groups = visible_groups(
        _config(TileGroup(name="Infra", tiles=[Tile(name="X", href="/x", visibility="admin")])),
        is_admin=False,
    )
    assert groups == []


@pytest.mark.parametrize(
    "href",
    ["javascript:alert(1)", "data:text/html,<script>", "vbscript:x", "ftp://files.test"],
)
def test_unsafe_links_are_dropped(href: str) -> None:
    groups = visible_groups(
        _config(TileGroup(name="G", tiles=[Tile(name="Bad", href=href)])),
        is_admin=True,
    )
    assert groups == []


@pytest.mark.parametrize("href", ["https://a.test", "http://a.test", "/relative"])
def test_safe_links_are_kept(href: str) -> None:
    groups = visible_groups(
        _config(TileGroup(name="G", tiles=[Tile(name="Good", href=href)])),
        is_admin=True,
    )
    assert groups[0].tiles[0].href == href


def test_unsafe_icon_is_dropped_but_tile_survives() -> None:
    groups = visible_groups(
        _config(
            TileGroup(
                name="G",
                tiles=[Tile(name="T", href="https://t.test", icon="javascript:alert(1)")],
            )
        ),
        is_admin=True,
    )
    assert groups[0].tiles[0].icon is None


def test_filtering_does_not_mutate_the_loaded_config() -> None:
    config = _mixed_config()
    visible_groups(config, is_admin=False)
    assert [t.name for t in config.groups[0].tiles] == ["Open", "Restricted"]
