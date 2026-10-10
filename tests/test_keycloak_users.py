"""The Keycloak Admin API client: where it points, what it sends and how it fails."""

from __future__ import annotations

import httpx
import pytest

from app.core.keycloak_users import (
    AdminEndpoint,
    KeycloakConflictError,
    KeycloakError,
    KeycloakForbiddenError,
    KeycloakNotFoundError,
    KeycloakRejectedError,
    KeycloakUnauthorizedError,
    KeycloakUnavailableError,
    KeycloakUsers,
    admin_endpoint,
)
from tests.fake_keycloak import REALM, TOKEN, FakeKeycloak

_ENDPOINT = AdminEndpoint(base="https://kc.test", realm=REALM)


def _client(fake: FakeKeycloak, token: str = TOKEN) -> KeycloakUsers:
    return KeycloakUsers(_ENDPOINT, token, transport=fake.transport())


# -- where the Admin API is ----------------------------------------------------------


@pytest.mark.parametrize(
    ("token_url", "base", "realm"),
    [
        (
            "https://keycloak:8443/realms/papaia/protocol/openid-connect/token",
            "https://keycloak:8443",
            "papaia",
        ),
        (
            "http://keycloak:8080/realms/papaia/protocol/openid-connect/token/",
            "http://keycloak:8080",
            "papaia",
        ),
        # A Keycloak that still serves under a path.
        (
            "https://id.example.com/auth/realms/acme/protocol/openid-connect/token",
            "https://id.example.com/auth",
            "acme",
        ),
        (
            "  https://kc.test/realms/papaia/protocol/openid-connect/token  ",
            "https://kc.test",
            "papaia",
        ),
    ],
)
def test_the_admin_api_is_derived_from_the_token_endpoint(
    token_url: str, base: str, realm: str
) -> None:
    endpoint = admin_endpoint(token_url)
    assert endpoint == AdminEndpoint(base=base, realm=realm)
    assert endpoint is not None
    assert endpoint.url == f"{base}/admin/realms/{realm}"


@pytest.mark.parametrize(
    "token_url",
    [
        "https://kc.test/token",
        "https://login.example.com/oauth2/v2.0/token",
        "https://kc.test/realms/papaia/protocol/openid-connect/auth",
        "",
        "not a url",
    ],
)
def test_a_token_endpoint_that_is_not_a_keycloak_realm_has_no_admin_api(token_url: str) -> None:
    assert admin_endpoint(token_url) is None


# -- what is sent --------------------------------------------------------------------


async def test_every_call_carries_the_users_token_and_nothing_else() -> None:
    fake = FakeKeycloak()
    fake.add_user("jane")
    async with _client(fake) as client:
        await client.list_users()
    assert fake.calls == [("GET", "/users")]


async def test_the_search_and_the_page_go_to_keycloak() -> None:
    fake = FakeKeycloak()
    for name in ("anna", "anton", "berta"):
        fake.add_user(name)
    async with _client(fake) as client:
        found = await client.list_users(search="an", first=0, limit=10)
        count = await client.count_users(search="an")
    assert [u["username"] for u in found] == ["anna", "anton"]
    assert count == 2


async def test_create_user_returns_the_id_keycloak_names() -> None:
    fake = FakeKeycloak()
    async with _client(fake) as client:
        user_id = await client.create_user({"username": "Jane", "enabled": True})
    assert fake.users[user_id]["username"] == "jane"


async def test_a_roles_removal_sends_the_roles_as_the_body_of_a_delete() -> None:
    fake = FakeKeycloak()
    user_id = fake.add_user("jane", roles=("user", "viewer"))
    async with _client(fake) as client:
        await client.remove_roles(user_id, [fake.roles["viewer"]])
    assert fake.direct[user_id] == {"user"}


async def test_realm_has_smtp_reads_the_realm() -> None:
    async with _client(FakeKeycloak(smtp=True)) as client:
        assert await client.realm_has_smtp() is True
    async with _client(FakeKeycloak(smtp=False)) as client:
        assert await client.realm_has_smtp() is False


# -- how it fails --------------------------------------------------------------------


async def test_a_token_keycloak_refuses_is_unauthorized() -> None:
    async with _client(FakeKeycloak(), token="stale") as client:
        with pytest.raises(KeycloakUnauthorizedError):
            await client.list_users()


async def test_an_account_without_the_rights_is_forbidden() -> None:
    fake = FakeKeycloak()
    fake.allowed = False
    async with _client(fake) as client:
        with pytest.raises(KeycloakForbiddenError):
            await client.list_users()


