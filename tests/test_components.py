"""The components an add-on's manifest declares: parsing, the views, and the pages that show them.

A catalog is not necessarily ours, so the parser is tested against shapes a manifest should
never have. The page tests run the real routes over a local catalog, an installed snapshot of an
older version and a bundle `.env` that pins one image by hand; Docker is replaced by an empty
reading, so no daemon is needed.
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
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

_CONFIG_DIR = tempfile.mkdtemp(prefix="papaia-components-config-")
_WORKSPACE_DIR = tempfile.mkdtemp(prefix="papaia-components-workspace-")

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

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from itsdangerous import TimestampSigner  # noqa: E402

from app.config import Settings, get_settings  # noqa: E402
from app.core import components  # noqa: E402
from app.main import create_app  # noqa: E402
from app.routers import api_addons, ui  # noqa: E402

_CSRF = "test-csrf-token-value"


def _entry(ident: str = "paperless-ngx", **extra: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": ident,
        "name": "Paperless-ngx",
        "role": "primary",
        "kind": "upstream",
        "image_var": "PAPERLESS_IMAGE",
        "version": "3.0.5",
        "source": "https://github.com/paperless-ngx/paperless-ngx",
    }
    entry.update(extra)
    return entry


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def test_a_manifest_without_components_has_none() -> None:
    assert components.parse_components(None) == []
    assert components.parse_components({}) == []
    assert components.parse_components({"components": "paperless"}) == []


def test_a_well_formed_entry_is_read_as_written() -> None:
    (found,) = components.parse_components({"components": [_entry(pinned=False)]})
    assert (found.id, found.name, found.role, found.kind) == (
        "paperless-ngx",
        "Paperless-ngx",
        "primary",
        "upstream",
    )
    assert found.version == "3.0.5"
    assert found.image_var == "PAPERLESS_IMAGE"
    assert found.source == "https://github.com/paperless-ngx/paperless-ngx"
    assert found.floating is True


def test_entries_that_cannot_be_shown_are_skipped() -> None:
    declared = [
        "not a mapping",
        _entry("no-version", version=None),
        _entry("number", version=18),  # an unquoted `18` is a number, not a tag
        _entry("no-name", name=""),
        _entry("ok"),
        _entry("ok"),  # a repeated id
    ]
    assert [c.id for c in components.parse_components({"components": declared})] == ["ok"]


def test_an_unknown_role_does_not_promote_an_image_to_the_card() -> None:
    (found,) = components.parse_components({"components": [_entry(role="main")]})
    assert found.role == "sidecar"


@pytest.mark.parametrize(
    "source",
    ["javascript:alert(1)", "http://example.com/x", "https://a b", 'https://x"onmouseover=1', 7],
)
def test_only_an_https_source_becomes_a_link(source: object) -> None:
    (found,) = components.parse_components({"components": [_entry(source=source)]})
    assert found.source is None


def test_an_image_variable_is_a_plain_name_or_nothing() -> None:
    (found,) = components.parse_components({"components": [_entry(image_var="A B; rm")]})
    assert found.image_var is None


def test_long_text_is_cut_and_the_list_is_capped() -> None:
    (found,) = components.parse_components({"components": [_entry(name="x" * 500)]})
    assert len(found.name) == 80
    many = [_entry(f"c{i}") for i in range(100)]
    assert len(components.parse_components({"components": many})) == 32


@pytest.mark.parametrize(
    ("ref", "tag"),
    [
        ("ghcr.io/o/n:0.3.0", "0.3.0"),
        ("localhost:5000/n:1.0.0@sha256:abc", "1.0.0"),
        ("localhost:5000/n", "latest"),
    ],
)
def test_image_tag(ref: str, tag: str) -> None:
    assert components.image_tag(ref) == tag


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------
def _manifest(version: str = "3.0.5", **extra: Any) -> dict[str, Any]:
    return {"components": [_entry(version=version, **extra)]}


def test_an_add_on_that_is_not_installed_shows_the_catalog_version_only() -> None:
    (view,) = components.component_views(_manifest(), None, None)
    assert view["version"] == "3.0.5"
    assert view["installed_version"] is None
    assert view["changed"] is False
    assert view["override_version"] is None


def test_an_installed_add_on_shows_what_an_update_changes() -> None:
    (view,) = components.component_views(_manifest("3.1.0"), _manifest("3.0.5"), {})
    assert (view["installed_version"], view["version"], view["changed"]) == ("3.0.5", "3.1.0", True)


def test_an_unchanged_component_is_not_marked() -> None:
    (view,) = components.component_views(_manifest(), _manifest(), {})
    assert view["installed_version"] == "3.0.5"
    assert view["changed"] is False


def test_a_snapshot_from_before_components_existed_has_no_installed_version() -> None:
    (view,) = components.component_views(_manifest(), {"version": "1.0.0"}, {})
    assert view["installed_version"] is None
    assert view["changed"] is False


def test_a_tag_pinned_in_the_env_is_shown_as_overridden() -> None:
    env = {"PAPERLESS_IMAGE": "registry.example/paperless:3.0.7"}
    (view,) = components.component_views(_manifest(), _manifest(), env)
    assert view["override_version"] == "3.0.7"
    assert view["override_ref"] == "registry.example/paperless:3.0.7"


def test_an_env_value_equal_to_the_version_in_use_is_not_an_override() -> None:
    env = {"PAPERLESS_IMAGE": "ghcr.io/paperless-ngx/paperless-ngx:3.0.5"}
    (view,) = components.component_views(_manifest("3.1.0"), _manifest("3.0.5"), env)
    assert view["override_version"] is None


def test_an_empty_env_value_is_not_an_override() -> None:
    (view,) = components.component_views(_manifest(), _manifest(), {"PAPERLESS_IMAGE": "  "})
    assert view["override_version"] is None


def test_an_add_on_the_catalogs_dropped_keeps_its_installed_components() -> None:
    (view,) = components.component_views({}, _manifest(), {})
    assert view["version"] == "3.0.5"
    assert view["changed"] is False


def test_the_installed_side_is_read_from_the_snapshot_and_the_bundle(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "papaia-app.yaml").write_text(yaml.safe_dump(_manifest("3.0.5")), encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text("PAPERLESS_IMAGE=docker.io/x/paperless:9.9.9\n", encoding="utf-8")
    (view,) = components.addon_components(_manifest("3.1.0"), snapshot, env)
    assert (view["installed_version"], view["override_version"]) == ("3.0.5", "9.9.9")


def test_a_missing_snapshot_or_env_is_not_an_error(tmp_path: Path) -> None:
    (view,) = components.addon_components(_manifest(), tmp_path / "gone", tmp_path / "gone.env")
    assert view["installed_version"] is None
    assert view["override_version"] is None


def test_a_damaged_snapshot_manifest_reads_as_nothing(tmp_path: Path) -> None:
    (tmp_path / "papaia-app.yaml").write_text("components: [unclosed", encoding="utf-8")
    assert components.read_manifest(tmp_path) is None


# ---------------------------------------------------------------------------
# The pages
# ---------------------------------------------------------------------------
def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def world(tmp_path: Path) -> dict[str, Path]:
    config = tmp_path / "config"
    workspace = tmp_path / "workspace"
    catalog = tmp_path / "catalog"
    managed = workspace / "addons" / "_managed" / "fidonis" / "paperless"
    sidecar = {"role": "sidecar", "kind": "upstream", "source": "https://www.postgresql.org"}

    _write(config / ".env", "PAPAIA_HOST=https://papaia.test\n")
    _write(
        config / "manager" / "catalogs.yaml",
        yaml.safe_dump(
            {
                "version": 1,
                "catalogs": [
                    {"name": "fidonis", "type": "local", "path": str(catalog), "enabled": True}
                ],
            }
        ),
    )
    _write(
        catalog / "paperless" / "papaia-app.yaml",
        yaml.safe_dump(
            {
                "name": "paperless",
                "version": "1.2.0",
                "description": "Paperless-ngx",
                "components": [
                    _entry(version="3.1.0"),
                    _entry("postgres", name="PostgreSQL", image_var="PAPERLESS_DB_IMAGE",
                           version="18", pinned=False, **sidecar),
                ],
            }
        ),
    )
    # A catalog that is not ours: every entry has the wrong shape or tries something.
    _write(
        catalog / "hostile" / "papaia-app.yaml",
        yaml.safe_dump(
            {
                "name": "hostile",
                "version": "1.0.0",
                "components": [
                    {"id": "x", "name": "<b>bold</b>", "version": "1", "role": "primary",
                     "source": "javascript:alert(1)"},
                    "nonsense",
                    7,
                ],
            }
        ),
    )
    _write(
        managed / "papaia-app.yaml",
        yaml.safe_dump(
            {
                "name": "paperless",
                "version": "1.1.0",
                "components": [
                    _entry(version="3.0.5"),
                    _entry("postgres", name="PostgreSQL", image_var="PAPERLESS_DB_IMAGE",
                           version="18", pinned=False, **sidecar),
                ],
            }
        ),
    )
    _write(
        config / "manager" / "installed.yaml",
        yaml.safe_dump(
            {
                "version": 1,
                "addons": {
                    "paperless": {
                        "catalog": "fidonis",
                        "commit": "abc123",
                        "manifest_version": "1.1.0",
                        "installed_at": "2026-10-01T00:00:00+00:00",
                        "managed": True,
                    }
                },
            }
        ),
    )
    _write(
        config / "deployment.yaml",
        yaml.safe_dump({"addons": [{"name": "paperless", "path": str(managed), "active": True}]}),
    )
    _write(
        config / "addons" / "paperless" / ".env",
        "PAPERLESS_IMAGE=registry.example/paperless:3.0.7\n",
    )
    return {"config": config, "workspace": workspace}


@pytest.fixture
def app(world: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:
    async def no_docker(_settings: Settings) -> Any:
        return SimpleNamespace(running_projects=set())

    async def no_projects(_settings: Settings) -> set[str]:
        return set()

    monkeypatch.setattr(ui, "_load_stack_snapshot", no_docker)
    monkeypatch.setattr(api_addons, "_running_projects", no_projects)
    get_settings.cache_clear()
    settings = get_settings().model_copy(
        update={
            "papaia_config_dir": str(world["config"]),
            "papaia_workspace_dir": str(world["workspace"]),
        }
    )
    application = create_app()
    application.dependency_overrides[get_settings] = lambda: settings
    yield application
    get_settings.cache_clear()


def _admin(app: FastAPI, *, locale: str | None = None) -> TestClient:
    session: dict[str, Any] = {
        "user": {
            "sub": "11111111-1111-1111-1111-111111111111",
            "preferred_username": "tester",
            "roles": ["admin"],
            "exp": int(time.time()) + 3600,
        },
        "_csrf_token": _CSRF,
    }
    payload = base64.b64encode(json.dumps(session).encode())
    signed = TimestampSigner(get_settings().manager_session_secret).sign(payload).decode()
    browser = TestClient(app, follow_redirects=False)
    browser.cookies.set("papaia_manager_session", signed)
    if locale:
        browser.post("/account/language", data={"locale": locale, "csrf_token": _CSRF})
    return browser


def _text(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def test_the_gallery_card_names_the_primary_components_and_what_an_update_changes(
    app: FastAPI,
) -> None:
    response = _admin(app).get("/partials/addons")
    assert response.status_code == 200
    text = _text(response.text)
    assert "Paperless-ngx 3.0.5 → 3.1.0" in text
    assert "PostgreSQL" not in text  # a sidecar stays off the card


def test_the_detail_page_lists_every_component_with_its_state(app: FastAPI) -> None:
    response = _admin(app).get("/partials/addons/paperless")
    assert response.status_code == 200
    text = _text(response.text)
    assert "Components" in text
    assert "3.0.5 → 3.1.0" in text
    assert "overridden in .env: 3.0.7" in text
    assert "PostgreSQL" in text
    assert "floating" in text
    assert 'href="https://github.com/paperless-ngx/paperless-ngx"' in response.text


def test_the_detail_page_is_in_the_language_of_the_account(app: FastAPI) -> None:
    response = _admin(app, locale="de").get("/partials/addons/paperless")
    text = _text(response.text)
    assert "Komponenten" in text
    assert "Components" not in text
    assert "in .env überschrieben: 3.0.7" in text


def test_a_hostile_manifest_is_shown_inert(app: FastAPI) -> None:
    gallery = _admin(app).get("/partials/addons").text
    detail = _admin(app).get("/partials/addons/hostile").text
    for html in (gallery, detail):
        assert "<b>bold</b>" not in html
        assert "javascript:" not in html
    assert "&lt;b&gt;bold&lt;/b&gt;" in detail


def test_the_api_carries_the_components(app: FastAPI) -> None:
    listed = _admin(app).get("/api/v1/addons").json()
    paperless = next(a for a in listed if a["name"] == "paperless")
    by_id = {c["id"]: c for c in paperless["components"]}
    assert by_id["paperless-ngx"]["installed_version"] == "3.0.5"
    assert by_id["paperless-ngx"]["version"] == "3.1.0"
    assert by_id["paperless-ngx"]["changed"] is True
    assert by_id["paperless-ngx"]["override_version"] == "3.0.7"
    assert by_id["postgres"]["floating"] is True
    one = _admin(app).get("/api/v1/addons/paperless").json()
    assert one["components"] == paperless["components"]
