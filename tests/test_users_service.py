"""What the Users page does: the rules that are the manager's own, against a fake Keycloak."""

from __future__ import annotations

import string
from collections.abc import AsyncIterator

import pytest

from app.auth.oidc import OIDCClaims
from app.core.keycloak_users import (
    AdminEndpoint,
    KeycloakConflictError,
    KeycloakNotFoundError,
    KeycloakUnauthorizedError,
    KeycloakUsers,
)
from app.core.users_service import (
    PASSWORD_LENGTH,
    InvalidInput,
    PartiallyCreated,
    UsersService,
    generate_temporary_password,
    is_technical_role,
    validate_new_user,
)
from tests.fake_keycloak import REALM, TOKEN, FakeKeycloak

_IDENTITY_ROLE = "papaia-admin"


class _Rig:
    def __init__(self, fake: FakeKeycloak, service: UsersService, client: KeycloakUsers) -> None:
        self.fake = fake
        self.service = service
        self.client = client


@pytest.fixture
async def rig() -> AsyncIterator[_Rig]:
    fake = FakeKeycloak(smtp=True)
    me = fake.add_user(
        "admin", user_id="11111111-1111-1111-1111-111111111111", roles=("papaia-admin",)
    )
    client = KeycloakUsers(
        AdminEndpoint(base="https://kc.test", realm=REALM), TOKEN, transport=fake.transport()
    )
    actor = OIDCClaims(
        sub=me, preferred_username="admin", roles=["papaia-admin"], exp=2_000_000_000
    )
    yield _Rig(fake, UsersService(client, actor=actor, identity_role=_IDENTITY_ROLE), client)
    await client.aclose()


# -- the temporary password ----------------------------------------------------------


def test_a_temporary_password_is_long_mixed_and_readable() -> None:
    for _ in range(50):
        password = generate_temporary_password()
        assert len(password) == PASSWORD_LENGTH
        assert any(c in string.ascii_uppercase for c in password)
        assert any(c in string.ascii_lowercase for c in password)
        assert any(c in string.digits for c in password)
        assert any(c in string.punctuation for c in password)
        assert not set(password) & set("0O1lI"), "characters that look alike"


def test_temporary_passwords_differ() -> None:
    assert len({generate_temporary_password() for _ in range(100)}) == 100


# -- what a new account needs --------------------------------------------------------


@pytest.mark.parametrize("username", ["jane", "jane.doe", "j_doe-2", "jane@example.com", "J1"])
def test_sensible_usernames_pass(username: str) -> None:
    validate_new_user(username=username, email="", first_name="", last_name="", credential="none")


@pytest.mark.parametrize(
    "username", ["", "a", ".jane", "jane doe", "jane/doe", "jane?x=1", "x" * 256]
)
def test_other_usernames_are_refused(username: str) -> None:
    with pytest.raises(InvalidInput):
        validate_new_user(
            username=username, email="", first_name="", last_name="", credential="none"
        )


@pytest.mark.parametrize(
    "email", ["jane", "jane@", "@example.com", "jane@example", "a b@example.com"]
)
def test_an_email_that_is_not_one_is_refused(email: str) -> None:
    with pytest.raises(InvalidInput):
        validate_new_user(
            username="jane", email=email, first_name="", last_name="", credential="none"
        )


def test_a_mailed_link_needs_an_address() -> None:
    with pytest.raises(InvalidInput, match="email address"):
        validate_new_user(
            username="jane", email="", first_name="", last_name="", credential="email"
        )


@pytest.mark.parametrize(
    ("name", "technical"),
    [
        ("offline_access", True),
        ("uma_authorization", True),
        ("default-roles-papaia", True),
        ("user", False),
        ("papaia-admin", False),
        ("viewer", False),
    ],
)
def test_keycloaks_own_roles_are_technical(name: str, technical: bool) -> None:
    assert is_technical_role(name) is technical