async def test_a_missing_user_is_not_found() -> None:
    async with _client(FakeKeycloak()) as client:
        with pytest.raises(KeycloakNotFoundError):
            await client.get_user("00000000-0000-0000-0000-000000000000")


async def test_a_username_that_is_taken_is_a_conflict_with_keycloaks_words() -> None:
    fake = FakeKeycloak()
    fake.add_user("jane")
    async with _client(fake) as client:
        with pytest.raises(KeycloakConflictError) as caught:
            await client.create_user({"username": "jane"})
    assert caught.value.detail == "User exists with same username"


async def test_a_refusal_of_the_values_is_rejected_with_keycloaks_words() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"errorMessage": "invalidPasswordMinLengthMessage"})

    client = KeycloakUsers(_ENDPOINT, TOKEN, transport=httpx.MockTransport(handler))
    with pytest.raises(KeycloakRejectedError) as caught:
        await client.set_password("u", "short", temporary=True)
    await client.aclose()
    assert caught.value.detail == "invalidPasswordMinLengthMessage"


async def test_a_server_error_is_reported_as_keycloaks_failure() -> None:
    fake = FakeKeycloak()
    user_id = fake.add_user("jane", email="jane@example.com")
    async with _client(fake) as client:
        with pytest.raises(KeycloakError) as caught:
            await client.send_password_email(user_id)
    assert caught.value.status == 500
    assert "Failed to send execute actions email" in caught.value.detail


async def test_an_unreachable_keycloak_is_unavailable_and_names_only_the_host() -> None:
    fake = FakeKeycloak()
    fake.down = True
    async with _client(fake) as client:
        with pytest.raises(KeycloakUnavailableError) as caught:
            await client.list_users()
    message = caught.value.detail
    assert "kc.test" in message
    assert "/admin/realms" not in message


async def test_neither_the_token_nor_a_password_is_in_an_error() -> None:
    fake = FakeKeycloak()
    fake.allowed = False
    async with _client(fake) as client:
        with pytest.raises(KeycloakForbiddenError) as caught:
            await client.set_password("u", "Sup3r-secret-value", temporary=True)
    text = f"{caught.value!s} {caught.value!r} {caught.value.detail}"
    assert TOKEN not in text
    assert "Sup3r-secret-value" not in text


# -- roles ---------------------------------------------------------------------------


async def test_a_role_is_created_changed_and_deleted() -> None:
    fake = FakeKeycloak()
    async with _client(fake) as client:
        await client.create_role("sales", "Sales team")
        assert fake.roles["sales"]["description"] == "Sales team"
        rep = await client.get_role("sales")
        await client.update_role("sales", {**rep, "description": "Sales"})
        assert fake.roles["sales"]["description"] == "Sales"
        await client.delete_role("sales")
    assert "sales" not in fake.roles


async def test_a_role_that_exists_is_a_conflict() -> None:
    fake = FakeKeycloak()
    async with _client(fake) as client:
        with pytest.raises(KeycloakConflictError):
            await client.create_role("user", "again")


async def test_what_a_role_contains_is_read_added_and_removed() -> None:
    fake = FakeKeycloak()
    async with _client(fake) as client:
        await client.create_role("team", "")
        await client.add_role_composites("team", [fake.roles["user"], fake.roles["viewer"]])
        members = await client.role_composites("team")
        assert sorted(m["name"] for m in members) == ["user", "viewer"]
        await client.remove_role_composites("team", [fake.roles["viewer"]])
        assert [m["name"] for m in await client.role_composites("team")] == ["user"]
    assert fake.roles["team"]["composite"] is True


async def test_the_roles_of_a_client_are_listed_apart_from_realm_roles() -> None:
    async with _client(FakeKeycloak()) as client:
        members = await client.role_composites("papaia-admin")
    assert {m["name"] for m in members if m.get("clientRole")} >= {"manage-users", "manage-realm"}
    assert any(not m.get("clientRole") for m in members)


async def test_the_accounts_holding_a_role_are_listed_with_a_limit() -> None:
    fake = FakeKeycloak()
    for number in range(5):
        fake.add_user(f"user{number}", roles=("viewer",))
    async with _client(fake) as client:
        found = await client.role_users("viewer", limit=3)
    assert len(found) == 3


async def test_changing_a_role_without_manage_realm_is_forbidden() -> None:
    fake = FakeKeycloak()
    fake.manage_realm = False
    async with _client(fake) as client:
        assert await client.list_roles()
        with pytest.raises(KeycloakForbiddenError):
            await client.create_role("sales", "")
