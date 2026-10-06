"""The optional RAG system in the manager: computed dashboard tiles and the sidebar.

The manager recognises the system by the `rag` profile in the core `.env`, never by
the URL keys, which the core stores while the profile is off. Everything here goes
through a configuration directory of its own, injected by overriding the settings
dependency, as `test_api_tiles.py` does.
"""
from __future__ import annotations

import base64
import json
import os
import re
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

_CONFIG_DIR = tempfile.mkdtemp(prefix="papaia-rag-config-")
_WORKSPACE_DIR = tempfile.mkdtemp(prefix="papaia-rag-workspace-")

for _key, _value in {
    "OIDC_ISSUER_KC_AUTH": "https://kc.test/auth",
    "OIDC_ISSUER_KC_TOKEN": "https://kc.test/token",
    "OIDC_ISSUER_KC_CERTS": "https://kc.test/certs",
    "MANAGER_ADMIN_ROLE": "admin",
    "MANAGER_USER_ROLE": "user",
    "MANAGER_HOST": "http://localhost:8120",
    "MANAGER_OIDC_CLIENT_SECRET": "client-secret",
    "MANAGER_SESSION_SECRET": "test-session-secret-value",
    "PAPAIA_CONFIG_DIR": _CONFIG_DIR,
    "PAPAIA_WORKSPACE_DIR": _WORKSPACE_DIR,
}.items():
    os.environ.setdefault(_key, _value)

from fastapi.testclient import TestClient  # noqa: E402
from itsdangerous import TimestampSigner  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.core import rag  # noqa: E402
from app.core.inventory import profiles_in  # noqa: E402
from app.core.tiles import Tile, TileGroup  # noqa: E402
from app.main import create_app  # noqa: E402

_CSRF = "test-csrf-token-value"

_QDRANT = "https://qdrant.test"
_INGEST = "https://ingest.test"
_RAG_ENV = (
    "PAPAIA_HOST=https://papaia.test\n"
    "COMPOSE_PROFILES=keycloak,librechat,rag\n"
    f"QDRANT_PUBLIC_URL={_QDRANT}\n"
    f"QDRANT_INGEST_PUBLIC_URL={_INGEST}\n"
)
# What a core without the RAG choice writes, and what one writes after the choice
# was turned off again: the URLs stay, the profile does not.
_OFF_ENV = (
    "PAPAIA_HOST=https://papaia.test\n"
    "COMPOSE_PROFILES=keycloak,librechat\n"
    f"QDRANT_PUBLIC_URL={_QDRANT}\n"
    f"QDRANT_INGEST_PUBLIC_URL={_INGEST}\n"
)
_OLD_CORE_ENV = "PAPAIA_HOST=https://papaia.test\nCOMPOSE_PROFILES=keycloak,librechat\n"


def _write_env(config_dir: Path, text: str) -> None:
    (config_dir / ".env").write_text(text, encoding="utf-8")


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "config"
    (directory / "manager").mkdir(parents=True)
    _write_env(directory, _RAG_ENV)
    return directory


@pytest.fixture
def client(config_dir: Path) -> Iterator[TestClient]:
    get_settings.cache_clear()
    settings = get_settings().model_copy(update={"papaia_config_dir": str(config_dir)})
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    yield TestClient(app, follow_redirects=False)
    get_settings.cache_clear()


def _as(client: TestClient, *roles: str) -> TestClient:
    session: dict[str, Any] = {
        "user": {
            "sub": "u-1",
            "preferred_username": "tester",
            "roles": list(roles),
            "exp": int(time.time()) + 3600,
        },
        "_csrf_token": _CSRF,
    }
    payload = base64.b64encode(json.dumps(session).encode())
    signed = TimestampSigner(get_settings().manager_session_secret).sign(payload).decode()
    client.cookies.clear()
    client.cookies.set("papaia_manager_session", signed)
    return client


def _admin(client: TestClient) -> TestClient:
    return _as(client, "admin")


def _nav_groups(body: str) -> dict[str, list[str]]:
    """Caption -> the entries under it, in order; the caption-less lead is ''."""
    nav = body[body.index("<nav") : body.index("</nav>")]
    parts = re.split(r'<p class="sidebar-label[^>]*>([^<]+)</p>', nav)
    groups = {"": re.findall(r'aria-label="([^"]+)"', parts[0])}
    for caption, chunk in zip(parts[1::2], parts[2::2], strict=True):
        groups[caption] = re.findall(r'aria-label="([^"]+)"', chunk)
    return groups


