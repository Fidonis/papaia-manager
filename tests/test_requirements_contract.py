"""The manager's `requires.addons` verdict against the core's real code: what the add-on page
shows is what papaia-ctl decides.

papaia-ctl stays the gate; the manager only mirrors it before the click. These tests load the
core's `compat`, `semver` and `cli_addon` and compare range checks, the parsed field and the
version each side reads for an active add-on. A drift between the two is a green badge on an
install papaia-ctl refuses, or a disabled button for one it would run.

They need the core's checkout and are skipped without it. Point `PAPAIA_SRC` at a checkout of
`Fidonis/papaia` that has `requires.addons` (Fidonis/papaia#312). Repeat them when either side's
range syntax, field format or version source changes.
"""

from __future__ import annotations

import importlib
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from app.core import requirements, semver

_SRC = os.environ.get("PAPAIA_SRC", "")


def _core_has_required_addons() -> bool:
    source = Path(_SRC) / "tools" / "lib" / "compat.py"
    if not (_SRC and source.is_file()):
        return False
    return "def evaluate_required_addons" in source.read_text(encoding="utf-8")


pytestmark = pytest.mark.skipif(
    not _core_has_required_addons(),
    reason="PAPAIA_SRC does not point at a checkout of the core that has requires.addons",
)

_VERSIONS = [
    "0.9.0", "1.0.0", "1.2.0", "1.2.5", "1.2.5-beta", "1.3.0-rc.1", "1.3.0-rc.2", "1.3.0",
    "1.9.9", "2.0.0-rc.1", "2.0.0", "0.2.5", "0.0.3",
]  # fmt: skip

_RANGES = [
    ">=1.2.0", ">1.2.0", "<=1.2.5", "<2.0.0", "==1.3.0", "!=1.3.0", "1.2.0", "^1.2.0",
    "^0.2.0", "^0.0.3", "~1.2.0", ">=1.0.0,<2.0.0", ">=1.3.0-rc.1", "^1.3.0-rc.1",
    ">=1.0.0,", ",>=1.0.0", ">=x", "",
]  # fmt: skip


@pytest.fixture(scope="module")
def core() -> Iterator[dict[str, ModuleType]]:
    tools = str(Path(_SRC) / "tools")
    sys.path.insert(0, tools)
    try:
        yield {
            "semver": importlib.import_module("lib.semver"),
            "compat": importlib.import_module("lib.compat"),
            "cli_addon": importlib.import_module("lib.cli_addon"),
        }
    finally:
        sys.path.remove(tools)


def _core_satisfies(core: dict[str, ModuleType], version: str, constraint: str) -> bool | None:
    try:
        return bool(core["semver"].satisfies(version, constraint))
    except ValueError:
        return None


@pytest.mark.parametrize("constraint", _RANGES)
def test_range_checks_match_the_core(core: dict[str, ModuleType], constraint: str) -> None:
    for version in _VERSIONS:
        expected = _core_satisfies(core, version, constraint)
        # A range the core rejects is "not checked" here; papaia-ctl reports it on install.
        assert semver.satisfies(version, constraint) == expected, (version, constraint)


def test_the_parsed_field_matches_the_core(core: dict[str, ModuleType]) -> None:
    manifest = {
        "requires": {
            "addons": ["wiki", {"name": "crm", "version": "^1.2.0"}, {"name": "search"}]
        }
    }
    assert requirements.required_addons(manifest) == core["compat"].required_addons(manifest)
    assert requirements.required_addons({}) == core["compat"].required_addons({})


def test_the_version_of_an_active_addon_matches_the_core(
    core: dict[str, ModuleType], tmp_path: Path
) -> None:
    repo = tmp_path / "papaia"
    for name, version in (("crm", "1.3.0"), ("wiki", None)):
        addon = repo / "addons" / name
        addon.mkdir(parents=True)
        text = f"name: {name}\n" + (f"version: {version}\n" if version else "")
        (addon / "papaia-app.yaml").write_text(text, encoding="utf-8")
    entries = [
        # Manifest at the registered path wins over everything.
        {"name": "crm", "active": True, "path": "addons/crm", "version": "1.1.0"},
        # Manifest without a version: deployment.yaml's version.
        {"name": "wiki", "active": True, "path": "addons/wiki", "version": "1.2.0"},
        # No manifest at all and no recorded version.
        {"name": "search", "active": True, "path": "addons/search"},
    ]
    core_versions = core["cli_addon"].active_addon_versions({"addons": entries}, repo)
    for entry in entries:
        manager = requirements._deployed_version(entry, str(tmp_path))
        assert manager == core_versions[entry["name"]], entry["name"]


def test_the_dependents_match_the_core(core: dict[str, ModuleType], tmp_path: Path) -> None:
    repo = tmp_path / "papaia"
    manifests = {
        "wiki": {"name": "wiki", "version": "1.0.0"},
        "demo": {"name": "demo", "requires": {"addons": ["wiki", {"name": "crm"}]}},
        "other": {"name": "other", "requires": {"addons": [{"name": "wiki", "version": "^1"}]}},
        "paused": {"name": "paused", "requires": {"addons": ["wiki"]}},
        "broken": {"name": "broken", "requires": {"addons": ["wiki", 42]}},
    }
    entries = []
    for name, manifest in manifests.items():
        addon = repo / "addons" / name
        addon.mkdir(parents=True)
        (addon / "papaia-app.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
        entries.append({"name": name, "active": name != "paused", "path": f"addons/{name}"})
    deployed = {"addons": entries}
    by_name = {e["name"]: e for e in entries}
    for name in ("wiki", "crm", "demo"):
        expected = sorted(core["cli_addon"].dependent_addons(name, deployed, repo))
        assert requirements.dependents(name, by_name, str(tmp_path)) == expected, name
