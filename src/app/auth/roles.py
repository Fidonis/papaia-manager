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


def has_manager_access(claims: OIDCClaims, settings: Settings) -> bool:
    """True if the account may use the manager at all.

    Admins are a superset of users -- they reach every surface a user
    reaches -- so this is an OR rather than a separate user-only tier.
    An account holding neither role has no business getting a session.
    """
    return is_admin(claims, settings) or is_user(claims, settings)