# -- the list ------------------------------------------------------------------------


async def test_the_list_shows_the_roles_that_can_be_handed_out(rig: _Rig) -> None:
    rig.fake.add_user(
        "jane",
        email="jane@example.com",
        first="Jane",
        last="Doe",
        roles=("user", "viewer", "offline_access", "default-roles-papaia"),
        actions=("UPDATE_PASSWORD",),
    )
    view = await rig.service.snapshot()
    assert view.available
    assert view.total == 2
    jane = next(u for u in view.users if u.username == "jane")
    assert jane.roles == ["user", "viewer"]
    assert jane.display_name == "Jane Doe"
    assert jane.initial == "J"
    assert jane.password_change_pending is True
    assert jane.created is not None
    assert view.smtp is True


async def test_the_list_marks_the_signed_in_account(rig: _Rig) -> None:
    rig.fake.add_user("jane")
    view = await rig.service.snapshot()
    assert {u.username: u.is_self for u in view.users} == {"admin": True, "jane": False}


async def test_the_list_pages_and_searches(rig: _Rig) -> None:
    for number in range(30):
        rig.fake.add_user(f"user{number:02d}")
    first = await rig.service.snapshot(limit=25)
    assert (len(first.users), first.total, first.first) == (25, 31, 0)
    second = await rig.service.snapshot(first=25, limit=25)
    assert len(second.users) == 6
    found = await rig.service.snapshot(search="user07")
    assert [u.username for u in found.users] == ["user07"]


async def test_a_missing_right_is_a_state_of_the_page_not_an_error(rig: _Rig) -> None:
    rig.fake.allowed = False
    view = await rig.service.snapshot()
    assert view.state == "forbidden"
    assert not view.available
    assert _IDENTITY_ROLE in view.reason
    assert "Associated roles" in view.reason and "realm-management" in view.reason


async def test_an_unreachable_keycloak_is_a_state_of_the_page_not_an_error(rig: _Rig) -> None:
    rig.fake.down = True
    view = await rig.service.snapshot()
    assert view.state == "unavailable"
    assert "kc.test" in view.reason


async def test_a_refused_token_is_not_a_state_of_the_page(rig: _Rig) -> None:
    rig.fake.token = "something-else"
    with pytest.raises(KeycloakUnauthorizedError):
        await rig.service.snapshot()


async def test_a_realm_without_a_mail_server_is_reported_as_one(rig: _Rig) -> None:
    rig.fake.smtp = False
    assert (await rig.service.snapshot()).smtp is False


# -- creating an account -------------------------------------------------------------


async def test_a_new_account_gets_a_temporary_password_that_must_be_changed(rig: _Rig) -> None:
    created = await rig.service.create(
        username="Jane", email="jane@example.com", first_name="Jane", last_name="Doe"
    )
    assert created.username == "jane", "Keycloak stores usernames in lower case"
    assert created.temporary_password is not None
    assert len(created.temporary_password) == PASSWORD_LENGTH
    assert rig.fake.passwords[created.id] == (created.temporary_password, True)
    stored = rig.fake.users[created.id]
    assert stored["email"] == "jane@example.com"
    assert stored["firstName"] == "Jane"
    assert stored["enabled"] is True


async def test_a_new_account_can_be_sent_a_link_instead(rig: _Rig) -> None:
    created = await rig.service.create(
        username="jane", email="jane@example.com", credential="email"
    )
    assert created.temporary_password is None
    assert created.email_sent is True
    assert rig.fake.mails == [created.id]
    assert created.id not in rig.fake.passwords


async def test_a_mail_that_fails_is_reported_and_the_account_stays(rig: _Rig) -> None:
    rig.fake.fail_mail = True
    created = await rig.service.create(
        username="jane", email="jane@example.com", credential="email"
    )
    assert created.email_sent is False
    assert created.email_error
    assert created.id in rig.fake.users


