"""End-to-end tests for the dashboard tiles API.

The model layer is covered in `test_tiles.py` and the access tiers in
`test_access_control.py`. What this module adds is the write path through the
real router: the conflict check, the server-side validation that decides
whether a file is written at all, and the promise that placeholder values stay
on the server.

Each test gets a configuration directory of its own, injected by overriding the
settings dependency rather than by moving the process environment -- the
environment is read once at import time and shared with every other module that
stands up the app.
"""
from __future__ import annotations

import base64
import json
import os
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

_CONFIG_DIR = tempfile.mkdtemp(prefix="papaia-tiles-config-")
_WORKSPACE_DIR = tempfile.mkdtemp(prefix="papaia-tiles-workspace-")

# `setdefault`, not `update`: another test module may have imported first and
# pointed the process at its own directories. Every test here works through the
# dependency override below, so whose values win does not matter.
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
from app.main import create_app  # noqa: E402

_CSRF = "test-csrf-token-value"

# A secret sitting in the same file as the placeholder values, because that is
# the situation the API has to be safe in: one .env holding both.
_CORE_ENV = "PAPAIA_HOST=https://papaia.test\nKEYCLOAK_ADMIN_PASSWORD=super-secret-value\n"


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "config"
    (directory / "manager").mkdir(parents=True)
    (directory / ".env").write_text(_CORE_ENV, encoding="utf-8")
    return directory


@pytest.fixture
def client(config_dir: Path) -> Iterator[TestClient]:
    get_settings.cache_clear()
    settings = get_settings().model_copy(update={"papaia_config_dir": str(config_dir)})

    # No `with` block: skipping the lifespan keeps the job-queue worker thread
    # and the workspace handshake out of these tests.
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


def _headers() -> dict[str, str]:
    return {"X-CSRF-Token": _CSRF}


def _tiles(client: TestClient) -> dict[str, Any]:
    response = _admin(client).get("/api/v1/tiles")
    assert response.status_code == 200
    return response.json()


def _draft(revision: str, *groups: dict[str, Any]) -> dict[str, Any]:
    return {"revision": revision, "version": 1, "groups": list(groups)}


def _group(name: str, *tiles: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "tiles": list(tiles)}


def _tile(name: str, href: str, **overrides: Any) -> dict[str, Any]:
    tile = {"name": name, "href": href, "description": "", "icon": None, "visibility": "all"}
    tile.update(overrides)
    return tile


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def test_the_editor_is_handed_the_configuration_unfiltered(client: TestClient) -> None:
    """Restricted tiles included -- the editor has to be able to edit them."""
    body = _tiles(client)

    names = [tile["name"] for group in body["groups"] for tile in group["tiles"]]
    assert "LibreChat" in names
    assert "Keycloak" in names


def test_reading_seeds_the_file_and_returns_its_revision(
    client: TestClient, config_dir: Path
) -> None:
    body = _tiles(client)

    assert (config_dir / "manager" / "tiles.yaml").exists()
    assert body["revision"]


def test_link_keys_are_names_and_the_env_values_stay_behind(client: TestClient) -> None:
    """The core .env holds every stack secret; only key names may travel."""
    response = _admin(client).get("/api/v1/tiles")
    body = response.json()

    assert body["link_keys"] == ["PAPAIA_HOST"]
    assert "super-secret-value" not in response.text
    assert "KEYCLOAK_ADMIN_PASSWORD" not in response.text


def test_hrefs_are_returned_unresolved(client: TestClient) -> None:
    """The editor edits the placeholder, not the address it expands to."""
    body = _tiles(client)
    hrefs = [tile["href"] for group in body["groups"] for tile in group["tiles"]]

    assert any(href.startswith("{{PAPAIA_HOST}}") for href in hrefs)


def test_a_file_that_cannot_be_parsed_is_reported_not_raised(
    client: TestClient, config_dir: Path
) -> None:
    (config_dir / "manager" / "tiles.yaml").write_text("groups: [unclosed", encoding="utf-8")

    response = _admin(client).get("/api/v1/tiles")

    assert response.status_code == 409
    assert "tiles.yaml" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def test_a_saved_draft_comes_back_on_the_next_read(
    client: TestClient, config_dir: Path
) -> None:
    revision = _tiles(client)["revision"]

    response = _admin(client).put(
        "/api/v1/tiles",
        headers=_headers(),
        json=_draft(
            revision,
            _group("Tools", _tile("Wiki", "https://wiki.test", description="The wiki")),
        ),
    )

    assert response.status_code == 200
    assert _tiles(client)["groups"] == [
        {
            "name": "Tools",
            "tiles": [
                {
                    "name": "Wiki",
                    "href": "https://wiki.test",
                    "description": "The wiki",
                    "icon": None,
                    "visibility": "all",
                }
            ],
        }
    ]

    on_disk = yaml.safe_load((config_dir / "manager" / "tiles.yaml").read_text(encoding="utf-8"))
    assert on_disk["groups"][0]["name"] == "Tools"


