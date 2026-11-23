"""Authorization policy: which realm roles grant which level of access.

These predicates are deliberately free of FastAPI types so that the same
rule is applied everywhere it is needed: by the route dependencies in
`app.auth.deps`, by the login callback (which runs before a session
exists), and by the page context that drives navigation rendering.

Keeping the policy in one module is what makes "the API is restricted the
same way the UI is" verifiable rather than a matter of discipline.
"""
from __future__ import annotations

from app.auth.oidc import OIDCClaims
from app.config import Settings


def is_admin(claims: OIDCClaims, settings: Settings) -> bool:
    """True if the account holds the configured admin role."""
    return settings.manager_admin_role in claims.roles


def is_user(claims: OIDCClaims, settings: Settings) -> bool:
    """True if the account holds the configured dashboard-user role."""
    return settings.manager_user_role in claims.roles


def is_identity_admin(claims: OIDCClaims, settings: Settings) -> bool:
    """True if the account may manage the realm's users and roles.

    A tier of its own above the admin role: the Users page acts with the Keycloak rights of
    the signed-in account, which only this role carries, so a plain administrator is denied
    here before Keycloak would have to refuse them.
    """
    return settings.manager_identity_admin_role in claims.roles


def is_auditor(claims: OIDCClaims, settings: Settings) -> bool:
    """True if the account holds the auditors' role (`papaia-auditor`).

    Kept apart from the admin role on purpose (separation of duties): an administrator does
    not see the inventory through being one, and an auditor administers nothing.
    """
    return settings.manager_auditor_role in claims.roles


def is_confirmer(claims: OIDCClaims, settings: Settings) -> bool:
    """True if the account may confirm a request for information or erasure.

    The page "Data subject requests" is where such a request is confirmed.

    The confirmation role (`papaia-confirm`) or the platform's administration role, the two
    roles papaia-audit accepts for it. papaia-audit checks them again on every call, and it
    alone decides whether this person is the one a request names.
    """
    return (
        settings.manager_confirm_role in claims.roles
        or settings.manager_identity_admin_role in claims.roles
    )


def has_manager_access(claims: OIDCClaims, settings: Settings) -> bool:
    """True if the account may use the manager at all.

    Admins are a superset of users -- they reach every surface a user
    reaches -- so this is an OR rather than a separate user-only tier. An auditor
    reaches the surfaces of a user too, plus the inventory, which names the role
    itself; so does a confirming person, plus the page "Data subject requests". An
    account holding none of them has no business getting a session.
    """
    return (
        is_admin(claims, settings)
        or is_user(claims, settings)
        or is_auditor(claims, settings)
        or is_confirmer(claims, settings)
    )
