"""What the Roles page does, on top of `KeycloakUsers`: create, change and delete realm roles.

Like the Users page it acts with the signed-in account's own Keycloak token, and what Keycloak
lets that account do is the limit: creating, changing and deleting a role needs the
`manage-realm` right of the `realm-management` client, which the identity role carries (ADR 0006
of the core). The rules here are the manager's own:

* the roles the stack ships (the realm template's, and the three the manager itself is
  configured with) are shown but not changed or deleted: services refer to them by name, and
  a deleted `manager-admin` or `papaia-admin` would lock everybody out;
* a role cannot be renamed, because the name is what every service refers to; a different
  name is a different role;
* a role cannot contain itself, directly or through another role: Keycloak's composites are a
  graph, and a loop in it is a role that holds itself;
* Keycloak's own roles (`default-roles-*`, `offline_access`, `uma_authorization`) are never
  shown or touched.

Only the realm roles a composite contains are edited here. The roles of clients it may also
contain (`papaia-admin` carries some of `realm-management`) are shown and left as they are.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Literal

from app.core.keycloak_users import (
    KeycloakConflictError,
    KeycloakError,
    KeycloakForbiddenError,
    KeycloakNotFoundError,
    KeycloakUnauthorizedError,
    KeycloakUsers,
)
from app.core.users_service import InvalidInput, is_technical_role, require_known_roles

# The roles of the realm template: what the stack's services are configured with.
BUILT_IN_ROLES = frozenset(
    {
        "papaia-admin",
        "librechat-admin",
        "librechat-user",
        "litellm-admin",
        "manager-admin",
        "npm-admin",
        "user",
        "viewer",
        "finance",
        "localai-access",
        "qdrant-admin",
        "qdrant-ingest-operator",
    }
)

MAX_DESCRIPTION = 255
# How many of the accounts that hold a role are listed before "and more".
USERS_SHOWN = 100

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,126}$")
_LOOKUPS_AT_ONCE = 8
# A realm's roles are few; this only keeps a pathological graph from running away.
_MAX_WALK = 500

_FORBIDDEN_HINT = (
    "Your account cannot read the realm's roles. The role {role} has to carry the roles "
    "view-realm and manage-realm of the realm-management client. Add them in the Keycloak "
    "Admin Console under Realm roles > {role} > Associated roles (filter by clients), or use a "
    "papAIa release whose realm gives {role} these rights: `papaia-ctl start` then applies "
    "them to an existing installation. Sign in again afterwards."
)


class RolePartiallyCreated(Exception):  # noqa: N818 - a state of the role, not a failure
    """The role exists, but giving it its members failed."""

    def __init__(self, name: str, cause: KeycloakError) -> None:
        self.name = name
        self.cause = cause
        self.message = (
            f"The role {name} was created, but giving it its roles failed ({cause.detail}). "
            "Open it to finish."
        )
        super().__init__(self.message)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


@dataclass(frozen=True, slots=True)
class RoleRow:
    name: str
    description: str
    # The realm roles it contains, and the roles of clients (named, not told apart by client).
    members: list[str]
    client_members: list[str]
    built_in: bool
    # The role that lets an account use the manager's dashboard.
    default: bool

    @property
    def composite(self) -> bool:
        return bool(self.members or self.client_members)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "composite": self.composite,
            "members": self.members,
            "client_members": self.client_members,
            "built_in": self.built_in,
            "default": self.default,
        }


@dataclass(frozen=True, slots=True)
class RolesView:
    """The realm's roles, or the reason there are none to show."""

    state: Literal["ok", "forbidden", "unavailable"]
    reason: str = ""
    roles: list[RoleRow] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return self.state == "ok"

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reason": self.reason,
            "roles": [role.as_dict() for role in self.roles],
        }


@dataclass(frozen=True, slots=True)
class RoleDetail:
    role: RoleRow
    users: list[str]
    users_more: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.role.as_dict(),
            "users": self.users,
            "users_more": self.users_more,
        }


@dataclass(frozen=True, slots=True)
class RoleChange:
    name: str
    description_changed: bool
    added: list[str]
    removed: list[str]

    @property
    def changed(self) -> bool:
        return self.description_changed or bool(self.added or self.removed)


def validate_role(name: str, description: str, *, creating: bool = True) -> None:
    if creating:
        if not _NAME.match(name):
            raise InvalidInput(
                "A role name needs 1 to 127 letters, digits or . _ : - and must start with a "
                "letter or digit."
            )
        if is_technical_role(name):
            raise InvalidInput(f"{name} is one of Keycloak's own roles.")
    if len(description) > MAX_DESCRIPTION:
        raise InvalidInput(f"The description is limited to {MAX_DESCRIPTION} characters.")


