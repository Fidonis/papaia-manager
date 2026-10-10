"""A small async client for the part of Keycloak's Admin REST API the Users page needs.

It acts with the access token of the signed-in user (see `app.auth.user_token`), so Keycloak
applies that account's rights: no service account and no credential of the manager's own. The
address comes from the token endpoint the manager already knows (`admin_endpoint`), so there
is nothing new to configure.

Failures are told apart because the page treats them differently:

* `KeycloakUnavailableError`: Keycloak cannot be reached. The page shows the reason.
* `KeycloakUnauthorizedError` (401): the token was refused; the browser signs in again.
* `KeycloakForbiddenError` (403): the account lacks Keycloak's user-administration rights.
  That is a state the page explains, not a fault.
* `KeycloakNotFoundError`, `KeycloakConflictError`, `KeycloakRejectedError`: Keycloak
  answered and said no to this one request. The caller decides what that means.

The token goes into one header and nowhere else: it is not part of any message, log line or
exception text, and neither is a password sent to Keycloak.
"""
from __future__ import annotations

import re
import ssl
from dataclasses import dataclass
from types import TracebackType
from typing import Any
from urllib.parse import quote

import httpx

DEFAULT_TIMEOUT = 10.0

# `<base>/realms/<realm>/protocol/openid-connect/token`: the token endpoint the manager
# signs in against. The base may carry a path (`/auth` on a Keycloak that still has one).
_TOKEN_URL = re.compile(
    r"^(?P<base>https?://[^?#]+?)/realms/(?P<realm>[^/?#]+)/protocol/openid-connect/token/?$"
)


@dataclass(frozen=True, slots=True)
class AdminEndpoint:
    """Where the Admin API of one realm is."""

    base: str
    realm: str

    @property
    def url(self) -> str:
        return f"{self.base}/admin/realms/{quote(self.realm, safe='')}"


def admin_endpoint(token_url: str) -> AdminEndpoint | None:
    """The Admin API of the realm the manager signs in against, or None.

    None means the token endpoint is not a Keycloak realm's (an external identity provider,
    or an address nobody could derive anything from), and the Users page says so.
    """
    match = _TOKEN_URL.match(token_url.strip())
    if match is None:
        return None
    return AdminEndpoint(base=match["base"].rstrip("/"), realm=match["realm"])


class KeycloakError(Exception):
    """Keycloak answered with an error status."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


class KeycloakUnavailableError(KeycloakError):
    """Keycloak cannot be reached."""


class KeycloakUnauthorizedError(KeycloakError):
    """The access token was refused (401): expired, revoked, or not for this realm."""


class KeycloakForbiddenError(KeycloakError):
    """The account does not have the Keycloak right this request needs (403)."""


class KeycloakNotFoundError(KeycloakError):
    """The user, role or session does not exist (any more)."""


class KeycloakConflictError(KeycloakError):
    """The request clashes with what exists, such as a username that is already taken."""


class KeycloakRejectedError(KeycloakError):
    """Keycloak refused the values (400/422), such as a password the realm's policy rejects."""


def _error_detail(response: httpx.Response) -> str:
    """Keycloak's own message, or the bare status.

    The body is Keycloak's answer to this request, never the request: a password sent in it
    is not echoed back by Keycloak, and the text is cut so a long stack trace stays out.
    """
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    if isinstance(body, dict):
        for key in ("errorMessage", "error_description", "error"):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:300]
    return f"HTTP {response.status_code}"