def _tile(name: str, href: str) -> Tile:
    return Tile(name=name, href=href)


# ---------------------------------------------------------------------------
# The profile decides, not the URLs
# ---------------------------------------------------------------------------


def test_profiles_are_read_from_compose_profiles() -> None:
    assert profiles_in({"COMPOSE_PROFILES": "keycloak, rag ,librechat"}) == {
        "keycloak",
        "rag",
        "librechat",
    }
    assert profiles_in({}) == set()
    assert profiles_in({"COMPOSE_PROFILES": ""}) == set()


def test_nothing_is_offered_without_the_profile_even_when_the_urls_are_stored(
    config_dir: Path,
) -> None:
    _write_env(config_dir, _OFF_ENV)

    assert not rag.rag_active(str(config_dir))
    off = {"COMPOSE_PROFILES": "keycloak", "QDRANT_PUBLIC_URL": _QDRANT}
    assert rag.with_rag_tiles([], off) == []


def test_a_core_that_does_not_know_the_profile_yields_nothing(config_dir: Path) -> None:
    _write_env(config_dir, _OLD_CORE_ENV)

    assert not rag.rag_active(str(config_dir))
    assert rag.with_rag_tiles([], {"COMPOSE_PROFILES": "keycloak,librechat"}) == []


def test_a_missing_env_file_yields_nothing(tmp_path: Path) -> None:
    assert not rag.rag_active(str(tmp_path / "does-not-exist"))


# ---------------------------------------------------------------------------
# The Qdrant tile
# ---------------------------------------------------------------------------


def test_the_tile_points_at_the_qdrant_dashboard(config_dir: Path) -> None:
    tiles = rag._tiles({"COMPOSE_PROFILES": "rag", "QDRANT_PUBLIC_URL": _QDRANT})

    assert [(t.tile_name, t.href) for t in tiles] == [("Qdrant", f"{_QDRANT}/dashboard")]


def test_a_trailing_slash_in_the_stored_url_does_not_double() -> None:
    tiles = rag._tiles({"COMPOSE_PROFILES": "rag", "QDRANT_PUBLIC_URL": f"{_QDRANT}///"})

    assert [t.href for t in tiles] == [f"{_QDRANT}/dashboard"]


def test_a_url_that_is_missing_or_unsafe_is_left_out() -> None:
    unsafe = {"COMPOSE_PROFILES": "rag", "QDRANT_PUBLIC_URL": "javascript:alert(1)//"}

    assert rag._tiles(unsafe) == []
    assert rag._tiles({"COMPOSE_PROFILES": "rag"}) == []


def test_the_ingester_has_no_tile_whatever_the_core_stores_for_it() -> None:
    """Its jobs, runs and credentials are pages of the manager; its own interface has no tile."""
    tiles = rag._tiles(_ENV)

    assert [t.tile_name for t in tiles] == ["Qdrant"]
    assert all(_INGEST not in t.href for t in tiles)


# ---------------------------------------------------------------------------
# Computed tiles
# ---------------------------------------------------------------------------

_ENV = {
    "COMPOSE_PROFILES": "rag",
    "QDRANT_PUBLIC_URL": _QDRANT,
    "QDRANT_INGEST_PUBLIC_URL": _INGEST,
}


def test_the_tiles_form_a_group_of_their_own_for_administrators() -> None:
    groups = [TileGroup("Tools", [_tile("Wiki", "https://wiki.test")])]

    result = rag.with_rag_tiles(groups, _ENV)

    assert [g.name for g in result] == ["Tools", "RAG"]
    tiles = result[1].tiles
    assert [t.name for t in tiles] == ["Qdrant"]
    assert [t.href for t in tiles] == [f"{_QDRANT}/dashboard"]
    # The Qdrant dashboard bypasses the MCP server's role checks.
    assert {t.visibility for t in tiles} == {"admin"}


def test_the_input_is_not_modified() -> None:
    groups = [TileGroup("Tools", [_tile("Wiki", "https://wiki.test")])]

    rag.with_rag_tiles(groups, _ENV)

    assert [g.name for g in groups] == ["Tools"]
    assert len(groups[0].tiles) == 1


def test_without_the_profile_the_groups_come_back_unchanged() -> None:
    groups = [TileGroup("Tools", [_tile("Wiki", "https://wiki.test")])]

    assert rag.with_rag_tiles(groups, {**_ENV, "COMPOSE_PROFILES": "keycloak"}) is groups


