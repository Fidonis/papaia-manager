"""What the Users page does, on top of `KeycloakUsers`.

The routes call this and nothing else. It holds the rules that are the manager's own:

* what a new account needs before Keycloak is asked (a username, an address when a mail is
  to be sent, a mail server in the realm when it is);
* the temporary password, generated here and returned once, never stored or logged;
* the roles that can be handed out (every realm role but Keycloak's technical ones);
* the lock-out guard: nobody disables their own account, takes their own identity-admin role
  away or ends all of their own sessions here. This is not a limit on what the role may do
  to others -- the role is the highest in the stack -- only protection against the one
  mistake that leaves nobody able to undo it from this page.

Everything else is Keycloak's to decide, with the rights of the signed-in account.
"""
from __future__ import annotations

import asyncio
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from typing import Any, Literal

from app.auth.oidc import OIDCClaims
from app.core.keycloak_users import (
    KeycloakError,
    KeycloakForbiddenError,
    KeycloakNotFoundError,
    KeycloakUnauthorizedError,
    KeycloakUsers,
)

Credential = Literal["temporary", "email", "none"]

PAGE_SIZE = 25
MAX_PAGE_SIZE = 100

# How many per-user role lookups run at once. A page of users is one request for the list
# and one for each user's roles: Keycloak's list has no roles in it.
_ROLE_LOOKUPS_AT_ONCE = 8

# Roles Keycloak keeps for itself: never offered, never touched.
_TECHNICAL_ROLES = frozenset({"offline_access", "uma_authorization"})

# What Keycloak accepts as a username is wider; this is what is sensible to type and to read
# in a list, and it keeps characters that mean something in a URL or an email out.
_USERNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{1,254}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MAX_TEXT = 255

# Without characters that look alike (0/O, 1/l/I): the password is read off a screen.
_UPPER = "ABCDEFGHJKLMNPQRSTUVWXYZ"
_LOWER = "abcdefghijkmnopqrstuvwxyz"
_DIGITS = "23456789"
_SYMBOLS = "-_.!#$%&*+=?@"
PASSWORD_LENGTH = 20

_FORBIDDEN_HINT = (
    "Your account does not have Keycloak's user-administration rights. The role {role} has "
    "to carry the roles manage-users, view-users, query-users, view-realm and manage-realm of "
    "the realm-management client. Add them in the Keycloak Admin Console under Realm roles > "
    "{role} > Associated roles (filter by clients), or use a papAIa release whose realm "
    "gives {role} these rights: `papaia-ctl start` then applies them to an existing "
    "installation. Sign in again afterwards."
)


class InvalidInput(ValueError):  # noqa: N818 - the message is for the person typing
    """The request is not acceptable as asked (422)."""


class PartiallyCreated(Exception):  # noqa: N818 - a state of the account, not a failure
    """The account exists, but a step after creating it failed.

    The route still has to record that the account was made, and the person has to be told
    what is missing, so this carries both: the account and what went wrong.
    """

    def __init__(self, username: str, step: str, cause: KeycloakError) -> None:
        self.username = username
        self.cause = cause
        self.message = (
            f"The account {username} was created, but {step} failed ({cause.detail}). "
            "Open its menu to finish."
        )
        super().__init__(self.message)


def is_technical_role(name: str) -> bool:
    return name in _TECHNICAL_ROLES or name.startswith("default-roles-")


def require_known_roles(wanted: list[str], known: dict[str, dict[str, Any]]) -> None:
    """Refuse a role that does not exist (or is Keycloak's own) before anything is written."""
    unknown = sorted({name for name in wanted if name not in known})
    if unknown:
        raise InvalidInput("Unknown role: " + ", ".join(unknown))


def generate_temporary_password() -> str:
    """A password for one sign-in: long, all four character classes, no look-alikes."""
    pools = (_UPPER, _LOWER, _DIGITS, _SYMBOLS)
    chars = [secrets.choice(pool) for pool in pools for _ in range(2)]
    everything = "".join(pools)
    chars += [secrets.choice(everything) for _ in range(PASSWORD_LENGTH - len(chars))]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def _when(millis: Any) -> datetime | None:
    if isinstance(millis, bool) or not isinstance(millis, int | float) or millis <= 0:
        return None
    return datetime.fromtimestamp(millis / 1000, tz=UTC)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


@dataclass(frozen=True, slots=True)
class UserRow:
    id: str
    username: str
    email: str
    first_name: str
    last_name: str
    enabled: bool
    email_verified: bool
    created: datetime | None
    password_change_pending: bool
    roles: list[str]
    is_self: bool

    @property
    def display_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def initial(self) -> str:
        source = self.display_name or self.username
        return source[:1].upper() if source else "?"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "username": self.username,
            "email": self.email,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "enabled": self.enabled,
            "email_verified": self.email_verified,
            "created": self.created.isoformat() if self.created else None,
            "password_change_pending": self.password_change_pending,
            "roles": self.roles,
            "is_self": self.is_self,
        }


