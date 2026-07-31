"""Unit tests for the declared target state read out of the Compose files."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.inventory import (
    active_profiles,
    addon_inventory,
    core_inventory,
    module_display_name,
)

# Mirrors the shape of the shipped core: a root file with an `include:` list and
# fragments whose services carry one profile plus the two `de.fidonis.*` labels.
# `oauth2-proxy` is here because it is the one core service whose module name
# does not follow its profile name -- exactly the case that rules out deriving
# the mapping instead of reading it.
_ROOT = """\
include:
  - path: ./infra/oauth2-proxy/docker-compose.yml
  - path: ./ai/librechat/docker-compose.yml
  - path: ./services/searxng/docker-compose.yml
networks:
  papaia-net:
    driver: bridge
"""

_OAUTH2 = """\
services:
  oauth2-proxy:
    image: quay.io/oauth2-proxy/oauth2-proxy
    labels:
      de.fidonis.module: papaia-auth
      de.fidonis.role: forward-auth
    profiles: [oauth2-proxy]
"""

_LIBRECHAT = """\
services:
  librechat:
    labels:
      de.fidonis.module: papaia-librechat
      de.fidonis.role: chat-interface
    profiles: [librechat]
  librechat-mongodb:
    labels:
      - de.fidonis.module=papaia-librechat
      - de.fidonis.role=database
    profiles: [librechat]
  librechat-unlabelled:
    profiles: [librechat]
"""

_SEARXNG = """\
services:
  searxng:
    labels:
      de.fidonis.module: papaia-searxng
      de.fidonis.role: search-engine
    profiles: [librechat-websearch]
"""


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A workspace laid out the way the path-parity mount presents it."""
    src = tmp_path / "papaia" / "src"
    for relative, content in (
        ("docker-compose.yml", _ROOT),
        ("infra/oauth2-proxy/docker-compose.yml", _OAUTH2),
        ("ai/librechat/docker-compose.yml", _LIBRECHAT),
        ("services/searxng/docker-compose.yml", _SEARXNG),
    ):
        path = src / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


def test_profiles_come_from_the_core_env(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "COMPOSE_PROJECT_NAME=papaia\nCOMPOSE_PROFILES=keycloak, librechat ,nginx\n",
        encoding="utf-8",
    )
    assert active_profiles(str(tmp_path)) == {"keycloak", "librechat", "nginx"}


def test_a_missing_env_file_enables_nothing(tmp_path: Path) -> None:
    # Compose would start nothing either, so an empty target state is the honest
    # answer rather than a guess at what should be up.
    assert active_profiles(str(tmp_path)) == set()


