"""Unit tests for dashboard tile configuration, resolution and visibility."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.core.tiles import (
    Tile,
    TileGroup,
    TilesConfig,
    TilesFileError,
    check_value,
    link_placeholder_keys,
    load_tiles,
    load_tiles_raw,
    parse_tiles,
    resolve_placeholders,
    save_tiles,
    save_tiles_raw,
    tiles_revision,
    validate_config,
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


# ---------------------------------------------------------------------------
# The write path: revision, parse failures, raw access
# ---------------------------------------------------------------------------


def test_revision_is_empty_before_the_file_exists(tmp_path: Path) -> None:
    assert tiles_revision(str(tmp_path)) == ""


def test_revision_is_stable_across_reads(tmp_path: Path) -> None:
    load_tiles(str(tmp_path))
    assert tiles_revision(str(tmp_path)) == tiles_revision(str(tmp_path))


def test_revision_moves_when_the_file_changes(tmp_path: Path) -> None:
    load_tiles(str(tmp_path))
    before = tiles_revision(str(tmp_path))

    save_tiles(str(tmp_path), _config(TileGroup(name="Other", tiles=[])))

    assert tiles_revision(str(tmp_path)) != before


def test_saved_file_explains_that_comments_are_lost(tmp_path: Path) -> None:
    """The one warning an operator gets before a hand-edited file is rewritten."""
    save_tiles(str(tmp_path), _config())

    text = (tmp_path / "manager" / "tiles.yaml").read_text(encoding="utf-8")
    assert text.startswith("#")
    assert "comments and key order are not preserved" in text


def test_the_header_does_not_confuse_the_loader(tmp_path: Path) -> None:
    save_tiles(str(tmp_path), _config(TileGroup(name="G", tiles=[Tile(name="T", href="/x")])))

    loaded = load_tiles(str(tmp_path))

    assert [g.name for g in loaded.groups] == ["G"]


def test_broken_yaml_raises_rather_than_escaping_as_a_parser_error(tmp_path: Path) -> None:
    tiles_file = tmp_path / "manager" / "tiles.yaml"
    tiles_file.parent.mkdir(parents=True)
    tiles_file.write_text("groups: [unclosed", encoding="utf-8")

    with pytest.raises(TilesFileError):
        load_tiles(str(tmp_path))


def test_a_document_that_is_not_a_mapping_is_rejected() -> None:
    with pytest.raises(TilesFileError):
        parse_tiles("- just\n- a list\n")


def test_an_empty_document_loads_as_an_empty_dashboard() -> None:
    assert parse_tiles("") == TilesConfig(version=1, groups=[])


def test_raw_access_seeds_the_file_like_the_model_does(tmp_path: Path) -> None:
    text = load_tiles_raw(str(tmp_path))

    assert "LibreChat" in text
    assert (tmp_path / "manager" / "tiles.yaml").exists()


def test_raw_save_goes_through_the_model(tmp_path: Path) -> None:
    """A raw write is normalised, not stored verbatim.

    The unrecognised visibility below has to come back restricted -- the same
    coercion a hand edit gets on read, applied at write time instead.
    """
    save_tiles_raw(
        str(tmp_path),
        """version: 1
groups:
- name: G
  tiles:
  - name: T
    href: /x
    visibility: everyone
""",
    )

    assert load_tiles(str(tmp_path)).groups[0].tiles[0].visibility == "admin"


# ---------------------------------------------------------------------------
# check_value: one verdict for the editor and the renderer
# ---------------------------------------------------------------------------


def test_check_value_accepts_a_resolved_link() -> None:
    resolved, problem = check_value("{{H}}/ui", {"H": "https://h.test"})
    assert resolved == "https://h.test/ui"
    assert problem is None


def test_check_value_names_the_placeholder_it_could_not_resolve() -> None:
    _, problem = check_value("{{MISSING}}:8000", {})
    assert problem is not None
    assert "MISSING" in problem


def test_check_value_rejects_an_unsafe_scheme() -> None:
    _, problem = check_value("javascript:alert(1)", {})
    assert problem is not None


def test_check_value_and_the_renderer_agree(tmp_path: Path) -> None:
    """Whatever check_value calls a problem is what visible_groups drops."""
    env = {"H": "https://h.test"}
    for href in ["{{H}}:80", "{{NOPE}}:80", "ftp://files.test", "/relative"]:
        _, problem = check_value(href, env)
        groups = visible_groups(
            _config(TileGroup(name="G", tiles=[Tile(name="T", href=href)])),
            is_admin=True,
            env=env,
        )
        assert (problem is None) == bool(groups), href


# ---------------------------------------------------------------------------
# validate_config
# ---------------------------------------------------------------------------


def test_a_valid_draft_has_no_problems() -> None:
    problems = validate_config(
        _config(TileGroup(name="G", tiles=[Tile(name="T", href="https://t.test")])),
        {},
    )
    assert problems == []


def test_an_empty_group_name_is_a_problem() -> None:
    problems = validate_config(_config(TileGroup(name="  ", tiles=[])), {})
    assert [(p.group, p.tile, p.field) for p in problems] == [(0, None, "name")]


def test_duplicate_group_names_are_a_problem() -> None:
    problems = validate_config(
        _config(TileGroup(name="Tools", tiles=[]), TileGroup(name="tools", tiles=[])),
        {},
    )
    assert [p.group for p in problems] == [1]


def test_a_tile_problem_carries_the_position_it_belongs_to() -> None:
    problems = validate_config(
        _config(
            TileGroup(name="A", tiles=[Tile(name="Ok", href="/ok")]),
            TileGroup(
                name="B",
                tiles=[
                    Tile(name="Ok", href="/ok"),
                    Tile(name="Bad", href="javascript:alert(1)"),
                ],
            ),
        ),
        {},
    )
    assert [(p.group, p.tile, p.field) for p in problems] == [(1, 1, "href")]


def test_an_unsafe_icon_is_reported_separately_from_the_link() -> None:
    problems = validate_config(
        _config(
            TileGroup(
                name="G",
                tiles=[Tile(name="T", href="https://t.test", icon="javascript:alert(1)")],
            )
        ),
        {},
    )
    assert [p.field for p in problems] == ["icon"]


def test_an_empty_link_is_reported_once() -> None:
    problems = validate_config(
        _config(TileGroup(name="G", tiles=[Tile(name="T", href="")])), {}
    )
    assert [p.field for p in problems] == ["href"]


# ---------------------------------------------------------------------------
# link_placeholder_keys
# ---------------------------------------------------------------------------


def test_only_address_shaped_keys_are_offered_to_the_editor() -> None:
    keys = link_placeholder_keys(
        {
            "PAPAIA_HOST": "https://papaia.test",
            "PAPAIA_DOMAIN": "papaia.test",
            "SEARXNG_PORT": "8300",
            "LITELLM_URL": "https://llm.test",
            "KEYCLOAK_ADMIN_PASSWORD": "s3cret",
            "POSTGRES_USER": "papaia",
        }
    )

    assert keys == ["LITELLM_URL", "PAPAIA_DOMAIN", "PAPAIA_HOST", "SEARXNG_PORT"]


def test_no_secret_shaped_key_reaches_the_editor() -> None:
    assert link_placeholder_keys({"MANAGER_SESSION_SECRET": "x", "GIT_TOKEN": "y"}) == []
