"""Release 1.8.0: one version everywhere it is written, a changelog entry and upgrade notes
for an installation that comes from manager 1.6.0 (papAIa core 1.8.0) or 1.7.0.

The tag itself is not part of the tree; these tests pin what the tag has to agree with: the
package version (which the image label and the release take), the lock file and the German
catalogue's header.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

from app.core import papaia_lib

ROOT = Path(__file__).resolve().parents[1]
VERSION = "1.8.0"


def _changelog() -> str:
    return (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def _entry(version: str) -> str:
    """The changelog entry of `version`, up to the next entry."""
    text = _changelog()
    start = re.search(rf"^## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}$", text, re.M)
    assert start is not None, f"no entry [{version}] with a date"
    following = re.search(r"^## \[", text[start.end():], re.M)
    end = start.end() + following.start() if following else len(text)
    return text[start.end():end]


def _section(entry: str, heading: str) -> str:
    start = re.search(rf"^### {re.escape(heading)}$", entry, re.M)
    assert start is not None, f"no section '### {heading}'"
    following = re.search(r"^### ", entry[start.end():], re.M)
    end = start.end() + following.start() if following else len(entry)
    return entry[start.end():end]


def test_the_package_is_version_1_8_0() -> None:
    project = tomllib.loads((ROOT / "src" / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["version"] == VERSION


def test_the_lock_file_names_the_same_version() -> None:
    lock = (ROOT / "src" / "uv.lock").read_text(encoding="utf-8")
    entry = re.search(r'\[\[package\]\]\nname = "papaia-manager"\nversion = "([^"]+)"', lock)
    assert entry is not None
    assert entry.group(1) == VERSION


def test_the_german_catalogue_names_the_same_version() -> None:
    po = (ROOT / "src" / "app" / "locale" / "de" / "LC_MESSAGES" / "messages.po")
    assert f'"Project-Id-Version: papaia-manager {VERSION}\\n"' in po.read_text(encoding="utf-8")


def test_the_changelog_has_a_dated_1_8_0_entry_above_1_7_0() -> None:
    text = _changelog()
    assert text.index(f"## [{VERSION}]") < text.index("## [1.7.0]")
    assert text.index("## [Unreleased]") < text.index(f"## [{VERSION}]")


def test_the_entry_names_every_change_of_the_release() -> None:
    entry = _entry(VERSION)
    for needle in (
        # MGR-001 shared frame, MGR-002 preferences, MGR-004 glossary and administration map
        "papaia-kit",
        "module switcher",
        "PREFS_URL",
        "PREFS_COOKIE_DOMAIN",
        "glossary",
        # MGR-005 installation settings
        "Settings > Installation",
        "approvals.four_eyes",
        # MGR-006 module catalog, audit rows, delegations
        "/modules",
        "papaia.audit/1",
        "AUDIT_OUTBOX_PATH",
        "/api/v1/delegations",
        # MGR-010 Docker access
        "papaia-docker-gate",
        "DOCKER_GATE_URL",
        # MGR-012 and MGR-030 kit 0.1.0, the first public kit release, and the design check
        "papaia-kit` 0.1.0",
        "/_kit/specimen",
        # MGR-003 and MGR-028 content security policy, enforced
        "Content-Security-Policy",
        "Content security policy, enforced",
    ):
        assert needle in entry, needle


def test_the_entry_says_what_needs_core_1_10_0() -> None:
    notes = _section(_entry(VERSION), "Notes")
    assert "papAIa core 1.10.0" in notes


def test_the_upgrade_notes_walk_an_installation_from_1_6_0() -> None:
    upgrade = _section(_entry(VERSION), "Upgrading from 1.6.0 or 1.7.0")
    for needle in (
        # The manager is not upgraded on its own: the core pins it.
        "papaia-ctl upgrade --version=1.10.0",
        "papaia-ctl addon upgrade paperless",
        "restore point",
        # What an operator has to change on a manager run from docker/docker-compose.yml.
        "DOCKER_GID",
        "DOCKER_GATE_URL",
        # The data the manager keeps, and the one value that moves.
        "manager/settings.yaml",
        "manager/migrations.json",
        "Settings > Installation",
        "SVG",
        "manager/preferences.yaml",
        # And the way back.
        "papaia-ctl rollback",
    ):
        assert needle in upgrade, needle


def test_the_manager_warns_below_core_1_10_0() -> None:
    assert papaia_lib.SUPPORTED_CORE_MIN == (1, 10, 0)