def test_an_empty_profile_list_enables_nothing(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("COMPOSE_PROFILES=\n", encoding="utf-8")
    assert active_profiles(str(tmp_path)) == set()


# ---------------------------------------------------------------------------
# Core inventory
# ---------------------------------------------------------------------------


def test_only_services_of_enabled_profiles_are_expected(workspace: Path) -> None:
    services = {e.service for e in core_inventory(str(workspace), {"librechat"})}
    assert services == {"librechat", "librechat-mongodb", "librechat-unlabelled"}


def test_the_module_name_is_read_not_derived_from_the_profile(workspace: Path) -> None:
    expected = core_inventory(str(workspace), {"oauth2-proxy"})
    assert [(e.service, e.module, e.role) for e in expected] == [
        ("oauth2-proxy", "auth", "forward-auth")
    ]


def test_one_profile_can_span_several_modules(workspace: Path) -> None:
    # `librechat-websearch` brings up four modules in the real stack; the point
    # is that the profile name says nothing about how many.
    both = core_inventory(str(workspace), {"librechat", "librechat-websearch"})
    assert {e.module for e in both} == {"librechat", "searxng", "other"}


def test_the_product_prefix_is_dropped(workspace: Path) -> None:
    # Both halves of the page group on this value, so they have to derive it the
    # same way -- hence one shared function rather than two `removeprefix` calls.
    assert {e.module for e in core_inventory(str(workspace), {"librechat-websearch"})} == {
        "searxng"
    }
    assert module_display_name("papaia-librechat") == "librechat"
    assert module_display_name("") == "other"


def test_labels_in_list_form_are_read_too(workspace: Path) -> None:
    mongodb = next(
        e for e in core_inventory(str(workspace), {"librechat"}) if e.service == "librechat-mongodb"
    )
    assert (mongodb.module, mongodb.role) == ("librechat", "database")


def test_a_core_service_without_labels_lands_in_the_ungrouped_bucket(workspace: Path) -> None:
    unlabelled = next(
        e
        for e in core_inventory(str(workspace), {"librechat"})
        if e.service == "librechat-unlabelled"
    )
    assert (unlabelled.module, unlabelled.role) == ("other", "")


def test_no_enabled_profiles_expects_nothing(workspace: Path) -> None:
    assert core_inventory(str(workspace), set()) == []


def test_an_absent_workspace_yields_no_target_state(tmp_path: Path) -> None:
    # The workspace is a bind mount that can be missing in tests or mid-restore.
    # Losing the target state is acceptable; losing the page is not.
    assert core_inventory(str(tmp_path), {"librechat"}) == []


def test_a_broken_root_file_yields_no_target_state(tmp_path: Path) -> None:
    root = tmp_path / "papaia" / "src" / "docker-compose.yml"
    root.parent.mkdir(parents=True)
    root.write_text("include: [ unterminated\n", encoding="utf-8")
    assert core_inventory(str(tmp_path), {"librechat"}) == []


def test_an_unreadable_fragment_does_not_take_the_others_with_it(workspace: Path) -> None:
    (workspace / "papaia" / "src" / "ai" / "librechat" / "docker-compose.yml").unlink()
    assert [e.service for e in core_inventory(str(workspace), {"oauth2-proxy", "librechat"})] == [
        "oauth2-proxy"
    ]


def test_compose_files_outside_the_include_list_are_ignored(workspace: Path) -> None:
    # An add-on checked out inside the workspace must not leak into the core
    # target state, which is why the include list is followed rather than globbed.
    stray = workspace / "papaia" / "src" / "stray" / "docker-compose.yml"
    stray.parent.mkdir(parents=True)
    stray.write_text(
        "services:\n  stray:\n    profiles: [librechat]\n", encoding="utf-8"
    )
    assert all(e.service != "stray" for e in core_inventory(str(workspace), {"librechat"}))


# ---------------------------------------------------------------------------
# Add-on inventory
# ---------------------------------------------------------------------------


def _addon(path: Path, body: str) -> str:
    path.mkdir(parents=True, exist_ok=True)
    (path / "docker-compose.yml").write_text(body, encoding="utf-8")
    return str(path)


def test_every_addon_service_is_expected(tmp_path: Path) -> None:
    # Add-on fragments carry no `profiles` -- an active add-on is expected whole.
    path = _addon(
        tmp_path / "paperless",
        "services:\n"
        "  paperless:\n"
        "    labels:\n"
        "      de.fidonis.module: papaia-paperless\n"
        "      de.fidonis.role: webserver\n"
        "  paperless-db:\n"
        "    labels:\n"
        "      de.fidonis.module: papaia-paperless\n"
        "      de.fidonis.role: database\n",
    )
    expected = addon_inventory(path, fallback_module="paperless")

    assert [(e.service, e.module, e.role) for e in expected] == [
        ("paperless", "paperless", "webserver"),
        ("paperless-db", "paperless", "database"),
    ]


def test_an_addon_without_the_module_label_falls_back_to_its_name(tmp_path: Path) -> None:
    # The label is not part of the add-on contract: neither ADDON_API nor the
    # core docs require it, so a third-party add-on may well omit it. Without the
    # fallback it would read as a stray container instead of as itself.
    path = _addon(tmp_path / "acme-crm", "services:\n  web:\n    image: acme/crm\n")
    expected = addon_inventory(path, fallback_module="acme-crm")

    assert [(e.service, e.module, e.role) for e in expected] == [("web", "acme-crm", "")]


def test_an_addon_without_a_compose_file_expects_nothing(tmp_path: Path) -> None:
    assert addon_inventory(str(tmp_path / "gone"), fallback_module="gone") == []
