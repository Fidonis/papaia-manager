"""A Keycloak Admin API that speaks just enough REST, for the tests of the Users page.

An in-memory fake behind `httpx.MockTransport`. It answers only the calls the manager makes,
in Keycloak's own error shape. Like the real one it rejects a request without the bearer
token it expects (401) and one from an account without user-administration rights (403), so
a test can tell "the manager sent the signed-in user's token" from "it sent something else".
"""

from __future__ import annotations

import json
import re
import secrets
import uuid
from typing import Any

import httpx

TOKEN = "user-token"
REALM = "papaia"

_ROLE_DEFAULTS = {
    "default-roles-papaia": "Default role",
    "offline_access": "",
    "uma_authorization": "",
    "user": "Regular user",
    "viewer": "Read-only viewer",
    "manager-admin": "papaia-manager administrator",
    "papaia-admin": "Full administrator access",
}


def _err(status: int, message: str) -> httpx.Response:
    return httpx.Response(status, json={"errorMessage": message})


class FakeKeycloak:
    def __init__(self, *, token: str = TOKEN, smtp: bool = False) -> None:
        self.token = token
        self.smtp = smtp
        # Whether the account behind the token holds manage-users. Without it: 403.
        self.allowed = True
        self.down = False
        self.users: dict[str, dict[str, Any]] = {}
        self.direct: dict[str, set[str]] = {}
        self.roles: dict[str, dict[str, Any]] = {}
        self.composites: dict[str, list[str]] = {
            "papaia-admin": ["manager-admin"],
            # As the realm import leaves it in a real Keycloak: Keycloak's own defaults, not the
            # `user` role the template asks for. A new account is not a dashboard user yet.
            "default-roles-papaia": ["offline_access", "uma_authorization"],
        }
        self.sessions: dict[str, list[dict[str, Any]]] = {}
        # What reached Keycloak, for assertions: (method, path relative to the realm).
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[Any] = []
        self.passwords: dict[str, tuple[str, bool]] = {}
        self.mails: list[str] = []
        self.logged_out: list[str] = []
        self.fail_mail = False
        # "METHOD /users/<id>/<rest>" suffixes that answer 500, to make a step fail half-way,
        # written as "METHOD <rest>": for example "POST /role-mappings/realm".
        self.fail_on: set[str] = set()
        self.reject_passwords = False
        # Whether the account holds manage-realm, which creating, changing and deleting a role
        # needs. Reading roles (view-realm) works without it.
        self.manage_realm = True
        # Roles of clients that a realm role contains, as Keycloak lists them next to the realm
        # roles: told apart by `clientRole` and `containerId`.
        self.client_composites: dict[str, list[dict[str, Any]]] = {
            "papaia-admin": [
                {
                    "id": f"rm-{right}",
                    "name": right,
                    "clientRole": True,
                    "containerId": "realm-management-uuid",
                    "composite": False,
                }
                for right in ("manage-users", "view-realm", "manage-realm")
            ]
        }
        for name, description in _ROLE_DEFAULTS.items():
            self.roles[name] = {
                "id": str(uuid.uuid4()),
                "name": name,
                "description": description,
                "composite": name in self.composites,
                "clientRole": False,
            }

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # -- seeding and inspection ------------------------------------------------

    def add_user(
        self,
        username: str,
        *,
        user_id: str | None = None,
        email: str = "",
        first: str = "",
        last: str = "",
        enabled: bool = True,
        roles: tuple[str, ...] = ("user",),
        actions: tuple[str, ...] = (),
        created: int = 1_760_000_000_000,
    ) -> str:
        identifier = user_id or str(uuid.uuid4())
        rep: dict[str, Any] = {
            "id": identifier,
            "username": username,
            "enabled": enabled,
            "emailVerified": False,
            "createdTimestamp": created,
            "requiredActions": list(actions),
        }
        if email:
            rep["email"] = email
        if first:
            rep["firstName"] = first
        if last:
            rep["lastName"] = last
        self.users[identifier] = rep
        self.direct[identifier] = set(roles)
        return identifier

    def add_session(
        self, user_id: str, *, ip: str = "10.0.0.5", clients: tuple[str, ...] = ("papaia-manager",)
    ) -> str:
        # Keycloak's session ids are 24-character base64url strings, not UUIDs.
        session_id = secrets.token_urlsafe(18)
        self.sessions.setdefault(user_id, []).append(
            {
                "id": session_id,
                "ipAddress": ip,
                "start": 1_760_000_000_000,
                "lastAccess": 1_760_000_600_000,
                "clients": {str(uuid.uuid4()): name for name in clients},
            }
        )
        return session_id

    def by_name(self, username: str) -> dict[str, Any]:
        return next(u for u in self.users.values() if u["username"] == username)

    def wrote(self) -> list[tuple[str, str]]:
        """Every call that changed something."""
        return [(m, p) for m, p in self.calls if m != "GET"]

    # -- the API ---------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        prefix = f"/admin/realms/{REALM}"
        path = request.url.path
        if not path.startswith(prefix):
            return _err(404, "Unknown realm")
        path = path[len(prefix) :]
        method = request.method
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401, json={"error": "HTTP 401 Unauthorized"})
        self.calls.append((method, path))
        if not self.allowed:
            return httpx.Response(403, json={"error": "HTTP 403 Forbidden"})
        body: Any = None
        if request.content:
            body = json.loads(request.content)
            self.bodies.append(body)
        params = dict(request.url.params)
        return self._route(method, path, params, body)

    def _route(self, method: str, path: str, params: dict[str, str], body: Any) -> httpx.Response:  # noqa: PLR0911, PLR0912
        if path == "" and method == "GET":
            smtp = {"host": "smtp.test", "password": "**********"} if self.smtp else {}
            return httpx.Response(200, json={"realm": REALM, "smtpServer": smtp})

        if path == "/users/count" and method == "GET":
            return httpx.Response(200, json=len(self._matching(params.get("search", ""))))
        if path == "/users" and method == "GET":
            found = self._matching(params.get("search", ""))
            first, limit = int(params.get("first", 0)), int(params.get("max", 100))
            return httpx.Response(200, json=found[first : first + limit])
        if path == "/users" and method == "POST":
            if any(u["username"] == body["username"].lower() for u in self.users.values()):
                return _err(409, "User exists with same username")
            # Like the real one: a new account holds the realm's default role, which is a
            # composite of `user`.
            identifier = self.add_user(body["username"].lower(), roles=("default-roles-papaia",))
            self.users[identifier].update({k: v for k, v in body.items() if k != "username"})
            return httpx.Response(
                201,
                headers={
                    "Location": f"https://kc.test{'/admin/realms/' + REALM}/users/{identifier}"
                },
            )

        match = re.fullmatch(r"/users/([^/]+)(/.*)?", path)
        if match:
            return self._user_route(method, match[1], match[2] or "", body)

        if path == "/roles" and method == "GET":
            return httpx.Response(200, json=list(self.roles.values()))
        match = re.fullmatch(r"/roles(?:/([^/]+)(/composites|/users)?)?", path)
        if match:
            return self._role_route(method, match[1], match[2] or "", params, body)

        match = re.fullmatch(r"/sessions/([^/]+)", path)
        if match and method == "DELETE":
            for rows in self.sessions.values():
                for row in rows:
                    if row["id"] == match[1]:
                        rows.remove(row)
                        return httpx.Response(204)
            return _err(404, "Session not found")
        return _err(404, f"No route for {method} {path}")

    def _role_route(  # noqa: PLR0911, PLR0912
        self, method: str, name: str | None, rest: str, params: dict[str, str], body: Any
    ) -> httpx.Response:
        """Create, read, change and delete a realm role, and what it contains."""
        if f"{method} /roles{rest}" in self.fail_on:
            return _err(500, "Keycloak had a problem")
        writes = method != "GET"
        if writes and not self.manage_realm:
            return httpx.Response(403, json={"error": "HTTP 403 Forbidden"})
        if name is None:  # POST /roles
            if body["name"] in self.roles:
                return _err(409, f"Role with name {body['name']} already exists")
            self.roles[body["name"]] = {
                "id": str(uuid.uuid4()),
                "name": body["name"],
                "description": body.get("description", ""),
                "composite": False,
                "clientRole": False,
            }
            return httpx.Response(201)
        role = self.roles.get(name)
        if role is None:
            return _err(404, "Could not find role")
        if rest == "":
            if method == "GET":
                return httpx.Response(200, json=role)
            if method == "PUT":
                role["description"] = body.get("description", "")
                return httpx.Response(204)
            if method == "DELETE":
                del self.roles[name]
                self.composites.pop(name, None)
                for members in self.composites.values():
                    if name in members:
                        members.remove(name)
                for held in self.direct.values():
                    held.discard(name)
                return httpx.Response(204)
        if rest == "/composites":
            members = self.composites.setdefault(name, [])
            if method == "GET":
                reps = [self.roles[m] for m in members if m in self.roles]
                return httpx.Response(200, json=[*reps, *self.client_composites.get(name, [])])
            names = [r["name"] for r in body if not r.get("clientRole")]
            if method == "POST":
                members.extend(n for n in names if n not in members)
            if method == "DELETE":
                for n in names:
                    if n in members:
                        members.remove(n)
            role["composite"] = bool(members or self.client_composites.get(name))
            return httpx.Response(204)
        if rest == "/users" and method == "GET":
            holders = [
                {"id": uid, "username": self.users[uid]["username"]}
                for uid, held in self.direct.items()
                if name in held
            ]
            first, limit = int(params.get("first", 0)), int(params.get("max", 100))
            return httpx.Response(200, json=holders[first : first + limit])
        return _err(404, f"No route for {method} /roles/{name}{rest}")

    def _matching(self, search: str) -> list[dict[str, Any]]:
        needle = search.lower()
        return [
            u
            for u in self.users.values()
            if not needle
            or any(
                needle in str(u.get(k, "")).lower()
                for k in ("username", "email", "firstName", "lastName")
            )
        ]

    def _effective(self, user_id: str) -> set[str]:
        names = set(self.direct[user_id])
        queue = list(names)
        while queue:
            for child in self.composites.get(queue.pop(), []):
                if child not in names:
                    names.add(child)
                    queue.append(child)
        return names

    def _user_route(self, method: str, user_id: str, rest: str, body: Any) -> httpx.Response:  # noqa: PLR0911, PLR0912
        if user_id not in self.users:
            return _err(404, "User not found")
        user = self.users[user_id]
        if rest == "":
            if method == "GET":
                return httpx.Response(200, json=user)
            if method == "PUT":
                self.users[user_id] = {**user, **body}
                return httpx.Response(204)
        if f"{method} {rest}" in self.fail_on:
            return _err(500, "Keycloak had a problem")
        if rest == "/reset-password" and method == "PUT" and self.reject_passwords:
            return _err(400, "invalidPasswordMinLengthMessage")
        if rest == "/reset-password" and method == "PUT":
            self.passwords[user_id] = (body["value"], bool(body["temporary"]))
            if body["temporary"] and "UPDATE_PASSWORD" not in user["requiredActions"]:
                user["requiredActions"].append("UPDATE_PASSWORD")
            return httpx.Response(204)
        if rest == "/execute-actions-email" and method == "PUT":
            if not self.smtp or self.fail_mail:
                return httpx.Response(
                    500, json={"errorMessage": "Failed to send execute actions email"}
                )
            self.mails.append(user_id)
            return httpx.Response(204)
        if rest == "/role-mappings/realm":
            if method == "GET":
                return httpx.Response(
                    200, json=[self.roles[n] for n in sorted(self.direct[user_id])]
                )
            names = {r["name"] for r in body}
            if method == "POST":
                self.direct[user_id] |= names
            if method == "DELETE":
                self.direct[user_id] -= names
            return httpx.Response(204)
        if rest == "/role-mappings/realm/composite" and method == "GET":
            return httpx.Response(
                200, json=[self.roles[n] for n in sorted(self._effective(user_id))]
            )
        if rest == "/sessions" and method == "GET":
            return httpx.Response(200, json=self.sessions.get(user_id, []))
        if rest == "/logout" and method == "POST":
            self.sessions[user_id] = []
            self.logged_out.append(user_id)
            return httpx.Response(204)
        return _err(404, f"No route for {method} /users/{user_id}{rest}")
