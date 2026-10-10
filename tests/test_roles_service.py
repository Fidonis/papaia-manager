"""What the Roles page does: the rules that are the manager's own, against a fake Keycloak."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from app.core.keycloak_users import (
    AdminEndpoint,
    KeycloakConflictError,
    KeycloakForbiddenError,
    KeycloakNotFoundError,
    KeycloakUnauthorizedError,
    KeycloakUsers,
)
from app.core.roles_service import (
    BUILT_IN_ROLES,
    MAX_DESCRIPTION,
    USERS_SHOWN,
    RolePartiallyCreated,
    RolesService,
    validate_role,
)
from app.core.users_service import InvalidInput
from tests.fake_keycloak import REALM, TOKEN, FakeKeycloak


class _Rig:
    def __init__(self, fake: FakeKeycloak, service: RolesService) -> None:
        self.fake = fake
        self.service = service

    def add_role(self, name: str, *, description: str = "", members: tuple[str, ...] = ()) -> None:
        self.fake.roles[name] = {
            "id": f"id-{name}",
            "name": name,
            "description": description,
            "composite": bool(members),
            "clientRole": False,
        }
        if members:
            self.fake.composites[name] = list(members)


@pytest.fixture
async def rig() -> AsyncIterator[_Rig]:
    fake = FakeKeycloak()
    client = KeycloakUsers(
        AdminEndpoint(base="https://kc.test", realm=REALM), TOKEN, transport=fake.transport()
    )
    service = RolesService(
        client,
        protected=frozenset({"ops-admin"}),
        identity_role="papaia-admin",
        default_role="user",
    )
    yield _Rig(fake, service)
    await client.aclose()


# -- what a role may be called -------------------------------------------------------


@pytest.mark.parametrize("name", ["sales", "sales-team", "team.sales", "Sales_2", "ns:role", "a"])
def test_sensible_role_names_pass(name: str) -> None:
    validate_role(name, "")


@pytest.mark.parametrize(
    "name",
    ["", "-sales", ".sales", "sales team", "sales/team", "sales?x", "x" * 128, "offline_access"],
)
def test_other_role_names_are_refused(name: str) -> None:
    with pytest.raises(InvalidInput):
        validate_role(name, "")


def test_keycloaks_own_roles_cannot_be_created() -> None:
    with pytest.raises(InvalidInput, match="Keycloak's own"):
        validate_role("default-roles-other", "")


def test_a_description_has_a_limit() -> None:
    validate_role("sales", "x" * MAX_DESCRIPTION)
    with pytest.raises(InvalidInput, match="limited"):
        validate_role("sales", "x" * (MAX_DESCRIPTION + 1))


# -- the list ------------------------------------------------------------------------


async def test_the_list_shows_roles_with_what_they_contain(rig: _Rig) -> None:
    rig.add_role("sales", description="Sales team", members=("viewer", "user"))
    view = await rig.service.snapshot()
    assert view.available
    by_name = {r.name: r for r in view.roles}
    assert [r.name for r in view.roles] == sorted(by_name)
    assert by_name["sales"].members == ["user", "viewer"]
    assert by_name["sales"].description == "Sales team"
    assert by_name["sales"].built_in is False
    assert by_name["sales"].composite is True
    assert by_name["viewer"].composite is False


async def test_the_list_hides_keycloaks_own_roles(rig: _Rig) -> None:
    names = [r.name for r in (await rig.service.snapshot()).roles]
    assert "offline_access" not in names
    assert "uma_authorization" not in names
    assert "default-roles-papaia" not in names


async def test_the_roles_of_the_stack_are_built_in_and_so_are_the_configured_ones(
    rig: _Rig,
) -> None:
    rig.add_role("ops-admin")
    rig.add_role("sales")
    by_name = {r.name: r for r in (await rig.service.snapshot()).roles}
    assert by_name["papaia-admin"].built_in and by_name["manager-admin"].built_in
    assert by_name["ops-admin"].built_in, "a role the manager is configured with is built in"
    assert not by_name["sales"].built_in
    assert {"papaia-admin", "manager-admin", "user"} <= BUILT_IN_ROLES


async def test_roles_of_clients_inside_a_composite_are_listed_apart(rig: _Rig) -> None:
    by_name = {r.name: r for r in (await rig.service.snapshot()).roles}
    admin = by_name["papaia-admin"]
    assert admin.members == ["manager-admin"]
    assert admin.client_members == ["manage-realm", "manage-users", "view-realm"]


async def test_the_dashboard_role_is_marked(rig: _Rig) -> None:
    by_name = {r.name: r for r in (await rig.service.snapshot()).roles}
    assert [n for n, r in by_name.items() if r.default] == ["user"]


async def test_a_missing_right_is_a_state_not_an_error(rig: _Rig) -> None:
    rig.fake.allowed = False
    view = await rig.service.snapshot()
    assert view.state == "forbidden"
    assert "manage-realm" in view.reason and "papaia-admin" in view.reason


async def test_an_unreachable_keycloak_is_a_state_not_an_error(rig: _Rig) -> None:
    rig.fake.down = True
    view = await rig.service.snapshot()
    assert view.state == "unavailable"
    assert "kc.test" in view.reason


async def test_a_refused_token_is_not_a_state_of_the_page(rig: _Rig) -> None:
    rig.fake.token = "something-else"
    with pytest.raises(KeycloakUnauthorizedError):
        await rig.service.snapshot()


async def test_a_role_shows_the_accounts_that_hold_it(rig: _Rig) -> None:
    rig.add_role("sales")
    for name in ("anna", "bernd"):
        rig.fake.add_user(name, roles=("sales",))
    rig.fake.add_user("carl", roles=("user",))
    detail = await rig.service.detail("sales")
    assert sorted(detail.users) == ["anna", "bernd"]
    assert detail.users_more is False


async def test_a_role_held_by_very_many_accounts_is_cut_and_says_so(rig: _Rig) -> None:
    rig.add_role("everyone")
    for number in range(USERS_SHOWN + 3):
        rig.fake.add_user(f"user{number:03d}", roles=("everyone",))
    detail = await rig.service.detail("everyone")
    assert len(detail.users) == USERS_SHOWN
    assert detail.users_more is True


async def test_keycloaks_own_role_has_no_detail(rig: _Rig) -> None:
    with pytest.raises(InvalidInput, match="Keycloak's own"):
        await rig.service.detail("offline_access")


# -- creating ------------------------------------------------------------------------


async def test_a_role_is_created_with_its_description(rig: _Rig) -> None:
    row = await rig.service.create(name=" sales ", description=" Sales team ")
    assert (row.name, row.description, row.members, row.built_in) == (
        "sales",
        "Sales team",
        [],
        False,
    )
    assert rig.fake.roles["sales"]["description"] == "Sales team"
    assert rig.fake.roles["sales"]["composite"] is False


async def test_a_role_is_created_with_the_roles_it_contains(rig: _Rig) -> None:
    row = await rig.service.create(name="sales", members=["viewer", "user", "viewer"])
    assert row.members == ["user", "viewer"]
    assert rig.fake.composites["sales"] == ["user", "viewer"]
    assert rig.fake.roles["sales"]["composite"] is True


async def test_a_role_that_exists_is_a_conflict_and_writes_nothing(rig: _Rig) -> None:
    rig.add_role("sales")
    with pytest.raises(KeycloakConflictError, match="already exists"):
        await rig.service.create(name="sales")
    assert rig.fake.wrote() == []


async def test_a_mistyped_member_leaves_no_role_behind(rig: _Rig) -> None:
    with pytest.raises(InvalidInput, match="Unknown role: ghost"):
        await rig.service.create(name="sales", members=["user", "ghost"])
    assert "sales" not in rig.fake.roles


async def test_keycloaks_own_roles_cannot_be_members(rig: _Rig) -> None:
    with pytest.raises(InvalidInput, match="offline_access"):
        await rig.service.create(name="sales", members=["offline_access"])
    assert "sales" not in rig.fake.roles


async def test_members_that_fail_to_attach_are_reported_with_the_role_that_exists(
    rig: _Rig,
) -> None:
    rig.fake.fail_on = {"POST /roles/composites"}
    with pytest.raises(RolePartiallyCreated, match="giving it its roles") as caught:
        await rig.service.create(name="sales", members=["user"])
    assert caught.value.name == "sales"
    assert "sales" in rig.fake.roles


async def test_a_missing_manage_realm_right_is_forbidden(rig: _Rig) -> None:
    rig.fake.manage_realm = False
    with pytest.raises(KeycloakForbiddenError):
        await rig.service.create(name="sales")
    assert "sales" not in rig.fake.roles


# -- changing ------------------------------------------------------------------------


async def test_the_description_is_changed(rig: _Rig) -> None:
    rig.add_role("sales", description="old")
    change = await rig.service.update("sales", description="new", members=[])
    assert (change.description_changed, change.added, change.removed) == (True, [], [])
    assert rig.fake.roles["sales"]["description"] == "new"


async def test_members_are_added_and_removed_to_match_the_choice(rig: _Rig) -> None:
    rig.add_role("sales", members=("user", "viewer"))
    change = await rig.service.update("sales", description="", members=["viewer", "manager-admin"])
    assert (change.added, change.removed) == (["manager-admin"], ["user"])
    assert sorted(rig.fake.composites["sales"]) == ["manager-admin", "viewer"]


async def test_a_choice_that_changes_nothing_writes_nothing(rig: _Rig) -> None:
    rig.add_role("sales", description="same", members=("user",))
    before = len(rig.fake.wrote())
    change = await rig.service.update("sales", description="same", members=["user"])
    assert change.changed is False
    assert len(rig.fake.wrote()) == before


async def test_the_roles_of_clients_inside_a_composite_are_left_alone(rig: _Rig) -> None:
    rig.add_role("sales", members=("user",))
    rig.fake.client_composites["sales"] = [
        {
            "id": "x",
            "name": "view-users",
            "clientRole": True,
            "containerId": "c",
            "composite": False,
        }
    ]
    change = await rig.service.update("sales", description="", members=[])
    assert change.removed == ["user"]
    assert rig.fake.client_composites["sales"], "a role of a client is not part of this choice"


@pytest.mark.parametrize("name", ["papaia-admin", "manager-admin", "user", "viewer", "ops-admin"])
async def test_a_built_in_role_is_not_changed(rig: _Rig, name: str) -> None:
    rig.add_role("ops-admin")
    with pytest.raises(InvalidInput, match="built-in"):
        await rig.service.update(name, description="x", members=[])


async def test_a_role_that_does_not_exist_is_not_found(rig: _Rig) -> None:
    with pytest.raises(KeycloakNotFoundError):
        await rig.service.update("ghost", description="", members=[])


async def test_a_member_that_does_not_exist_is_refused_and_nothing_changes(rig: _Rig) -> None:
    rig.add_role("sales", description="old")
    with pytest.raises(InvalidInput, match="Unknown role: ghost"):
        await rig.service.update("sales", description="new", members=["ghost"])
    assert rig.fake.roles["sales"]["description"] == "old"


async def test_a_role_cannot_contain_itself(rig: _Rig) -> None:
    rig.add_role("sales")
    with pytest.raises(InvalidInput, match="cannot contain itself"):
        await rig.service.update("sales", description="", members=["sales"])


async def test_a_loop_through_another_role_is_refused(rig: _Rig) -> None:
    rig.add_role("a", members=("b",))
    rig.add_role("b")
    with pytest.raises(InvalidInput, match="loop"):
        await rig.service.update("b", description="", members=["a"])
    assert rig.fake.composites.get("b", []) == []


async def test_a_longer_loop_is_found_too(rig: _Rig) -> None:
    rig.add_role("a", members=("b",))
    rig.add_role("b", members=("c",))
    rig.add_role("c")
    with pytest.raises(InvalidInput, match="loop"):
        await rig.service.update("c", description="", members=["a"])


async def test_a_role_may_contain_a_role_that_does_not_lead_back(rig: _Rig) -> None:
    rig.add_role("a", members=("b",))
    rig.add_role("b")
    rig.add_role("c")
    change = await rig.service.update("a", description="", members=["b", "c"])
    assert change.added == ["c"]


# -- deleting ------------------------------------------------------------------------


async def test_a_role_is_deleted_and_the_accounts_that_held_it_are_counted(rig: _Rig) -> None:
    rig.add_role("sales")
    holder = rig.fake.add_user("anna", roles=("sales", "user"))
    count, more = await rig.service.delete("sales")
    assert (count, more) == (1, False)
    assert "sales" not in rig.fake.roles
    assert rig.fake.direct[holder] == {"user"}, "the account stays and keeps its other roles"


async def test_a_deleted_role_is_gone_from_the_roles_that_contained_it(rig: _Rig) -> None:
    rig.add_role("sales")
    rig.add_role("team", members=("sales", "user"))
    await rig.service.delete("sales")
    assert rig.fake.composites["team"] == ["user"]


@pytest.mark.parametrize("name", ["papaia-admin", "manager-admin", "user", "ops-admin"])
async def test_a_built_in_role_is_not_deleted(rig: _Rig, name: str) -> None:
    rig.add_role("ops-admin")
    with pytest.raises(InvalidInput, match="built-in"):
        await rig.service.delete(name)
    assert name in rig.fake.roles


async def test_keycloaks_own_role_is_not_deleted(rig: _Rig) -> None:
    with pytest.raises(InvalidInput, match="Keycloak's own"):
        await rig.service.delete("offline_access")


async def test_a_role_that_does_not_exist_cannot_be_deleted(rig: _Rig) -> None:
    with pytest.raises(KeycloakNotFoundError):
        await rig.service.delete("ghost")


async def test_deleting_needs_manage_realm(rig: _Rig) -> None:
    rig.add_role("sales")
    rig.fake.manage_realm = False
    with pytest.raises(KeycloakForbiddenError):
        await rig.service.delete("sales")
    assert "sales" in rig.fake.roles
