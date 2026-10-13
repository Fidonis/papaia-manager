"""Widget tiles in tiles.yaml: the file format, identity, visibility and validation.

Link tiles keep their format and their tests in `test_tiles.py`. What is pinned here is
what a widget tile adds -- and that adding it left a file without widgets exactly as it was.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from app.core.tiles import (
    Tile,
    TileGroup,
    TilesConfig,
    WidgetTile,
    config_from_json,
    config_to_json,
    find_widget,
    load_tiles,
    parse_tiles,
    save_tiles,
    validate_config,
    visible_groups,
)


def _widgets(config: TilesConfig) -> list[WidgetTile]:
    return [t for g in config.groups for t in g.tiles if isinstance(t, WidgetTile)]


def _doc(*tiles: dict[str, object]) -> str:
    return yaml.safe_dump({"version": 1, "groups": [{"name": "Status", "tiles": list(tiles)}]})


# ---------------------------------------------------------------------------
# The file format
# ---------------------------------------------------------------------------


def test_a_widget_tile_is_read_with_all_its_fields() -> None:
    config = parse_tiles(
        _doc(
            {
                "type": "widget",
                "widget": "backup-age",
                "id": "abc123",
                "title": "Backups",
                "size": "m",
                "visibility": "all",
            }
        )
    )

    (tile,) = _widgets(config)
    assert (tile.widget, tile.id, tile.title, tile.size, tile.visibility) == (
        "backup-age",
        "abc123",
        "Backups",
        "m",
        "all",
    )


def test_a_tile_without_a_type_is_still_a_link() -> None:
    """The format is additive: every file written before widgets existed reads as before."""
    config = parse_tiles(_doc({"name": "LibreChat", "href": "https://chat.test"}))

    (tile,) = config.groups[0].tiles
    assert isinstance(tile, Tile)
    assert tile.href == "https://chat.test"


def test_a_widget_without_a_visibility_is_for_administrators() -> None:
    """A link left unsaid is for everyone; a reading of the installation is not."""
    config = parse_tiles(
        _doc({"type": "widget", "widget": "backup-age"}, {"name": "A", "href": "/a"})
    )

    widget, link = config.groups[0].tiles
    assert isinstance(widget, WidgetTile) and widget.visibility == "admin"
    assert isinstance(link, Tile) and link.visibility == "all"


def test_a_visibility_that_is_not_understood_restricts_a_widget() -> None:
    config = parse_tiles(_doc({"type": "widget", "widget": "backup-age", "visibility": "world"}))

    assert _widgets(config)[0].visibility == "admin"


def test_a_size_that_is_not_understood_falls_back_to_the_narrowest() -> None:
    config = parse_tiles(_doc({"type": "widget", "widget": "backup-age", "size": "huge"}))

    assert _widgets(config)[0].size == "s"


def test_a_widget_survives_a_save_and_a_load(tmp_path: Path) -> None:
    config = TilesConfig(
        groups=[
            TileGroup(
                name="Status",
                tiles=[
                    WidgetTile(
                        widget="disk-space", id="d1", title="Disks", size="l", visibility="all"
                    ),
                    Tile(name="LibreChat", href="https://chat.test"),
                ],
            )
        ]
    )

    save_tiles(str(tmp_path), config)
    loaded = load_tiles(str(tmp_path))

    assert loaded.groups[0].tiles == config.groups[0].tiles
    written = yaml.safe_load((tmp_path / "manager" / "tiles.yaml").read_text(encoding="utf-8"))
    assert written["groups"][0]["tiles"][0] == {
        "type": "widget",
        "widget": "disk-space",
        "id": "d1",
        "title": "Disks",
        "size": "l",
        "visibility": "all",
    }


def test_an_empty_title_is_not_written() -> None:
    from app.core.tiles import _tile_to_dict  # noqa: PLC0415

    assert "title" not in _tile_to_dict(WidgetTile(widget="backup-age", id="x1"))


def test_the_wire_form_tells_a_widget_from_a_link_and_leaves_the_link_alone() -> None:
    config = TilesConfig(
        groups=[
            TileGroup(
                name="G",
                tiles=[
                    WidgetTile(widget="backup-age", id="x1"),
                    Tile(name="A", href="/a"),
                ],
            )
        ]
    )

    widget, link = config_to_json(config)["groups"][0]["tiles"]

    assert widget["type"] == "widget" and widget["widget"] == "backup-age"
    assert "type" not in link
    assert link == {
        "name": "A",
        "href": "/a",
        "description": "",
        "icon": None,
        "visibility": "all",
    }


def test_the_wire_form_comes_back_the_same() -> None:
    config = parse_tiles(
        _doc(
            {"type": "widget", "widget": "backup-age", "id": "x1", "size": "m", "visibility": "all"}
        )
    )

    again = config_from_json(config_to_json(config))

    assert again.groups[0].tiles == config.groups[0].tiles


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def test_a_widget_without_an_id_gets_one_and_the_same_one_every_time() -> None:
    document = _doc({"type": "widget", "widget": "backup-age"})

    first = _widgets(parse_tiles(document))[0].id
    second = _widgets(parse_tiles(document))[0].id

    assert first and first == second


def test_two_widgets_of_one_type_get_different_ids() -> None:
    config = parse_tiles(
        _doc({"type": "widget", "widget": "disk-space"}, {"type": "widget", "widget": "disk-space"})
    )

    ids = [t.id for t in _widgets(config)]

    assert len(set(ids)) == 2


def test_an_id_written_down_is_kept() -> None:
    config = parse_tiles(
        _doc(
            {"type": "widget", "widget": "disk-space", "id": "mine"},
            {"type": "widget", "widget": "disk-space"},
        )
    )

    ids = [t.id for t in _widgets(config)]

    assert ids[0] == "mine" and ids[1] != "mine"


def test_a_repeated_id_is_given_to_the_first_only() -> None:
    config = parse_tiles(
        _doc(
            {"type": "widget", "widget": "disk-space", "id": "same"},
            {"type": "widget", "widget": "backup-age", "id": "same"},
        )
    )

    ids = [t.id for t in _widgets(config)]

    assert ids[0] == "same" and ids[1] not in ("", "same")


def test_an_id_that_is_not_path_safe_is_replaced() -> None:
    config = parse_tiles(_doc({"type": "widget", "widget": "backup-age", "id": "../etc/x"}))

    tile = _widgets(config)[0]

    assert tile.id and "/" not in tile.id and "." not in tile.id


def test_a_widget_can_be_found_by_its_id() -> None:
    config = parse_tiles(_doc({"type": "widget", "widget": "backup-age", "id": "x1"}))

    assert find_widget(config, "x1") is not None
    assert find_widget(config, "nope") is None


# ---------------------------------------------------------------------------
# Visibility
# ---------------------------------------------------------------------------


def _mixed() -> TilesConfig:
    return TilesConfig(
        groups=[
            TileGroup(
                name="Status",
                tiles=[
                    WidgetTile(widget="backup-age", id="a", visibility="admin"),
                    WidgetTile(widget="disk-space", id="b", visibility="all"),
                ],
            ),
            TileGroup(
                name="Admin only",
                tiles=[WidgetTile(widget="core-version", id="c", visibility="admin")],
            ),
        ]
    )


def test_a_user_gets_only_the_widgets_set_for_everyone() -> None:
    groups = visible_groups(_mixed(), is_admin=False)

    assert [[t.id for t in g.tiles if isinstance(t, WidgetTile)] for g in groups] == [["b"]]


def test_an_administrator_gets_every_widget() -> None:
    groups = visible_groups(_mixed(), is_admin=True)

    assert [t.id for g in groups for t in g.tiles if isinstance(t, WidgetTile)] == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_a_widget_that_does_not_exist_is_a_problem_on_its_tile() -> None:
    config = TilesConfig(groups=[TileGroup(name="G", tiles=[WidgetTile(widget="no-such", id="x")])])

    problems = validate_config(config, {}, known_widgets={"backup-age"})

    assert [(p.group, p.tile, p.field) for p in problems] == [(0, 0, "widget")]
    assert "no-such" in problems[0].message


def test_a_known_widget_passes() -> None:
    config = TilesConfig(
        groups=[TileGroup(name="G", tiles=[WidgetTile(widget="backup-age", id="x")])]
    )

    assert validate_config(config, {}, known_widgets={"backup-age"}) == []


def test_without_a_list_of_widgets_their_names_are_not_judged() -> None:
    config = TilesConfig(
        groups=[TileGroup(name="G", tiles=[WidgetTile(widget="anything", id="x")])]
    )

    assert validate_config(config, {}) == []


def test_a_widget_without_a_name_is_a_problem() -> None:
    config = TilesConfig(groups=[TileGroup(name="G", tiles=[WidgetTile(widget=" ", id="x")])])

    problems = validate_config(config, {}, known_widgets={"backup-age"})

    assert [p.field for p in problems] == ["widget"]


def test_a_title_that_is_too_long_is_a_problem() -> None:
    config = TilesConfig(
        groups=[
            TileGroup(name="G", tiles=[WidgetTile(widget="backup-age", id="x", title="t" * 81)])
        ]
    )

    problems = validate_config(config, {}, known_widgets={"backup-age"})

    assert [p.field for p in problems] == ["title"]


def test_link_problems_are_still_found_next_to_a_widget() -> None:
    config = TilesConfig(
        groups=[
            TileGroup(
                name="G",
                tiles=[WidgetTile(widget="backup-age", id="x"), Tile(name="A", href="ftp://x")],
            )
        ]
    )

    problems = validate_config(config, {}, known_widgets={"backup-age"})

    assert [(p.tile, p.field) for p in problems] == [(1, "href")]