def test_the_response_carries_the_revision_of_what_was_just_written(
    client: TestClient,
) -> None:
    revision = _tiles(client)["revision"]

    saved = _admin(client).put(
        "/api/v1/tiles",
        headers=_headers(),
        json=_draft(revision, _group("Tools", _tile("Wiki", "https://wiki.test"))),
    ).json()

    assert saved["revision"] != revision
    assert saved["revision"] == _tiles(client)["revision"]


def test_a_second_save_from_the_same_read_is_refused(client: TestClient) -> None:
    """Two editors open at once: the one working from the older file loses."""
    revision = _tiles(client)["revision"]
    draft = _draft(revision, _group("Tools", _tile("Wiki", "https://wiki.test")))

    assert _admin(client).put("/api/v1/tiles", headers=_headers(), json=draft).status_code == 200

    stale = _admin(client).put("/api/v1/tiles", headers=_headers(), json=draft)

    assert stale.status_code == 409
    assert "reload" in stale.json()["detail"]


def test_an_edit_made_on_the_host_is_not_overwritten(
    client: TestClient, config_dir: Path
) -> None:
    revision = _tiles(client)["revision"]
    (config_dir / "manager" / "tiles.yaml").write_text(
        "version: 1\ngroups:\n- name: Hand edited\n  tiles: []\n", encoding="utf-8"
    )

    response = _admin(client).put(
        "/api/v1/tiles",
        headers=_headers(),
        json=_draft(revision, _group("From the editor")),
    )

    assert response.status_code == 409
    assert "Hand edited" in (config_dir / "manager" / "tiles.yaml").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "href",
    ["javascript:alert(1)", "data:text/html,<script>", "ftp://files.test"],
)
def test_a_link_the_dashboard_would_drop_is_refused(client: TestClient, href: str) -> None:
    """Refused rather than accepted and then silently not rendered."""
    revision = _tiles(client)["revision"]

    response = _admin(client).put(
        "/api/v1/tiles",
        headers=_headers(),
        json=_draft(revision, _group("Tools", _tile("Bad", href))),
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["field"] == "href"


def test_an_unresolvable_placeholder_is_refused_and_named(client: TestClient) -> None:
    revision = _tiles(client)["revision"]

    response = _admin(client).put(
        "/api/v1/tiles",
        headers=_headers(),
        json=_draft(revision, _group("Tools", _tile("Typo", "{{PAPAIA_HSOT}}:8000"))),
    )

    assert response.status_code == 422
    assert "PAPAIA_HSOT" in response.json()["detail"][0]["message"]


def test_a_problem_is_addressed_to_the_tile_it_belongs_to(client: TestClient) -> None:
    revision = _tiles(client)["revision"]

    response = _admin(client).put(
        "/api/v1/tiles",
        headers=_headers(),
        json=_draft(
            revision,
            _group("Fine", _tile("Ok", "/ok")),
            _group("Broken", _tile("Ok", "/ok"), _tile("Bad", "javascript:alert(1)")),
        ),
    )

    problem = response.json()["detail"][0]
    assert (problem["group"], problem["tile"]) == (1, 1)


def test_duplicate_group_names_are_refused(client: TestClient) -> None:
    revision = _tiles(client)["revision"]

    response = _admin(client).put(
        "/api/v1/tiles",
        headers=_headers(),
        json=_draft(revision, _group("Tools"), _group("tools")),
    )

    assert response.status_code == 422


def test_a_rejected_draft_leaves_the_file_alone(
    client: TestClient, config_dir: Path
) -> None:
    revision = _tiles(client)["revision"]
    before = (config_dir / "manager" / "tiles.yaml").read_text(encoding="utf-8")

    _admin(client).put(
        "/api/v1/tiles",
        headers=_headers(),
        json=_draft(revision, _group("Tools", _tile("Bad", "javascript:alert(1)"))),
    )

    assert (config_dir / "manager" / "tiles.yaml").read_text(encoding="utf-8") == before


def test_an_empty_group_survives_a_save(client: TestClient) -> None:
    """`visible_groups` drops it from the dashboard; the file must keep it."""
    revision = _tiles(client)["revision"]

    _admin(client).put(
        "/api/v1/tiles", headers=_headers(), json=_draft(revision, _group("Not filled in yet"))
    )

    assert [group["name"] for group in _tiles(client)["groups"]] == ["Not filled in yet"]


def test_a_save_is_written_to_the_audit_log(client: TestClient, config_dir: Path) -> None:
    revision = _tiles(client)["revision"]

    _admin(client).put(
        "/api/v1/tiles",
        headers=_headers(),
        json=_draft(revision, _group("Tools", _tile("Wiki", "https://wiki.test"))),
    )

    entries = [
        json.loads(line)
        for line in (config_dir / "manager" / "audit.log").read_text(encoding="utf-8").splitlines()
    ]
    assert entries[-1]["action"] == "tiles-save"
    assert entries[-1]["target"] == "tiles.yaml"
    assert entries[-1]["params"]["tiles"] == 1


# ---------------------------------------------------------------------------
# Raw YAML
# ---------------------------------------------------------------------------


def test_the_raw_document_is_returned_as_text(client: TestClient) -> None:
    body = _admin(client).get("/api/v1/tiles/raw").json()

    assert "LibreChat" in body["yaml"]
    assert body["revision"]


def test_a_raw_save_replaces_the_file(client: TestClient) -> None:
    revision = _admin(client).get("/api/v1/tiles/raw").json()["revision"]

    response = _admin(client).put(
        "/api/v1/tiles/raw",
        headers=_headers(),
        json={
            "revision": revision,
            "yaml": "version: 1\ngroups:\n- name: Typed by hand\n  tiles:\n"
            "  - name: Wiki\n    href: https://wiki.test\n",
        },
    )

    assert response.status_code == 200
    assert [group["name"] for group in _tiles(client)["groups"]] == ["Typed by hand"]


def test_unparseable_yaml_is_refused_before_it_reaches_the_file(
    client: TestClient, config_dir: Path
) -> None:
    revision = _admin(client).get("/api/v1/tiles/raw").json()["revision"]
    before = (config_dir / "manager" / "tiles.yaml").read_text(encoding="utf-8")

    response = _admin(client).put(
        "/api/v1/tiles/raw",
        headers=_headers(),
        json={"revision": revision, "yaml": "groups: [unclosed"},
    )

    assert response.status_code == 400
    assert (config_dir / "manager" / "tiles.yaml").read_text(encoding="utf-8") == before


def test_the_raw_editor_cannot_write_a_link_the_dashboard_would_drop(
    client: TestClient,
) -> None:
    revision = _admin(client).get("/api/v1/tiles/raw").json()["revision"]

    response = _admin(client).put(
        "/api/v1/tiles/raw",
        headers=_headers(),
        json={
            "revision": revision,
            "yaml": "version: 1\ngroups:\n- name: G\n  tiles:\n"
            "  - name: Bad\n    href: javascript:alert(1)\n",
        },
    )

    assert response.status_code == 422


def test_a_stale_raw_save_is_refused(client: TestClient) -> None:
    revision = _admin(client).get("/api/v1/tiles/raw").json()["revision"]
    document = {
        "revision": revision,
        "yaml": "version: 1\ngroups:\n- name: One\n  tiles: []\n",
    }

    assert _admin(client).put(
        "/api/v1/tiles/raw", headers=_headers(), json=document
    ).status_code == 200
    assert _admin(client).put(
        "/api/v1/tiles/raw", headers=_headers(), json=document
    ).status_code == 409


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def test_resolution_expands_a_placeholder_without_disclosing_the_file(
    client: TestClient,
) -> None:
    response = _admin(client).post(
        "/api/v1/tiles/resolve",
        headers=_headers(),
        json={"values": ["{{PAPAIA_HOST}}:8000"]},
    )

    assert response.json() == [
        {
            "input": "{{PAPAIA_HOST}}:8000",
            "resolved": "https://papaia.test:8000",
            "ok": True,
            "reason": None,
        }
    ]


def test_resolution_reports_why_a_link_would_be_dropped(client: TestClient) -> None:
    response = _admin(client).post(
        "/api/v1/tiles/resolve",
        headers=_headers(),
        json={"values": ["{{NOPE}}:8000", "javascript:alert(1)"]},
    )

    results = response.json()
    assert [result["ok"] for result in results] == [False, False]
    assert "NOPE" in results[0]["reason"]


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def test_a_write_without_the_csrf_header_is_refused(client: TestClient) -> None:
    revision = _tiles(client)["revision"]

    response = _admin(client).put(
        "/api/v1/tiles", json=_draft(revision, _group("Tools", _tile("Wiki", "https://wiki.test")))
    )

    assert response.status_code == 403


def test_a_dashboard_only_account_cannot_write(client: TestClient) -> None:
    """The gallery is open to every role; this is where the tiers part."""
    revision = _tiles(client)["revision"]

    response = _as(client, "user").put(
        "/api/v1/tiles",
        headers=_headers(),
        json=_draft(revision, _group("Tools", _tile("Wiki", "https://wiki.test"))),
    )

    assert response.status_code == 403


# ---------------------------------------------------------------------------
# The editor partial
# ---------------------------------------------------------------------------


def test_the_editor_partial_carries_the_draft_and_the_revision(client: TestClient) -> None:
    body = _admin(client).get("/partials/tiles/edit").text

    assert "Keycloak" in body, "the editor renders restricted tiles too"
    assert "Save changes" in body


def test_the_editor_shows_a_group_the_dashboard_hides(client: TestClient) -> None:
    """An empty group has to stay visible while it is being filled in."""
    revision = _tiles(client)["revision"]
    _admin(client).put(
        "/api/v1/tiles", headers=_headers(), json=_draft(revision, _group("Brand new"))
    )

    assert "Brand new" in _admin(client).get("/partials/tiles/edit").text
    assert "Brand new" not in _admin(client).get("/partials/tiles").text


def test_a_broken_file_does_not_take_the_dashboard_down(
    client: TestClient, config_dir: Path
) -> None:
    (config_dir / "manager" / "tiles.yaml").write_text("groups: [unclosed", encoding="utf-8")

    response = _admin(client).get("/partials/tiles")

    assert response.status_code == 200
    assert "could not be read" in response.text
