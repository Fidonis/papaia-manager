"""Unit tests for the papaia-ctl allowlists.

Nothing here forks a process. What is under test is the layer that decides
whether a process may be forked at all -- the whole security surface between an
HTTP request and a command that starts and stops containers.
"""
from __future__ import annotations

import pytest

from app.core.ctl import (
    ALLOWED_CORE_VERBS,
    ALLOWED_PY_COMMANDS,
    ALLOWED_VERBS,
    MAX_SELECTORS,
    profiles_flag,
    run_addon_verb,
    run_core_verb,
    run_py_cli,
    selection_flag,
)

# The deployment's real profile set, as `inventory.core_groups` would report it.
ALLOWED = {
    "keycloak": frozenset({"keycloak"}),
    "librechat": frozenset({"librechat"}),
    "librechat-websearch": frozenset({"firecrawl", "jinaai", "mcp-firecrawl", "searxng"}),
    "manager": frozenset({"manager"}),
    "nginx": frozenset({"nginx"}),
}


# ---------------------------------------------------------------------------
# Verb allowlists
# ---------------------------------------------------------------------------


def test_the_core_verbs_are_exactly_the_scoped_ones_plus_backup() -> None:
    # `start` and `stop` are reachable only because the callers scope them with
    # --profiles=. `restore-scoped` is reachable because papaia-ctl refuses it
    # without --only and with --restart-clean, so it cannot replace the config
    # directory however this process calls it. `backup-delete` runs no
    # `docker compose` command at all -- it only rewrites backup.yaml and removes
    # a snapshot directory -- and needs an explicit --restore-point per point.
    # Anything else added here runs unscoped against the project this container
    # is part of.
    assert set(ALLOWED_CORE_VERBS) == {
        "backup",
        "backup-delete",
        "start",
        "stop",
        "restore-scoped",
    }


def test_backup_delete_is_a_core_verb_and_nothing_else() -> None:
    # It is dispatched as `papaia-ctl backup-delete`, never as an addon verb or
    # a python sub-command.
    assert "backup-delete" in ALLOWED_CORE_VERBS
    assert "backup-delete" not in ALLOWED_VERBS
    assert "backup-delete" not in ALLOWED_PY_COMMANDS


def test_restore_stays_out_of_the_core_verbs() -> None:
    # It tears the core stack down unconditionally, so no scoping makes it safe
    # to run as a child of this process. It goes through app.core.runner.
    # `restore-scoped` is a different verb with the refusals built in, not this
    # one with flags -- the distinction is the whole safety argument.
    assert "restore" not in ALLOWED_CORE_VERBS
    assert "restore-scoped" in ALLOWED_CORE_VERBS


async def test_an_unlisted_core_verb_is_refused_before_anything_forks() -> None:
    with pytest.raises(ValueError, match="not in the allowed set"):
        await run_core_verb(verb="restore", workspace_dir="/w", config_dir="/c")


async def test_an_unlisted_addon_verb_is_refused() -> None:
    with pytest.raises(ValueError, match="not in the allowed set"):
        await run_addon_verb(verb="backup", name="paperless", workspace_dir="/w", config_dir="/c")


def test_the_two_allowlists_stay_apart() -> None:
    # An addon name must not be dispatchable as a stack operation, nor the other
    # way round. `install` is the clearest case: there is no such core verb.
    assert "install" in ALLOWED_VERBS
    assert "install" not in ALLOWED_CORE_VERBS


def test_upgrade_stays_out_of_the_core_verbs() -> None:
    # Same argument as `restore`, one step stronger: `upgrade` runs
    # `stop --clean-up --addons` between its two phases, which removes the
    # container this process runs in, and no flag scopes that away. It goes
    # through app.core.runner.
    assert "upgrade" not in ALLOWED_CORE_VERBS
    assert "upgrade" not in ALLOWED_VERBS


# ---------------------------------------------------------------------------
# The core's Python sub-commands
# ---------------------------------------------------------------------------


def test_the_py_commands_are_exactly_the_read_only_ones() -> None:
    # Every one of these resolves, lists or evaluates. None writes. Adding a
    # command that does would put a write path behind a read-shaped helper.
    assert set(ALLOWED_PY_COMMANDS) == {"upgrade-resolve", "upgrade-plan", "addon-check"}


def test_upgrade_record_is_not_reachable_from_the_manager() -> None:
    # It appends to the migration ledger. Only the upgrade's own second phase
    # may do that -- an entry written here would make a migration that never
    # ran look applied, and the next attempt would skip it.
    assert "upgrade-record" not in ALLOWED_PY_COMMANDS


async def test_an_unlisted_py_command_is_refused_before_anything_forks() -> None:
    with pytest.raises(ValueError, match="not in the allowed set"):
        await run_py_cli(
            command="upgrade-record", workspace_dir="/w", config_dir="/c"
        )


def test_the_py_commands_share_nothing_with_the_verb_allowlists() -> None:
    # Different entry point, different process shape: these are dispatched as
    # `python3 -m lib.cli`, not as `papaia-ctl <verb>`. An overlap would mean a
    # name is dispatchable two ways with two different sets of guarantees.
    assert not (set(ALLOWED_PY_COMMANDS) & set(ALLOWED_CORE_VERBS))
    assert not (set(ALLOWED_PY_COMMANDS) & set(ALLOWED_VERBS))