class KeycloakUsers:
    """One client per request: the route opens it, uses it and closes it."""

    def __init__(
        self,
        endpoint: AdminEndpoint,
        token: str,
        *,
        verify: ssl.SSLContext | bool = True,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            verify=verify,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> KeycloakUsers:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: Any = None,
    ) -> httpx.Response:
        """Send one request. Raises a `KeycloakError` for a transport failure or non-2xx."""
        try:
            response = await self._client.request(
                method, f"{self._endpoint.url}{path}", params=params, json=json
            )
        except httpx.HTTPError as exc:
            # The exception text can carry the full URL; the host is enough to act on.
            target = httpx.URL(self._endpoint.base)
            where = f"{target.host}:{target.port}" if target.port else str(target.host)
            raise KeycloakUnavailableError(
                0, f"Keycloak is not reachable at {where} ({type(exc).__name__})"
            ) from exc

        status = response.status_code
        if status < 400:
            return response
        detail = _error_detail(response)
        if status == 401:
            raise KeycloakUnauthorizedError(status, "Keycloak refused the session token")
        if status == 403:
            raise KeycloakForbiddenError(status, detail)
        if status == 404:
            raise KeycloakNotFoundError(status, detail)
        if status == 409:
            raise KeycloakConflictError(status, detail)
        if status in (400, 422):
            raise KeycloakRejectedError(status, detail)
        raise KeycloakError(status, f"Keycloak failed: {detail}")

    @staticmethod
    def _json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise KeycloakError(
                response.status_code, "Keycloak sent an unreadable answer"
            ) from exc

    @staticmethod
    def _user_path(user_id: str) -> str:
        return f"/users/{quote(user_id, safe='')}"

    # -- users ----------------------------------------------------------------------------

    async def list_users(
        self, *, search: str = "", first: int = 0, limit: int = 25
    ) -> list[dict[str, Any]]:
        """One page of users, full representations: the brief one has no required actions."""
        params = {"first": str(first), "max": str(limit), "briefRepresentation": "false"}
        if search:
            params["search"] = search
        body = self._json(await self._request("GET", "/users", params=params))
        return [u for u in body if isinstance(u, dict)] if isinstance(body, list) else []

    async def count_users(self, *, search: str = "") -> int:
        params = {"search": search} if search else None
        body = self._json(await self._request("GET", "/users/count", params=params))
        return body if isinstance(body, int) and not isinstance(body, bool) else 0

    async def get_user(self, user_id: str) -> dict[str, Any]:
        body = self._json(await self._request("GET", self._user_path(user_id)))
        if not isinstance(body, dict):
            raise KeycloakError(200, "Keycloak sent an unreadable answer")
        return body

    async def create_user(self, representation: dict[str, Any]) -> str:
        """Create a user and return its id (Keycloak names it in the `Location` header)."""
        response = await self._request("POST", "/users", json=representation)
        location = str(response.headers.get("location", ""))
        user_id = location.rstrip("/").rsplit("/", 1)[-1]
        if user_id:
            return user_id
        # No header (a proxy dropped it): ask for the user by name.
        username = str(representation.get("username", ""))
        found = await self.list_users(search=username, limit=20)
        for user in found:
            if str(user.get("username", "")).lower() == username.lower() and user.get("id"):
                return str(user["id"])
        raise KeycloakError(201, "Keycloak created the user but did not say which")

    async def update_user(self, user_id: str, changes: dict[str, Any]) -> None:
        await self._request("PUT", self._user_path(user_id), json=changes)

    # -- credentials ----------------------------------------------------------------------

    async def set_password(self, user_id: str, password: str, *, temporary: bool) -> None:
        """Set a password. A temporary one makes Keycloak ask for a new one at the next sign-in."""
        await self._request(
            "PUT",
            f"{self._user_path(user_id)}/reset-password",
            json={"type": "password", "value": password, "temporary": temporary},
        )

    async def send_password_email(self, user_id: str) -> None:
        """Mail the user Keycloak's own "update your password" link. Needs SMTP in the realm."""
        await self._request(
            "PUT",
            f"{self._user_path(user_id)}/execute-actions-email",
            json=["UPDATE_PASSWORD"],
        )

    async def realm_has_smtp(self) -> bool:
        """Whether the realm can send mail at all: a mail server is configured on it."""
        body = self._json(await self._request("GET", ""))
        smtp = body.get("smtpServer") if isinstance(body, dict) else None
        return isinstance(smtp, dict) and bool(smtp.get("host"))

    # -- roles ----------------------------------------------------------------------------

    async def list_roles(self) -> list[dict[str, Any]]:
        body = self._json(
            await self._request("GET", "/roles", params={"first": "0", "max": "500"})
        )
        return [r for r in body if isinstance(r, dict)] if isinstance(body, list) else []

    async def get_role(self, name: str) -> dict[str, Any]:
        body = self._json(await self._request("GET", f"/roles/{quote(name, safe='')}"))
        if not isinstance(body, dict):
            raise KeycloakError(200, "Keycloak sent an unreadable answer")
        return body

    async def direct_roles(self, user_id: str) -> list[dict[str, Any]]:
        """The realm roles mapped to the user itself."""
        body = self._json(
            await self._request("GET", f"{self._user_path(user_id)}/role-mappings/realm")
        )
        return [r for r in body if isinstance(r, dict)] if isinstance(body, list) else []

    async def effective_roles(self, user_id: str) -> list[dict[str, Any]]:
        """The realm roles the user holds, those inherited through a composite included."""
        body = self._json(
            await self._request(
                "GET", f"{self._user_path(user_id)}/role-mappings/realm/composite"
            )
        )
        return [r for r in body if isinstance(r, dict)] if isinstance(body, list) else []

    async def add_roles(self, user_id: str, roles: list[dict[str, Any]]) -> None:
        await self._request(
            "POST", f"{self._user_path(user_id)}/role-mappings/realm", json=roles
        )

    async def remove_roles(self, user_id: str, roles: list[dict[str, Any]]) -> None:
        await self._request(
            "DELETE", f"{self._user_path(user_id)}/role-mappings/realm", json=roles
        )

    # -- sessions -------------------------------------------------------------------------

    async def user_sessions(self, user_id: str) -> list[dict[str, Any]]:
        body = self._json(await self._request("GET", f"{self._user_path(user_id)}/sessions"))
        return [s for s in body if isinstance(s, dict)] if isinstance(body, list) else []

    async def delete_session(self, session_id: str) -> None:
        await self._request("DELETE", f"/sessions/{quote(session_id, safe='')}")

    async def logout_user(self, user_id: str) -> None:
        """End every session of the user."""
        await self._request("POST", f"{self._user_path(user_id)}/logout")