def test_a_tile_the_operator_made_for_the_same_service_wins_by_name() -> None:
    groups = [TileGroup("Mine", [_tile("qdrant", "https://my-qdrant.test")])]

    result = rag.with_rag_tiles(groups, _ENV)

    assert [t.name for g in result for t in g.tiles] == ["qdrant"]


def test_a_tile_the_operator_made_for_the_same_link_wins_by_link() -> None:
    groups = [TileGroup("Mine", [_tile("Vectors", f"{_QDRANT}/dashboard/")])]

    result = rag.with_rag_tiles(groups, _ENV)

    assert [t.name for g in result for t in g.tiles] == ["Vectors"]


def test_nothing_is_added_when_the_operator_already_has_it() -> None:
    groups = [TileGroup("Mine", [_tile("Qdrant", "https://a.test")])]

    assert rag.with_rag_tiles(groups, _ENV) is groups


def test_the_tiles_join_a_group_the_operator_already_called_rag() -> None:
    groups = [
        TileGroup("Tools", [_tile("Wiki", "https://wiki.test")]),
        TileGroup("rag", [_tile("Notes", "https://notes.test")]),
    ]

    result = rag.with_rag_tiles(groups, _ENV)

    assert [g.name for g in result] == ["Tools", "rag"]
    assert [t.name for t in result[1].tiles] == ["Notes", "Qdrant"]


# ---------------------------------------------------------------------------
# The dashboard
# ---------------------------------------------------------------------------


def test_an_administrator_sees_the_qdrant_tile_and_no_ingest_tile(client: TestClient) -> None:
    body = _admin(client).get("/partials/tiles").text

    assert f"{_QDRANT}/dashboard" in body
    assert "Qdrant Ingest" not in body
    assert f"{_INGEST}/ui" not in body
    assert re.search(r"RAG\s*&nbsp;·&nbsp;\s*1\s+application\b", body), "the group heading"


def test_a_user_without_the_admin_role_does_not_see_them(client: TestClient) -> None:
    body = _as(client, "user").get("/partials/tiles").text

    assert "Qdrant" not in body
    assert f"{_QDRANT}/dashboard" not in body


@pytest.mark.parametrize("env", [_OFF_ENV, _OLD_CORE_ENV])
def test_no_tile_appears_without_the_profile(
    client: TestClient, config_dir: Path, env: str
) -> None:
    _write_env(config_dir, env)

    body = _admin(client).get("/partials/tiles").text

    assert "Qdrant" not in body


def test_the_tiles_follow_the_profile_without_a_restart(
    client: TestClient, config_dir: Path
) -> None:
    assert "Qdrant" in _admin(client).get("/partials/tiles").text

    _write_env(config_dir, _OFF_ENV)

    assert "Qdrant" not in _admin(client).get("/partials/tiles").text


def test_a_tile_the_operator_created_is_shown_once(client: TestClient, config_dir: Path) -> None:
    (config_dir / "manager" / "tiles.yaml").write_text(
        "version: 1\ngroups:\n- name: Mine\n  tiles:\n"
        f"  - name: Qdrant\n    href: {_QDRANT}/dashboard\n    visibility: admin\n",
        encoding="utf-8",
    )

    body = _admin(client).get("/partials/tiles").text

    assert body.count(f"{_QDRANT}/dashboard") == 1


def test_the_tiles_are_computed_and_never_written_to_the_file(
    client: TestClient, config_dir: Path
) -> None:
    _admin(client).get("/partials/tiles")
    tiles_file = (config_dir / "manager" / "tiles.yaml").read_text(encoding="utf-8")

    assert "Qdrant" not in tiles_file
    assert "QDRANT" not in tiles_file


def test_the_editor_does_not_list_the_computed_tiles_and_still_saves(
    client: TestClient,
) -> None:
    editor = _admin(client).get("/partials/tiles/edit").text
    assert "Qdrant" not in editor

    read = _admin(client).get("/api/v1/tiles")
    assert "Qdrant" not in read.text
    body = read.json()
    saved = _admin(client).put(
        "/api/v1/tiles",
        headers={"X-CSRF-Token": _CSRF},
        json={"revision": body["revision"], "version": 1, "groups": body["groups"]},
    )

    assert saved.status_code == 200, saved.text


# ---------------------------------------------------------------------------
# The sidebar
# ---------------------------------------------------------------------------