# ---------------------------------------------------------------------------
# Profile scoping
# ---------------------------------------------------------------------------


def test_the_flag_is_sorted_and_deduplicated() -> None:
    # Sorted so the same request produces the same audit entry and the same job
    # target every time, whatever order the checkboxes were ticked in.
    flag = profiles_flag(["nginx", "keycloak", "nginx"], allowed=ALLOWED)
    assert flag == "--profiles=keycloak,nginx"


def test_one_group_is_a_valid_request() -> None:
    assert profiles_flag(["librechat-websearch"], allowed=ALLOWED) == (
        "--profiles=librechat-websearch"
    )


def test_the_managers_own_profile_is_refused() -> None:
    # Even though it is in `allowed`: the caller filters it out, and this is the
    # backstop for the caller that forgets to.
    with pytest.raises(ValueError, match="runs this panel"):
        profiles_flag(["manager"], allowed=ALLOWED)


def test_the_managers_profile_poisons_the_whole_request() -> None:
    # Not silently dropped -- the operator asked for it, and a partial action
    # reported as the requested one is worse than a refusal.
    with pytest.raises(ValueError, match="runs this panel"):
        profiles_flag(["keycloak", "manager"], allowed=ALLOWED)


def test_a_profile_this_deployment_does_not_have_is_refused() -> None:
    # `localai` exists in the stack repo but is not enabled here, so its env file
    # was never rendered and `docker compose config` would fail outright.
    with pytest.raises(ValueError, match="not a service group"):
        profiles_flag(["localai"], allowed=ALLOWED)


@pytest.mark.parametrize(
    "name",
    ["../etc", "keycloak;rm -rf /", "Keycloak", "-x", "", "a" * 33],
)
def test_a_name_that_is_not_a_profile_is_refused_by_shape(name: str) -> None:
    with pytest.raises(ValueError):
        profiles_flag([name], allowed=ALLOWED)


def test_an_empty_selection_is_refused() -> None:
    # An empty --profiles= would fall back to COMPOSE_PROFILES, i.e. the whole
    # stack, which is emphatically not what "no groups selected" means.
    with pytest.raises(ValueError, match="no service group"):
        profiles_flag([], allowed=ALLOWED)


# ---------------------------------------------------------------------------
# selection_flag
#
# Same two-stage shape as profiles_flag: the pattern is the cheap half, and the
# allowlist -- derived from the snapshot's own manifest -- is what stops a
# well-formed selector naming something that restore point does not contain.
# ---------------------------------------------------------------------------

# What `restore_scope.allowed_selectors` would report for one snapshot.
SELECTORS = {
    "module:keycloak",
    "module:librechat",
    "addon:paperless",
    "volume:papaia_librechat-mongodb",
    "volume:papaia_searxng_config",
    "config",
}


def test_selectors_are_sorted_and_deduplicated() -> None:
    flag = selection_flag(
        ["module:librechat", "module:keycloak", "module:librechat"], allowed=SELECTORS
    )
    assert flag == "--only=module:keycloak,module:librechat"


def test_a_volume_selector_may_carry_underscores() -> None:
    # Real names look like this: papaia_searxng_config, paperless-dir_paperless-data.
    assert selection_flag(["volume:papaia_searxng_config"], allowed=SELECTORS) == (
        "--only=volume:papaia_searxng_config"
    )


@pytest.mark.parametrize(
    "selector",
    [
        "librechat",  # module, service, profile and volume prefix all at once
        "profile:librechat",  # deliberately not in the grammar
        "module:../etc",
        "module:-y",
        "module:--restart-clean",
        "volume:../../etc/passwd",
        "volume:a b",
        "volume:x;rm -rf /",
        "module:Keycloak",
        "module:",
        ":librechat",
        "",
    ],
)
def test_a_malformed_selector_is_refused_by_shape(selector: str) -> None:
    with pytest.raises(ValueError):
        selection_flag([selector], allowed=SELECTORS | {selector})


def test_the_manager_module_is_refused_even_if_allowed_lists_it() -> None:
    # It runs this panel. Same rule as profiles_flag's `manager`, and it holds
    # regardless of what the snapshot happens to contain.
    with pytest.raises(ValueError, match="runs this panel"):
        selection_flag(["module:manager"], allowed=SELECTORS | {"module:manager"})


def test_a_selector_outside_the_restore_point_is_refused() -> None:
    with pytest.raises(ValueError, match="not part of this restore point"):
        selection_flag(["module:litellm"], allowed=SELECTORS)


def test_an_empty_selection_flag_is_refused() -> None:
    # An empty --only= would be a whole-snapshot restore, which is the opposite
    # of what "nothing selected" means.
    with pytest.raises(ValueError, match="no selection"):
        selection_flag([], allowed=SELECTORS)


def test_too_many_selectors_are_refused() -> None:
    many = {f"module:m{i}" for i in range(MAX_SELECTORS + 1)}
    with pytest.raises(ValueError, match="at most"):
        selection_flag(many, allowed=SELECTORS | many)
