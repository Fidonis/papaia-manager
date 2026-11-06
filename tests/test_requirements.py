"""The add-ons an add-on requires (`requires.addons`): the states the manager shows and the
Install button it disables while papaia-ctl would refuse the install."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core import requirements, semver
from app.templating import templates

# ── semver.satisfies ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("version", "constraint", "expected"),
    [
        ("1.2.0", ">=1.0.0", True),
        ("0.9.0", ">=1.0.0", False),
        ("1.2.0", ">=1.0.0,<2.0.0", True),
        ("2.0.0", ">=1.0.0,<2.0.0", False),
        ("1.4.2", "^1.2.0", True),
        ("2.0.0", "^1.2.0", False),
        ("0.2.5", "^0.2.0", True),
        ("0.3.0", "^0.2.0", False),
        ("1.2.9", "~1.2.0", True),
        ("1.3.0", "~1.2.0", False),
        ("1.0.0", "1.0.0", True),
        ("1.0.1", "!=1.0.0", True),
    ],
)
def test_satisfies_reads_the_range_syntax_of_manifests(
    version: str, constraint: str, expected: bool
) -> None:
    assert semver.satisfies(version, constraint) is expected


@pytest.mark.parametrize(
    ("version", "constraint", "expected"),
    [
        ("1.3.0-rc.1", ">=1.2.0", False),
        ("2.0.0-rc.1", "^1.2.0", False),
        ("1.2.5-beta", "~1.2.0", False),
        ("1.3.0-rc.2", ">=1.3.0-rc.1", True),
        ("1.3.0-rc.1", "^1.3.0-rc.1", True),
        ("1.3.0", ">=1.3.0-rc.1", True),
    ],
)
def test_a_pre_release_only_meets_a_range_that_opts_in(
    version: str, constraint: str, expected: bool
) -> None:
    # papaia-ctl's rule: a pre-release needs a pre-release bound on the same triple.
    assert semver.satisfies(version, constraint) is expected


@pytest.mark.parametrize(
    ("version", "constraint"),
    [("main", ">=1.0.0"), ("1.0.0", ">=x"), ("1.0.0", ">=1.0.0,"), ("1.0.0", "")],
)
def test_satisfies_says_not_comparable_instead_of_guessing(version: str, constraint: str) -> None:
    assert semver.satisfies(version, constraint) is None


# ── requirement_states ────────────────────────────────────────────────────────

_MANIFEST = {
    "requires": {
        "addon_api": 4,
        "addons": ["wiki", {"name": "crm", "version": ">=1.2.0"}, {"name": "search"}],
    }
}


def _states(deployment: dict, installed: dict, workspace: Path) -> dict[str, dict]:
    return {
        s["name"]: s
        for s in requirements.requirement_states(_MANIFEST, deployment, installed, str(workspace))
    }


def test_each_requirement_gets_its_state(tmp_path: Path) -> None:
    deployment = {
        "wiki": {"name": "wiki", "active": True, "path": "/x/wiki"},
        "crm": {"name": "crm", "active": True, "path": "/x/crm"},
        "search": {"name": "search", "active": False, "path": "/x/search"},
    }
    installed = {"crm": SimpleNamespace(manifest_version="1.1.0")}

    states = _states(deployment, installed, tmp_path)

    assert states["wiki"]["state"] == requirements.ACTIVE
    assert states["crm"] == {
        "name": "crm",
        "range": ">=1.2.0",
        "state": requirements.TOO_OLD,
        "version": "1.1.0",
    }
    assert states["search"]["state"] == requirements.INACTIVE


def test_a_missing_requirement_blocks_and_an_unreadable_version_does_not(tmp_path: Path) -> None:
    deployment = {"wiki": {"name": "wiki", "active": True, "path": "/nowhere/wiki"}}
    deployment["crm"] = {"name": "crm", "active": True, "path": "/nowhere/crm"}

    states = _states(deployment, {}, tmp_path)

    assert states["search"]["state"] == requirements.MISSING
    # No installed.yaml record and no manifest at the path: papaia-ctl decides.
    assert states["crm"]["state"] == requirements.UNCHECKED
    assert not requirements.requirements_met(list(states.values()))
    del states["search"]
    assert requirements.requirements_met(list(states.values()))


def test_an_addon_installed_by_path_is_read_from_its_manifest(tmp_path: Path) -> None:
    addon = tmp_path / "papaia" / "addons" / "crm"
    addon.mkdir(parents=True)
    (addon / "papaia-app.yaml").write_text("name: crm\nversion: 1.3.0\n", encoding="utf-8")
    deployment = {"crm": {"name": "crm", "active": True, "path": "addons/crm"}}

    states = _states(deployment, {}, tmp_path)

    assert states["crm"]["state"] == requirements.ACTIVE
    assert states["crm"]["version"] == "1.3.0"


def test_the_registration_wins_over_installed_yaml(tmp_path: Path) -> None:
    # After `addon upgrade` on the host, installed.yaml still names the old version.
    addon = tmp_path / "crm"
    addon.mkdir()
    (addon / "papaia-app.yaml").write_text("name: crm\nversion: 1.3.0\n", encoding="utf-8")
    deployment = {"crm": {"name": "crm", "active": True, "path": str(addon)}}
    installed = {"crm": SimpleNamespace(manifest_version="1.1.0")}

    states = _states(deployment, installed, tmp_path)

    assert states["crm"]["state"] == requirements.ACTIVE
    assert states["crm"]["version"] == "1.3.0"


def test_deployment_yaml_version_comes_before_installed_yaml(tmp_path: Path) -> None:
    deployment = {"crm": {"name": "crm", "active": True, "path": "/nowhere", "version": "1.2.0"}}
    installed = {"crm": SimpleNamespace(manifest_version="1.1.0")}

    states = _states(deployment, installed, tmp_path)

    assert states["crm"]["state"] == requirements.ACTIVE
    assert states["crm"]["version"] == "1.2.0"


@pytest.mark.parametrize(
    ("env", "deployment", "mode"),
    [
        (None, {}, "enforce"),
        (None, {"mode": "dev"}, "warn"),
        (None, {"mode": "production"}, "enforce"),
        ("warn", {"mode": "production"}, "warn"),
        ("enforce", {"mode": "dev"}, "enforce"),
        ("bogus", {"mode": "dev"}, "warn"),
        (None, None, "enforce"),
    ],
)
def test_compat_mode_follows_papaia_ctl(
    monkeypatch: pytest.MonkeyPatch, env: str | None, deployment: dict | None, mode: str
) -> None:
    if env is None:
        monkeypatch.delenv("PAPAIA_COMPAT_MODE", raising=False)
    else:
        monkeypatch.setenv("PAPAIA_COMPAT_MODE", env)
    assert requirements.compat_mode(deployment) == mode


def test_view_says_whether_papaia_ctl_enforces(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("PAPAIA_COMPAT_MODE", raising=False)
    enforced = requirements.view(_MANIFEST, {}, {}, str(tmp_path), {"mode": "production"})
    warned = requirements.view(_MANIFEST, {}, {}, str(tmp_path), {"mode": "dev"})
    assert enforced["requirements_met"] is False and enforced["requirements_enforced"] is True
    assert warned["requirements_met"] is False and warned["requirements_enforced"] is False


def test_malformed_entries_are_left_to_papaia_ctl() -> None:
    manifest = {"requires": {"addons": ["", 42, {"version": ">=1"}, "wiki"]}}
    assert requirements.required_addons(manifest) == [("wiki", None)]
    assert requirements.required_addons({"requires": {"addons": "wiki"}}) == []
    assert requirements.required_addons({}) == []


# ── rendering ─────────────────────────────────────────────────────────────────


def _card(**addon: object) -> str:
    template = templates.env.from_string(
        '{% from "partials/_addon_controls.html" import primary_action, requirements_list %}'
        "{{ requirements_list(addon) }}{{ primary_action(addon, addon.status, 'addonAction') }}"
    )
    model = {"name": "demo", "catalog": "cat", "status": "available", **addon}
    return template.render(addon=model)


def test_an_unmet_requirement_disables_install_and_says_why() -> None:
    html = _card(
        requirements=[{"name": "wiki", "range": None, "state": "missing", "version": None}],
        requirements_met=False,
    )
    assert 'href="/addons/wiki"' in html
    assert "is not installed" in html
    assert "Install the required add-ons first" in html
    assert "installModal" not in html


def test_warn_mode_shows_the_badges_and_keeps_the_install_button() -> None:
    html = _card(
        requirements=[{"name": "wiki", "range": None, "state": "missing", "version": None}],
        requirements_met=False,
        requirements_enforced=False,
    )
    assert "is not installed" in html
    assert "Install the required add-ons first" not in html
    assert "installModal" in html


def test_met_requirements_keep_the_install_button() -> None:
    html = _card(
        requirements=[{"name": "wiki", "range": ">=1.0.0", "state": "active", "version": "1.0.0"}],
        requirements_met=True,
    )
    assert "wiki &gt;=1.0.0" in html or "wiki >=1.0.0" in html
    assert "installModal" in html


def test_an_addon_without_requirements_looks_as_before() -> None:
    html = _card(requirements=[], requirements_met=True)
    assert "Requires" not in html
    assert "installModal" in html