async def test_a_link_cannot_be_asked_for_where_the_realm_cannot_send_mail(rig: _Rig) -> None:
    rig.fake.smtp = False
    with pytest.raises(InvalidInput, match="mail server"):
        await rig.service.create(username="jane", email="jane@example.com", credential="email")
    assert rig.fake.wrote() == []


async def test_a_new_account_can_be_left_without_a_password(rig: _Rig) -> None:
    created = await rig.service.create(username="jane", credential="none")
    assert created.temporary_password is None
    assert created.id not in rig.fake.passwords


async def test_a_username_that_is_taken_is_a_conflict(rig: _Rig) -> None:
    rig.fake.add_user("jane")
    with pytest.raises(KeycloakConflictError):
        await rig.service.create(username="Jane")


async def test_a_new_account_has_no_dashboard_role_until_it_is_given_one(rig: _Rig) -> None:
    # The realm's default role carries Keycloak's own roles, not `user`: without a role of
    # its own a new account can sign in to Keycloak and use nothing.
    created = await rig.service.create(username="jane")
    assert created.roles == []
    _, options = await rig.service.role_options(created.id)
    user = next(o for o in options if o.name == "user")
    assert (user.direct, user.inherited) == (False, False)


async def test_a_new_account_is_given_the_roles_asked_for(rig: _Rig) -> None:
    created = await rig.service.create(username="jane", roles=["viewer", "user", "viewer"])
    assert created.roles == ["user", "viewer"]
    assert rig.fake.direct[created.id] >= {"user", "viewer"}
    view = await rig.service.snapshot()
    assert next(u for u in view.users if u.id == created.id).roles == ["user", "viewer"]


async def test_the_highest_role_can_be_given_to_a_new_account(rig: _Rig) -> None:
    created = await rig.service.create(username="jane", roles=["papaia-admin"])
    assert "papaia-admin" in rig.fake.direct[created.id]


async def test_a_password_keycloak_refuses_is_reported_with_the_account_that_exists(
    rig: _Rig,
) -> None:
    rig.fake.reject_passwords = True
    with pytest.raises(PartiallyCreated) as caught:
        await rig.service.create(username="jane")
    assert caught.value.username == "jane"
    assert "setting its password" in caught.value.message
    assert "invalidPasswordMinLengthMessage" in caught.value.message
    assert any(u["username"] == "jane" for u in rig.fake.users.values())


async def test_roles_that_fail_to_attach_are_reported_with_the_account_that_exists(
    rig: _Rig,
) -> None:
    rig.fake.fail_on = {"POST /role-mappings/realm"}
    with pytest.raises(PartiallyCreated, match="giving it its roles"):
        await rig.service.create(username="jane", roles=["user"])
    assert any(u["username"] == "jane" for u in rig.fake.users.values())


async def test_a_mistyped_role_leaves_no_account_behind(rig: _Rig) -> None:
    with pytest.raises(InvalidInput, match="Unknown role: ghost"):
        await rig.service.create(username="jane", roles=["user", "ghost"])
    assert rig.fake.wrote() == []
    assert not any(u["username"] == "jane" for u in rig.fake.users.values())


async def test_roles_for_a_new_account_come_with_the_dashboard_role_marked(rig: _Rig) -> None:
    service = UsersService(
        rig.client,
        actor=OIDCClaims(sub="x", preferred_username="x", roles=[], exp=2_000_000_000),
        identity_role=_IDENTITY_ROLE,
        default_role="user",
    )
    options = await service.assignable_roles()
    names = [o.name for o in options]
    assert names == sorted(names)
    assert "offline_access" not in names and "default-roles-papaia" not in names
    assert [o.name for o in options if o.default] == ["user"]
    assert not any(o.direct or o.inherited for o in options)


# -- enabling and disabling ----------------------------------------------------------


