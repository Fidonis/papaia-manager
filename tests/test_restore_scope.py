"""What a restore point offers to restore, and what choosing it would cost.

Pure functions over one manifest dict: no Docker, no subprocess, no filesystem.
The presentation policy is under test as much as the grouping is -- which volumes
are offered, which are advanced, which are not offered at all, and which one
cannot be restored on its own -- because that policy is the difference between a
picker an operator can use during an incident and thirteen identical checkboxes.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.core import restore_scope


def _artifact(
    kind: str,
    archive: str,
    target: str,
    *,
    owner: str = "core",
    module: str = "",
    profiles: list[str] | None = None,
    project: str = "papaia",
) -> dict[str, Any]:
    return {
        "kind": kind,
        "archive": archive,
        "target": target,
        "owner": owner,
        # An add-on's compose project is its directory basename, not the core's
        # -- which is why the fixture has to be able to say so.
        "project": project,
        "module": module,
        "services": [],
        "profiles": profiles or [],
    }


def _manifest(*artifacts: dict[str, Any], version: int = 2) -> dict[str, Any]:
    return {
        "version": version,
        "id": "2026-07-30_10-19-38",
        "core_project": "papaia",
        "artifacts": list(artifacts),
    }


def _full_manifest() -> dict[str, Any]:
    """Shaped like a real snapshot: the config dir, three core modules including
    the volumes that are deliberately not offered, and one add-on."""
    return _manifest(
        _artifact("configdir", "papaia-config.tar.gz", "/srv/papaia-config"),
        _artifact(
            "volume", "volumes/papaia_keycloak-postgresql.tar.gz",
            "papaia_keycloak-postgresql", module="keycloak", profiles=["keycloak"],
        ),
        _artifact(
            "volume", "volumes/papaia_librechat-mongodb.tar.gz",
            "papaia_librechat-mongodb", module="librechat", profiles=["librechat"],
        ),
        _artifact(
            "volume", "volumes/papaia_librechat-meilisearch.tar.gz",
            "papaia_librechat-meilisearch", module="librechat", profiles=["librechat"],
        ),
        _artifact(
            "volume", "volumes/papaia_librechat-logs.tar.gz",
            "papaia_librechat-logs", module="librechat", profiles=["librechat"],
        ),
        _artifact(
            "volume", "volumes/papaia_searxng_config.tar.gz",
            "papaia_searxng_config", module="searxng", profiles=["librechat-websearch"],
        ),
        _artifact(
            "volume", "volumes/papaia_searxng_data.tar.gz",
            "papaia_searxng_data", module="searxng", profiles=["librechat-websearch"],
        ),
        _artifact(
            "volume", "volumes/paperless-dir_paperless-data.tar.gz",
            "paperless-dir_paperless-data", owner="addon:paperless", module="paperless",
            project="paperless-dir",
        ),
    )


def _by_selector(manifest: dict[str, Any]) -> dict[str, restore_scope.ScopeGroup]:
    return {g.selector: g for g in restore_scope.build_groups(manifest)}


# ---------------------------------------------------------------------------
# grouping
# ---------------------------------------------------------------------------


def test_groups_are_modules_and_add_ons() -> None:
    groups = _by_selector(_full_manifest())
    assert set(groups) == {
        "module:keycloak", "module:librechat", "module:searxng", "addon:paperless"
    }


def test_core_groups_come_before_add_ons_and_advanced_ones_come_last() -> None:
    ordered = [g.selector for g in restore_scope.build_groups(_full_manifest())]
    assert ordered.index("module:keycloak") < ordered.index("addon:paperless")
    # SearXNG is the advanced one; it sorts behind the ordinary core modules.
    assert ordered.index("module:librechat") < ordered.index("module:searxng")


def test_the_config_directory_is_never_a_group() -> None:
    # It is one monolithic archive whose restore wipes the target first, and it
    # is what forces a restore out of this process. It has no module, so it
    # cannot become a group -- this pins that.
    for group in restore_scope.build_groups(_full_manifest()):
        assert group.kind in ("module", "addon")
        assert "config" not in group.selector


def test_a_v1_manifest_offers_no_groups() -> None:
    v1 = _manifest(
        {
            "kind": "volume",
            "archive": "volumes/papaia_librechat-mongodb.tar.gz",
            "target": "papaia_librechat-mongodb",
            "owner": "core",
        },
        version=1,
    )
    assert restore_scope.build_groups(v1) == []


def test_a_missing_manifest_offers_no_groups() -> None:
    assert restore_scope.build_groups(None) == []


def test_an_unknown_module_still_groups_with_a_readable_label() -> None:
    # A core that ships a module this build has never heard of must not vanish
    # from the picker; it gets a derived label instead of being dropped.
    groups = _by_selector(
        _manifest(
            _artifact("volume", "v.tar.gz", "papaia_thing", module="new-thing",
                      profiles=["new-thing"])
        )
    )
    assert groups["module:new-thing"].label == "New thing"


# ---------------------------------------------------------------------------
# presentation policy
# ---------------------------------------------------------------------------


def test_logs_and_caches_are_not_offered_as_individual_items() -> None:
    """They are still restored with their module, which is harmless. What they
    must not do is sit in a picker looking like something worth choosing."""
    groups = _by_selector(_full_manifest())
    targets = {i.target for i in groups["module:librechat"].items}
    assert "papaia_librechat-mongodb" in targets
    assert "papaia_librechat-logs" not in targets
    searxng = {i.target for i in groups["module:searxng"].items}
    assert "papaia_searxng_data" not in searxng


def test_a_hidden_volume_still_counts_towards_the_module_archive_count() -> None:
    # Restoring the module does restore it, so the count must say so.
    groups = _by_selector(_full_manifest())
    assert groups["module:librechat"].archives == 3
    assert len(groups["module:librechat"].items) == 2


def test_keycloak_cannot_be_restored_without_the_configuration() -> None:
    """The realm carries every service's client secret and is imported only on
    first start, so restoring the database alone breaks every OIDC client --
    this panel included, with no self-service way back."""
    groups = _by_selector(_full_manifest())
    assert groups["module:keycloak"].requires_config is True
    assert groups["module:keycloak"].hazards


def test_no_other_module_drags_the_configuration_in() -> None:
    groups = _by_selector(_full_manifest())
    for selector, group in groups.items():
        if selector != "module:keycloak":
            assert group.requires_config is False


def test_searxng_is_advanced_and_says_why() -> None:
    group = _by_selector(_full_manifest())["module:searxng"]
    assert group.advanced is True
    assert any("mounted over" in h for h in group.hazards)


def test_an_add_on_gets_a_generic_summary_rather_than_none() -> None:
    group = _by_selector(_full_manifest())["addon:paperless"]
    assert group.kind == "addon"
    assert group.summary
    assert group.requires_config is False


def test_an_add_on_volume_label_strips_the_add_ons_own_project_prefix() -> None:
    """An add-on's volumes are prefixed with its directory basename, not the
    core project. Stripping the wrong one leaves `paperless-dir_paperless-data`
    to be read back as a label."""
    group = _by_selector(_full_manifest())["addon:paperless"]
    labels = [i.label for i in group.items]
    assert labels == ["Paperless data"]


def test_the_reverse_proxy_gap_is_stated_rather_than_left_blank() -> None:
    # It declares no named volume, so it can never appear as a group. An
    # operator looking for it has to be told where it went.
    assert any("reverse proxy" in note.lower() for note in restore_scope.NOTES)


# ---------------------------------------------------------------------------
# allowlist and escalation
# ---------------------------------------------------------------------------


def test_the_allowlist_holds_every_group_and_item_plus_the_config_selector() -> None:
    groups = restore_scope.build_groups(_full_manifest())
    allowed = restore_scope.allowed_selectors(groups)
    assert "module:librechat" in allowed
    assert "volume:papaia_librechat-mongodb" in allowed
    assert "addon:paperless" in allowed
    assert restore_scope.CONFIG_SELECTOR in allowed
    # Hidden volumes are not individually selectable.
    assert "volume:papaia_librechat-logs" not in allowed


@pytest.mark.parametrize(
    "selected",
    [
        [restore_scope.CONFIG_SELECTOR],
        ["module:keycloak"],
        ["volume:papaia_keycloak-postgresql"],
        ["module:librechat", "module:keycloak"],
    ],
)
def test_a_selection_touching_keycloak_or_the_config_needs_a_full_restore(
    selected: list[str],
) -> None:
    groups = restore_scope.build_groups(_full_manifest())
    assert restore_scope.requires_full_restore(groups, selected) is True


@pytest.mark.parametrize(
    "selected",
    [["module:librechat"], ["volume:papaia_librechat-mongodb"], ["addon:paperless"]],
)
def test_an_ordinary_selection_does_not(selected: list[str]) -> None:
    groups = restore_scope.build_groups(_full_manifest())
    assert restore_scope.requires_full_restore(groups, selected) is False


# ---------------------------------------------------------------------------
# impact
# ---------------------------------------------------------------------------


def test_profiles_come_from_the_group_not_from_its_name() -> None:
    """A profile is what actually stops. SearXNG's is `librechat-websearch`,
    which covers four modules -- so the impact preview cannot be built from the
    module the operator ticked."""
    groups = restore_scope.build_groups(_full_manifest())
    assert restore_scope.selected_profiles(groups, ["module:searxng"]) == [
        "librechat-websearch"
    ]


def test_selecting_a_single_volume_still_resolves_its_profile() -> None:
    groups = restore_scope.build_groups(_full_manifest())
    assert restore_scope.selected_profiles(
        groups, ["volume:papaia_librechat-mongodb"]
    ) == ["librechat"]


def test_an_add_on_contributes_no_core_profile() -> None:
    # Its teardown unit is its own compose project, not a core profile.
    groups = restore_scope.build_groups(_full_manifest())
    assert restore_scope.selected_profiles(groups, ["addon:paperless"]) == []
    assert restore_scope.selected_addons(groups, ["addon:paperless"]) == ["paperless"]


def test_profiles_are_unioned_across_a_mixed_selection() -> None:
    groups = restore_scope.build_groups(_full_manifest())
    assert restore_scope.selected_profiles(
        groups, ["module:librechat", "module:searxng", "addon:paperless"]
    ) == ["librechat", "librechat-websearch"]


def test_nothing_selected_resolves_to_nothing() -> None:
    groups = restore_scope.build_groups(_full_manifest())
    assert restore_scope.selected_profiles(groups, []) == []
    assert restore_scope.selected_addons(groups, []) == []


# ---------------------------------------------------------------------------
# progress markers
# ---------------------------------------------------------------------------

_LOG = "\n".join(
    [
        "[ctl] papaia-ctl restore-scoped --only=module:librechat",
        "RESTORE-STEP\tteardown\tlibrechat\tbegin",
        "[info] Stopping and removing containers in profiles: librechat",
        "RESTORE-STEP\tteardown\tlibrechat\tok",
        "RESTORE-STEP\tartifact\tpapaia_librechat-mongodb\tbegin",
        "RESTORE-STEP\tartifact\tpapaia_librechat-mongodb\tok",
        "RESTORE-STEP\tartifact\tpapaia_librechat-meilisearch\tbegin",
    ]
)


def test_a_later_marker_replaces_the_earlier_one_in_place() -> None:
    """begin then an outcome is two lines about one subject. Collapsing them is
    what turns an append-only log into a checklist without `Job` gaining a
    progress field that would then have to be kept in sync."""
    steps = restore_scope.parse_steps(_LOG)
    assert [(s.phase, s.subject, s.state) for s in steps] == [
        ("teardown", "librechat", "ok"),
        ("artifact", "papaia_librechat-mongodb", "ok"),
        ("artifact", "papaia_librechat-meilisearch", "begin"),
    ]


def test_step_state_reads_as_done_running_or_failed() -> None:
    steps = {s.subject: s for s in restore_scope.parse_steps(_LOG)}
    assert steps["papaia_librechat-mongodb"].done is True
    assert steps["papaia_librechat-meilisearch"].running is True
    assert steps["papaia_librechat-meilisearch"].done is False


def test_a_skipped_volume_counts_as_failed_not_as_done() -> None:
    # It was left alone because a container still had it open. Reporting that as
    # success would be the worst possible lie in a restore.
    step = restore_scope.parse_steps("RESTORE-STEP\tartifact\tpapaia_x\tin-use")[0]
    assert step.failed is True
    assert step.done is False


def test_a_log_without_markers_yields_no_steps() -> None:
    assert restore_scope.parse_steps("[info] done\n[ctl] papaia-ctl backup") == []


def test_a_malformed_marker_is_ignored_rather_than_raising() -> None:
    assert restore_scope.parse_steps("RESTORE-STEP\tonly-two\tfields") == []


def test_stripping_leaves_the_operator_prose_intact() -> None:
    stripped = restore_scope.strip_steps(_LOG)
    assert "RESTORE-STEP" not in stripped
    assert "[ctl] papaia-ctl restore-scoped --only=module:librechat" in stripped
    assert "[info] Stopping and removing containers in profiles: librechat" in stripped


def test_group_to_dict_is_json_shaped() -> None:
    group = _by_selector(_full_manifest())["module:librechat"]
    payload = restore_scope.group_to_dict(group)
    assert payload["selector"] == "module:librechat"
    assert payload["profiles"] == ["librechat"]
    assert isinstance(payload["items"], list)
    assert all(isinstance(i["selector"], str) for i in payload["items"])