def test_the_admin_sidebar_has_a_rag_category_between_extensions_and_system(
    client: TestClient,
) -> None:
    groups = _nav_groups(_admin(client).get("/").text)

    assert list(groups) == ["", "Monitor", "Extensions", "RAG", "System"]
    assert groups["RAG"] == [
        "Connections",
        "Collections",
        "Embedding",
        "Ingest jobs",
        "Ingest runs",
    ]
    assert groups["Extensions"] == ["Add-Ons", "Catalogs"]


@pytest.mark.parametrize("page", ["/connections", "/collections", "/embedding"])
def test_the_manager_pages_are_not_links_out(client: TestClient, page: str) -> None:
    nav = _admin(client).get("/").text
    nav = nav[nav.index("<nav") : nav.index("</nav>")]

    anchor = re.search(rf'<a href="{page}"[^>]*>', nav)
    assert anchor
    assert "target=" not in anchor.group(0)
    assert "aria-label=" in anchor.group(0), "the collapsed rail has no other label"


def test_connections_is_marked_active_on_its_own_page_only(client: TestClient) -> None:
    def active(body: str) -> bool:
        nav = body[body.index("<nav") : body.index("</nav>")]
        row = nav[nav.index('<a href="/connections"') :]
        return "bg-secondary/15" in row[: row.index("</a>")]

    assert not active(_admin(client).get("/collections").text)
    assert active(_admin(client).get("/connections").text)


def test_collections_is_marked_active_on_its_own_page_only(client: TestClient) -> None:
    # The page is a shell; the list is fetched afterwards, so no Qdrant is involved.
    def active(body: str) -> bool:
        nav = body[body.index("<nav") : body.index("</nav>")]
        row = nav[nav.index('<a href="/collections"') :]
        return "bg-secondary/15" in row[: row.index("</a>")]

    assert not active(_admin(client).get("/catalogs").text)
    assert active(_admin(client).get("/collections").text)


def test_embedding_is_marked_active_on_its_own_page_only(client: TestClient) -> None:
    # The page is a shell; the body is fetched afterwards, so no Qdrant is involved.
    def active(body: str) -> bool:
        nav = body[body.index("<nav") : body.index("</nav>")]
        row = nav[nav.index('<a href="/embedding"') :]
        return "bg-secondary/15" in row[: row.index("</a>")]

    assert not active(_admin(client).get("/collections").text)
    assert active(_admin(client).get("/embedding").text)


def test_the_category_survives_a_profile_that_is_on_without_its_url_keys(
    client: TestClient, config_dir: Path
) -> None:
    _write_env(config_dir, "COMPOSE_PROFILES=keycloak,rag\n")

    groups = _nav_groups(_admin(client).get("/").text)

    assert groups["RAG"] == [
        "Connections",
        "Collections",
        "Embedding",
        "Ingest jobs",
        "Ingest runs",
    ]


def test_no_entry_of_the_category_leaves_the_manager(client: TestClient) -> None:
    """The ingester's interface is replaced by pages, and Qdrant has its tile on the dashboard."""
    body = _admin(client).get("/").text
    nav = body[body.index("<nav") : body.index("</nav>")]

    assert 'target="_blank"' not in nav
    assert f"{_INGEST}/ui" not in nav
    assert f"{_QDRANT}/dashboard" not in nav
    assert "aria-label=\"Ingest\"" not in nav and "aria-label=\"Qdrant\"" not in nav


@pytest.mark.parametrize("env", [_OFF_ENV, _OLD_CORE_ENV])
def test_there_is_no_empty_category_without_the_profile(
    client: TestClient, config_dir: Path, env: str
) -> None:
    _write_env(config_dir, env)

    body = _admin(client).get("/").text

    assert list(_nav_groups(body)) == ["", "Monitor", "Extensions", "System"]
    assert ">RAG<" not in body


def test_a_user_without_the_admin_role_gets_no_category_even_with_the_profile(
    client: TestClient,
) -> None:
    body = _as(client, "user").get("/").text

    assert _nav_groups(body) == {"": ["Dashboard"]}
    assert _QDRANT not in body[body.index("<nav") : body.index("</nav>")]


def test_the_category_appears_on_the_other_admin_pages_too(client: TestClient) -> None:
    for path in ("/addons", "/catalogs"):
        response = _admin(client).get(path)
        assert response.status_code == 200, path
        assert "RAG" in _nav_groups(response.text), path


def test_an_error_page_renders_without_the_category(client: TestClient) -> None:
    response = _as(client, "user").get("/addons")

    assert response.status_code == 403
    assert ">RAG<" not in response.text