class RolesService:
    """The actions of the Roles page, for one request."""

    def __init__(
        self,
        client: KeycloakUsers,
        *,
        protected: frozenset[str] = frozenset(),
        identity_role: str = "",
        default_role: str = "",
    ) -> None:
        self._client = client
        self._identity_role = identity_role
        self._default_role = default_role
        # The stack's own roles, and the three the manager is configured with.
        self._built_in = BUILT_IN_ROLES | protected

    # -- reading --------------------------------------------------------------------------

    def _row(self, rep: dict[str, Any], members: list[dict[str, Any]]) -> RoleRow:
        name = _text(rep.get("name"))
        return RoleRow(
            name=name,
            description=_text(rep.get("description")),
            members=sorted(
                n
                for m in members
                if not m.get("clientRole")
                and (n := _text(m.get("name")))
                and not is_technical_role(n)
            ),
            client_members=sorted(
                n for m in members if m.get("clientRole") and (n := _text(m.get("name")))
            ),
            built_in=name in self._built_in,
            default=name == self._default_role,
        )

    async def _members(self, rep: dict[str, Any]) -> list[dict[str, Any]]:
        if not rep.get("composite"):
            return []
        try:
            return await self._client.role_composites(_text(rep.get("name")))
        except KeycloakUnauthorizedError:
            raise
        except KeycloakError:
            return []

    async def snapshot(self) -> RolesView:
        """Every role with what it contains, or why there is nothing to show."""
        view = partial(RolesView)
        try:
            reps = await self._client.list_roles()
        except KeycloakUnauthorizedError:
            raise
        except KeycloakForbiddenError:
            return view(
                state="forbidden", reason=_FORBIDDEN_HINT.format(role=self._identity_role)
            )
        except KeycloakError as exc:
            return view(state="unavailable", reason=exc.detail)

        shown = [
            rep for rep in reps if (n := _text(rep.get("name"))) and not is_technical_role(n)
        ]
        gate = asyncio.Semaphore(_LOOKUPS_AT_ONCE)

        async def row_of(rep: dict[str, Any]) -> RoleRow:
            async with gate:
                members = await self._members(rep)
            return self._row(rep, members)

        rows = await asyncio.gather(*(row_of(rep) for rep in shown))
        return view(state="ok", roles=sorted(rows, key=lambda r: r.name))

    async def detail(self, name: str) -> RoleDetail:
        """One role, with the accounts that hold it (the first few, and whether there are more)."""
        self._require_shown(name)
        rep, users = await asyncio.gather(
            self._client.get_role(name), self._client.role_users(name, limit=USERS_SHOWN + 1)
        )
        row = self._row(rep, await self._members(rep))
        names = [n for u in users if (n := _text(u.get("username")))]
        return RoleDetail(role=row, users=names[:USERS_SHOWN], users_more=len(users) > USERS_SHOWN)

    # -- changing -------------------------------------------------------------------------

    async def _known(self) -> dict[str, dict[str, Any]]:
        return {
            n: rep
            for rep in await self._client.list_roles()
            if (n := _text(rep.get("name"))) and not is_technical_role(n)
        }

    @staticmethod
    def _require_shown(name: str) -> None:
        if is_technical_role(name):
            raise InvalidInput(f"{name} is one of Keycloak's own roles.")

    def _require_editable(self, name: str) -> None:
        self._require_shown(name)
        if name in self._built_in:
            raise InvalidInput(
                f"{name} is a built-in role of the stack: services refer to it by name, so it "
                "is not changed or deleted here."
            )

    async def _reaches(self, start: str, target: str) -> bool:
        """Whether `target` is `start` or lies somewhere below it in the composite graph."""
        seen = {start}
        queue = [start]
        while queue and len(seen) < _MAX_WALK:
            current = queue.pop()
            if current == target:
                return True
            for member in await self._client.role_composites(current):
                n = _text(member.get("name"))
                if not member.get("clientRole") and n and n not in seen:
                    seen.add(n)
                    queue.append(n)
        return target in seen

    async def create(
        self, *, name: str, description: str = "", members: list[str] | None = None
    ) -> RoleRow:
        name, description = name.strip(), description.strip()
        validate_role(name, description)
        chosen = sorted(set(members or []))
        known = await self._known()
        if name in known:
            raise KeycloakConflictError(409, f"A role named {name} already exists.")
        # Checked before anything is written: a mistyped member must not leave a role behind.
        require_known_roles(chosen, known)

        await self._client.create_role(name, description)
        if chosen:
            try:
                await self._client.add_role_composites(name, [known[m] for m in chosen])
            except KeycloakError as exc:
                raise RolePartiallyCreated(name, exc) from exc
        return RoleRow(
            name=name,
            description=description,
            members=chosen,
            client_members=[],
            built_in=False,
            default=name == self._default_role,
        )

    async def update(self, name: str, *, description: str, members: list[str]) -> RoleChange:
        """Set a role's description and the realm roles it contains."""
        self._require_editable(name)
        description = description.strip()
        validate_role(name, description, creating=False)
        known = await self._known()
        if name not in known:
            raise KeycloakNotFoundError(404, f"Role {name} was not found.")
        chosen = set(members)
        require_known_roles(sorted(chosen), known)

        rep = await self._client.get_role(name)
        current = {
            n
            for c in await self._client.role_composites(name)
            if not c.get("clientRole") and (n := _text(c.get("name")))
        }
        to_add = sorted(chosen - current)
        to_remove = sorted(current - chosen)
        for member in to_add:
            if await self._reaches(member, name):
                raise InvalidInput(
                    f"{name} cannot contain {member}: "
                    + (
                        "a role cannot contain itself."
                        if member == name
                        else f"{member} already contains {name}, which would make a loop."
                    )
                )

        description_changed = description != _text(rep.get("description"))
        if description_changed:
            await self._client.update_role(name, {**rep, "description": description})
        if to_add:
            await self._client.add_role_composites(name, [known[m] for m in to_add])
        if to_remove:
            await self._client.remove_role_composites(name, [known[m] for m in to_remove])
        return RoleChange(name, description_changed, to_add, to_remove)

    async def delete(self, name: str) -> tuple[int, bool]:
        """Delete a role. Returns how many accounts held it (up to the first few) and whether
        there were more."""
        self._require_editable(name)
        known = await self._known()
        if name not in known:
            raise KeycloakNotFoundError(404, f"Role {name} was not found.")
        users = await self._client.role_users(name, limit=USERS_SHOWN + 1)
        await self._client.delete_role(name)
        return min(len(users), USERS_SHOWN), len(users) > USERS_SHOWN