@dataclass(frozen=True, slots=True)
class UsersView:
    """One page of users, or the reason there is none."""

    state: Literal["ok", "forbidden", "unavailable"]
    reason: str = ""
    users: list[UserRow] = field(default_factory=list)
    total: int = 0
    first: int = 0
    limit: int = PAGE_SIZE
    search: str = ""
    smtp: bool = False
    identity_role: str = ""
    # The realm role that lets an account use the manager's dashboard (MANAGER_USER_ROLE).
    default_role: str = ""

    @property
    def available(self) -> bool:
        return self.state == "ok"

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reason": self.reason,
            "users": [user.as_dict() for user in self.users],
            "total": self.total,
            "first": self.first,
            "limit": self.limit,
            "search": self.search,
            "smtp": self.smtp,
        }


@dataclass(frozen=True, slots=True)
class CreatedUser:
    id: str
    username: str
    # Returned once, to the person who asked for it. Not part of any log or audit entry.
    temporary_password: str | None = None
    email_sent: bool = False
    email_error: str | None = None
    roles: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RoleOption:
    name: str
    description: str
    direct: bool
    # Held through a composite role: shown, but taken away only by changing that role.
    inherited: bool
    locked: bool = False
    # The role that lets an account use the manager's dashboard: what a new account is
    # offered first, because the realm's default role does not carry it.
    default: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "direct": self.direct,
            "inherited": self.inherited,
            "locked": self.locked,
            "default": self.default,
        }


@dataclass(frozen=True, slots=True)
class RoleChange:
    username: str
    added: list[str]
    removed: list[str]


@dataclass(frozen=True, slots=True)
class SessionRow:
    id: str
    ip_address: str
    started: datetime | None
    last_access: datetime | None
    clients: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ip_address": self.ip_address,
            "started": self.started.isoformat() if self.started else None,
            "last_access": self.last_access.isoformat() if self.last_access else None,
            "clients": self.clients,
        }


def validate_new_user(
    *, username: str, email: str, first_name: str, last_name: str, credential: Credential
) -> None:
    if not _USERNAME.match(username):
        raise InvalidInput(
            "The username needs 2 to 255 letters, digits or . _ @ + - and must start with "
            "a letter or digit."
        )
    if email and (len(email) > _MAX_TEXT or not _EMAIL.match(email)):
        raise InvalidInput("That does not look like an email address.")
    if len(first_name) > _MAX_TEXT or len(last_name) > _MAX_TEXT:
        raise InvalidInput("Names are limited to 255 characters.")
    if credential == "email" and not email:
        raise InvalidInput("An email address is needed to send the link.")


