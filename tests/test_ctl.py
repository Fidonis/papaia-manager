"""Unit tests for the papaia-ctl allowlists.

Nothing here forks a process. What is under test is the layer that decides
whether a process may be forked at all -- the whole security surface between an
HTTP request and a command that starts and stops containers.
"""
from __future__ import annotations

import pytest

from app.core.ctl import (
    ALLOWED_CORE_VERBS,
    ALLOWED_VERBS,
    profiles_flag,
    run_addon_verb,
    run_core_verb,
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
    # --profiles=. Anything else added here runs unscoped against the project
    # this container is part of.
    assert set(ALLOWED_CORE_VERBS) == {"backup", "start", "stop"}


def test_restore_stays_out_of_the_core_verbs() -> None:
    # It tears the core stack down unconditionally, so no scoping makes it safe
    # to run as a child of this process. It goes through app.core.runner.
    assert "restore" not in ALLOWED_CORE_VERBS


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