async def test_disabling_keeps_the_whole_record_and_ends_the_sessions(rig: _Rig) -> None:
    user_id = rig.fake.add_user("jane", email="jane@example.com", first="Jane")
    rig.fake.add_session(user_id)
    username, ended = await rig.service.set_enabled(user_id, False)
    assert (username, ended) == ("jane", True)
    stored = rig.fake.users[user_id]
    assert stored["enabled"] is False
    assert stored["email"] == "jane@example.com", "a partial update must not clear the profile"
    assert rig.fake.sessions[user_id] == []


async def test_enabling_does_not_touch_the_sessions(rig: _Rig) -> None:
    user_id = rig.fake.add_user("jane", enabled=False)
    username, ended = await rig.service.set_enabled(user_id, True)
    assert (username, ended) == ("jane", False)
    assert rig.fake.users[user_id]["enabled"] is True
    assert rig.fake.logged_out == []


async def test_nobody_disables_their_own_account(rig: _Rig) -> None:
    me = next(iter(rig.fake.users))
    with pytest.raises(InvalidInput, match="own account"):
        await rig.service.set_enabled(me, False)
    assert rig.fake.wrote() == []


async def test_a_missing_account_is_not_found(rig: _Rig) -> None:
    with pytest.raises(KeycloakNotFoundError):
        await rig.service.set_enabled("00000000-0000-0000-0000-000000000000", False)


# -- passwords -----------------------------------------------------------------------


async def test_a_new_temporary_password_is_set_and_returned(rig: _Rig) -> None:
    user_id = rig.fake.add_user("jane")
    username, password = await rig.service.temporary_password(user_id)
    assert username == "jane"
    assert rig.fake.passwords[user_id] == (password, True)


async def test_a_password_link_needs_an_address_and_a_mail_server(rig: _Rig) -> None:
    without = rig.fake.add_user("nomail")
    with pytest.raises(InvalidInput, match="no email address"):
        await rig.service.password_email(without)
    with_mail = rig.fake.add_user("jane", email="jane@example.com")
    rig.fake.smtp = False
    with pytest.raises(InvalidInput, match="mail server"):
        await rig.service.password_email(with_mail)
    rig.fake.smtp = True
    assert await rig.service.password_email(with_mail) == "jane"
    assert rig.fake.mails == [with_mail]


# -- roles ---------------------------------------------------------------------------


async def test_the_role_dialog_lists_what_can_be_handed_out(rig: _Rig) -> None:
    user_id = rig.fake.add_user("jane", roles=("viewer",))
    username, options = await rig.service.role_options(user_id)
    names = [o.name for o in options]
    assert username == "jane"
    assert names == sorted(names)
    assert "offline_access" not in names
    assert "default-roles-papaia" not in names
    assert {o.name: o.direct for o in options}["viewer"] is True
    assert {o.name: o.direct for o in options}["user"] is False


async def test_a_role_held_through_a_composite_is_inherited(rig: _Rig) -> None:
    _, options = await rig.service.role_options(next(iter(rig.fake.users)))
    by_name = {o.name: o for o in options}
    assert by_name["papaia-admin"].direct is True
    assert by_name["manager-admin"].inherited is True
    assert by_name["manager-admin"].direct is False


async def test_the_own_identity_role_is_locked(rig: _Rig) -> None:
    me = next(iter(rig.fake.users))
    _, mine = await rig.service.role_options(me)
    assert {o.name: o.locked for o in mine}["papaia-admin"] is True
    other = rig.fake.add_user("jane")
    _, theirs = await rig.service.role_options(other)
    assert not any(o.locked for o in theirs)


async def test_roles_are_added_and_removed_to_match_the_choice(rig: _Rig) -> None:
    user_id = rig.fake.add_user("jane", roles=("user", "viewer"))
    change = await rig.service.set_roles(user_id, ["user", "manager-admin"])
    assert (change.added, change.removed) == (["manager-admin"], ["viewer"])
    assert rig.fake.direct[user_id] == {"user", "manager-admin"}