class UsersService:
    """The actions of the Users page, for one signed-in account and one request."""

    def __init__(
        self,
        client: KeycloakUsers,
        *,
        actor: OIDCClaims,
        identity_role: str,
        default_role: str = "",
    ) -> None:
        self._client = client
        self._actor = actor
        self._identity_role = identity_role
        self._default_role = default_role

    # -- the list -------------------------------------------------------------------------

    async def snapshot(
        self, *, search: str = "", first: int = 0, limit: int = PAGE_SIZE
    ) -> UsersView:
        """One page of users with their roles, or why there is none.

        A refusal and an unreachable Keycloak are states of the page, not errors: the page
        says what to do about them instead of failing.
        """
        search = search.strip()
        first = max(first, 0)
        limit = min(max(limit, 1), MAX_PAGE_SIZE)
        view = partial(
            UsersView,
            search=search,
            first=first,
            limit=limit,
            identity_role=self._identity_role,
            default_role=self._default_role,
        )
        try:
            raw, total = await asyncio.gather(
                self._client.list_users(search=search, first=first, limit=limit),
                self._client.count_users(search=search),
            )
        except KeycloakUnauthorizedError:
            # Not a state of the page: the session is over, and the route answers 401.
            raise
        except KeycloakForbiddenError:
            return view(
                state="forbidden",
                reason=_FORBIDDEN_HINT.format(role=self._identity_role),
            )
        except KeycloakError as exc:
            return view(state="unavailable", reason=exc.detail)

        gate = asyncio.Semaphore(_ROLE_LOOKUPS_AT_ONCE)

        async def roles_of(user_id: str) -> list[str]:
            async with gate:
                try:
                    reps = await self._client.direct_roles(user_id)
                except KeycloakError:
                    return []
            return sorted(
                name for r in reps if (name := _text(r.get("name"))) and not is_technical_role(name)
            )

        role_lists, smtp = await asyncio.gather(
            asyncio.gather(*(roles_of(str(u.get("id", ""))) for u in raw)),
            self._has_smtp(),
        )
        users = [self._row(rep, roles) for rep, roles in zip(raw, role_lists, strict=True)]
        return view(state="ok", users=users, total=total, smtp=smtp)

    def _row(self, rep: dict[str, Any], roles: list[str]) -> UserRow:
        user_id = str(rep.get("id", ""))
        actions = rep.get("requiredActions")
        return UserRow(
            id=user_id,
            username=_text(rep.get("username")),
            email=_text(rep.get("email")),
            first_name=_text(rep.get("firstName")),
            last_name=_text(rep.get("lastName")),
            enabled=bool(rep.get("enabled", True)),
            email_verified=bool(rep.get("emailVerified", False)),
            created=_when(rep.get("createdTimestamp")),
            password_change_pending=isinstance(actions, list) and "UPDATE_PASSWORD" in actions,
            roles=roles,
            is_self=user_id == self._actor.sub,
        )

    async def _has_smtp(self) -> bool:
        """A realm that cannot be read for this is treated as one without a mail server."""
        try:
            return await self._client.realm_has_smtp()
        except KeycloakError:
            return False

    # -- accounts -------------------------------------------------------------------------

    async def create(
        self,
        *,
        username: str,
        email: str = "",
        first_name: str = "",
        last_name: str = "",
        credential: Credential = "temporary",
        roles: list[str] | None = None,
    ) -> CreatedUser:
        # Keycloak keeps usernames in lower case; saying so here keeps the answer, the list
        # and the audit entry from spelling the same account two ways.
        username, email = username.strip().lower(), email.strip()
        first_name, last_name = first_name.strip(), last_name.strip()
        validate_new_user(
            username=username,
            email=email,
            first_name=first_name,
            last_name=last_name,
            credential=credential,
        )
        if credential == "email" and not await self._has_smtp():
            raise InvalidInput(
                "The realm has no mail server configured, so the link cannot be sent."
            )

        # Checked before anything is written: a mistyped role must not leave an account behind.
        chosen = sorted(set(roles or []))
        known = await self._known_roles()
        self._require_known(chosen, known)

        representation: dict[str, Any] = {"username": username, "enabled": True}
        if email:
            representation["email"] = email
        if first_name:
            representation["firstName"] = first_name
        if last_name:
            representation["lastName"] = last_name
        user_id = await self._client.create_user(representation)
        if chosen:
            try:
                await self._client.add_roles(user_id, [known[name] for name in chosen])
            except KeycloakError as exc:
                raise PartiallyCreated(username, "giving it its roles", exc) from exc

        if credential == "temporary":
            password = generate_temporary_password()
            try:
                await self._client.set_password(user_id, password, temporary=True)
            except KeycloakError as exc:
                raise PartiallyCreated(username, "setting its password", exc) from exc
            return CreatedUser(
                id=user_id, username=username, temporary_password=password, roles=chosen
            )
        if credential == "email":
            # The account exists either way; a mail that fails is reported, not undone.
            try:
                await self._client.send_password_email(user_id)
            except KeycloakError as exc:
                return CreatedUser(
                    id=user_id,
                    username=username,
                    email_sent=False,
                    email_error=exc.detail,
                    roles=chosen,
                )
            return CreatedUser(id=user_id, username=username, email_sent=True, roles=chosen)
        return CreatedUser(id=user_id, username=username, roles=chosen)

    async def set_enabled(self, user_id: str, enabled: bool) -> tuple[str, bool]:
        """Enable or disable an account. Returns the username and whether its sessions ended.

        Disabling also ends the account's sessions: a departed employee should not stay
        signed in until the next token refresh happens to fail.
        """
        if not enabled and user_id == self._actor.sub:
            raise InvalidInput("You cannot disable your own account.")
        user = await self._client.get_user(user_id)
        # The whole representation goes back: with a declarative user profile a partial one
        # is validated as if the missing fields had been cleared.
        await self._client.update_user(user_id, {**user, "enabled": enabled})
        ended = False
        if not enabled:
            try:
                await self._client.logout_user(user_id)
                ended = True
            except KeycloakError:
                ended = False
        return _text(user.get("username")), ended

    # -- passwords ------------------------------------------------------------------------

    async def temporary_password(self, user_id: str) -> tuple[str, str]:
        """Give the account a new temporary password. Returns the username and the password."""
        user = await self._client.get_user(user_id)
        password = generate_temporary_password()
        await self._client.set_password(user_id, password, temporary=True)
        return _text(user.get("username")), password

    async def password_email(self, user_id: str) -> str:
        """Mail the account Keycloak's "update your password" link. Returns the username."""
        user = await self._client.get_user(user_id)
        if not _text(user.get("email")):
            raise InvalidInput("This account has no email address.")
        if not await self._has_smtp():
            raise InvalidInput(
                "The realm has no mail server configured, so the link cannot be sent."
            )
        await self._client.send_password_email(user_id)
        return _text(user.get("username"))

    # -- roles ----------------------------------------------------------------------------

    async def _known_roles(self) -> dict[str, dict[str, Any]]:
        """The realm roles that can be handed out, by name."""
        return {
            name: role
            for role in await self._client.list_roles()
            if (name := _text(role.get("name"))) and not is_technical_role(name)
        }

    @staticmethod
    def _require_known(wanted: list[str], known: dict[str, dict[str, Any]]) -> None:
        require_known_roles(wanted, known)

    async def assignable_roles(self) -> list[RoleOption]:
        """The roles a new account can be given, with the dashboard role marked."""
        known = await self._known_roles()
        return [
            RoleOption(
                name=name,
                description=_text(role.get("description")),
                direct=False,
                inherited=False,
                default=name == self._default_role,
            )
            for name, role in sorted(known.items())
        ]

    async def role_options(self, user_id: str) -> tuple[str, list[RoleOption]]:
        """Every role that can be handed out, marked with what the account holds."""
        user, realm_roles, direct, effective = await asyncio.gather(
            self._client.get_user(user_id),
            self._client.list_roles(),
            self._client.direct_roles(user_id),
            self._client.effective_roles(user_id),
        )
        held = {_text(r.get("name")) for r in direct}
        inherited = {_text(r.get("name")) for r in effective} - held
        is_self = user_id == self._actor.sub
        options = [
            RoleOption(
                name=name,
                description=_text(role.get("description")),
                direct=name in held,
                inherited=name in inherited,
                locked=is_self and name == self._identity_role,
                default=name == self._default_role,
            )
            for role in realm_roles
            if (name := _text(role.get("name"))) and not is_technical_role(name)
        ]
        options.sort(key=lambda option: option.name)
        return _text(user.get("username")), options

    async def set_roles(self, user_id: str, wanted: list[str]) -> RoleChange:
        """Make the account's own realm roles the given set.

        Only the roles that can be handed out take part: a technical role the account holds
        is neither removed nor required. A role held through a composite is not "held" here,
        so naming it adds it as a role of its own.
        """
        user, known, direct = await asyncio.gather(
            self._client.get_user(user_id),
            self._known_roles(),
            self._client.direct_roles(user_id),
        )
        self._require_known(wanted, known)

        current = {_text(r.get("name")) for r in direct} & set(known)
        desired = set(wanted)
        to_add = sorted(desired - current)
        to_remove = sorted(current - desired)
        if user_id == self._actor.sub and self._identity_role in to_remove:
            raise InvalidInput(
                f"You cannot take the {self._identity_role} role away from your own account."
            )

        if to_add:
            await self._client.add_roles(user_id, [known[name] for name in to_add])
        if to_remove:
            await self._client.remove_roles(user_id, [known[name] for name in to_remove])
        return RoleChange(_text(user.get("username")), to_add, to_remove)

    # -- sessions -------------------------------------------------------------------------

    async def sessions(self, user_id: str) -> tuple[str, list[SessionRow]]:
        user, reps = await asyncio.gather(
            self._client.get_user(user_id), self._client.user_sessions(user_id)
        )
        rows = [
            SessionRow(
                id=_text(rep.get("id")),
                ip_address=_text(rep.get("ipAddress")),
                started=_when(rep.get("start")),
                last_access=_when(rep.get("lastAccess")),
                clients=sorted(
                    str(name) for name in (rep.get("clients") or {}).values() if name
                ),
            )
            for rep in reps
        ]
        rows.sort(key=lambda row: row.last_access or datetime.min.replace(tzinfo=UTC), reverse=True)
        return _text(user.get("username")), rows

    async def end_session(self, user_id: str, session_id: str) -> str:
        """End one session of the account. Returns the username."""
        username, rows = await self.sessions(user_id)
        if session_id not in {row.id for row in rows}:
            raise KeycloakNotFoundError(404, "That session no longer exists.")
        await self._client.delete_session(session_id)
        return username

    async def end_all_sessions(self, user_id: str) -> tuple[str, int]:
        """End every session of the account. Returns the username and how many there were."""
        if user_id == self._actor.sub:
            raise InvalidInput(
                "Ending all of your own sessions would sign you out of the manager too. "
                "Sign out instead."
            )
        username, rows = await self.sessions(user_id)
        await self._client.logout_user(user_id)
        return username, len(rows)