async def test_the_highest_role_can_be_handed_out(rig: _Rig) -> None:
    # The role is the highest in the stack, so nothing is held back from it.
    user_id = rig.fake.add_user("jane")
    change = await rig.service.set_roles(user_id, ["user", "papaia-admin"])
    assert change.added == ["papaia-admin"]


async def test_a_technical_role_is_neither_required_nor_removed(rig: _Rig) -> None:
    user_id = rig.fake.add_user("jane", roles=("user", "offline_access"))
    await rig.service.set_roles(user_id, [])
    assert rig.fake.direct[user_id] == {"offline_access"}


async def test_an_unknown_role_is_refused_and_nothing_changes(rig: _Rig) -> None:
    user_id = rig.fake.add_user("jane")
    with pytest.raises(InvalidInput, match="Unknown role: ghost"):
        await rig.service.set_roles(user_id, ["ghost"])
    assert rig.fake.wrote() == []


async def test_a_technical_role_cannot_be_named(rig: _Rig) -> None:
    user_id = rig.fake.add_user("jane")
    with pytest.raises(InvalidInput, match="offline_access"):
        await rig.service.set_roles(user_id, ["offline_access"])


async def test_nobody_takes_their_own_identity_role_away(rig: _Rig) -> None:
    me = next(iter(rig.fake.users))
    with pytest.raises(InvalidInput, match="your own account"):
        await rig.service.set_roles(me, ["user"])
    assert rig.fake.direct[me] == {"papaia-admin"}


async def test_an_unchanged_choice_writes_nothing(rig: _Rig) -> None:
    user_id = rig.fake.add_user("jane", roles=("user",))
    change = await rig.service.set_roles(user_id, ["user"])
    assert (change.added, change.removed) == ([], [])
    assert rig.fake.wrote() == []


# -- sessions ------------------------------------------------------------------------


async def test_sessions_list_the_newest_activity_first(rig: _Rig) -> None:
    user_id = rig.fake.add_user("jane")
    old = rig.fake.add_session(user_id, ip="10.0.0.1", clients=("librechat", "papaia-manager"))
    new = rig.fake.add_session(user_id, ip="10.0.0.2")
    rig.fake.sessions[user_id][0]["lastAccess"] = 1_760_000_100_000
    username, rows = await rig.service.sessions(user_id)
    assert username == "jane"
    assert [r.id for r in rows] == [new, old]
    assert rows[1].clients == ["librechat", "papaia-manager"]
    assert rows[0].ip_address == "10.0.0.2"


async def test_one_session_ends(rig: _Rig) -> None:
    user_id = rig.fake.add_user("jane")
    keep = rig.fake.add_session(user_id)
    gone = rig.fake.add_session(user_id)
    assert await rig.service.end_session(user_id, gone) == "jane"
    assert [s["id"] for s in rig.fake.sessions[user_id]] == [keep]


async def test_a_session_of_somebody_else_cannot_be_ended_through_this_account(rig: _Rig) -> None:
    jane = rig.fake.add_user("jane")
    other = rig.fake.add_user("other")
    foreign = rig.fake.add_session(other)
    with pytest.raises(KeycloakNotFoundError):
        await rig.service.end_session(jane, foreign)
    assert rig.fake.sessions[other] != []


async def test_all_sessions_end_and_are_counted(rig: _Rig) -> None:
    user_id = rig.fake.add_user("jane")
    rig.fake.add_session(user_id)
    rig.fake.add_session(user_id)
    assert await rig.service.end_all_sessions(user_id) == ("jane", 2)
    assert rig.fake.sessions[user_id] == []


async def test_nobody_ends_all_of_their_own_sessions_here(rig: _Rig) -> None:
    me = next(iter(rig.fake.users))
    rig.fake.add_session(me)
    with pytest.raises(InvalidInput, match="Sign out instead"):
        await rig.service.end_all_sessions(me)
    assert rig.fake.sessions[me] != []
