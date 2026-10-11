"""The signed-in user's own access token, for calls that act with their rights.

The Users page does not use a service account or a credential of its own: it calls
Keycloak's Admin API with the access token of whoever is signed in, so Keycloak applies that
account's rights and nothing more. The session only keeps the refresh token (a cookie has no
room for a second token, and an access token in a cookie would be one more place for it), so
the access token is obtained from it on demand and held in memory for the few minutes it
lives.

The cache assumes one process, like the refresh state in `app.auth.deps`: the image runs a
single uvicorn worker. With more workers each would simply ask Keycloak for its own token.
"""
from __future__ import annotations

import asyncio
import hashlib
import time

from fastapi import Request

from app.auth.oidc import OIDCError, get_oidc_client

# Ask for a new token this long before the cached one expires, so a request that is already
# on its way does not arrive with a token that dies in transit.
_SKEW_SECONDS = 30.0

# sha256(refresh token) -> (expires_at, access token). Keyed by the refresh token the cookie
# holds, which changes when Keycloak rotates it; both the old and the new key are filled so a
# second tab still carrying the old cookie is served from the same entry.
_cache: dict[str, tuple[float, str]] = {}
_locks: dict[str, asyncio.Lock] = {}


class UserTokenUnavailable(Exception):  # noqa: N818 - a state of the session, not a failure
    """The session cannot produce an access token: no refresh token, or Keycloak refused it."""


def _key(refresh_token: str) -> str:
    return hashlib.sha256(refresh_token.encode()).hexdigest()


def _prune(now: float) -> None:
    for key in [k for k, (expires_at, _) in _cache.items() if expires_at <= now]:
        _cache.pop(key, None)
    if len(_locks) > 512:
        for key in [k for k in _locks if k not in _cache]:
            _locks.pop(key, None)


def clear() -> None:
    """Forget every cached token (tests)."""
    _cache.clear()
    _locks.clear()


async def user_access_token(request: Request) -> str:
    """The access token of the account behind this request's session.

    Raises `UserTokenUnavailable` when the session has no refresh token or Keycloak no longer
    accepts it, which is the normal outcome once the SSO session has ended. The caller turns
    that into a 401, and the browser signs in again.
    """
    oidc_state = request.session.get("oidc")
    refresh_token = oidc_state.get("refresh_token") if isinstance(oidc_state, dict) else None
    if not isinstance(refresh_token, str) or not refresh_token:
        raise UserTokenUnavailable("the session has no refresh token")

    key = _key(refresh_token)
    cached = _cache.get(key)
    if cached is not None and cached[0] - time.time() > _SKEW_SECONDS:
        return cached[1]

    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        now = time.time()
        _prune(now)
        # Another request may have refreshed while this one waited for the lock.
        cached = _cache.get(key)
        if cached is not None and cached[0] - now > _SKEW_SECONDS:
            return cached[1]

        try:
            token_set = await get_oidc_client().refresh(refresh_token=refresh_token)
        except OIDCError as exc:
            raise UserTokenUnavailable("Keycloak no longer accepts the session") from exc
        if token_set.access_token is None:
            raise UserTokenUnavailable("Keycloak answered without an access token")

        rotated = token_set.refresh_token or refresh_token
        # A response without `expires_in` is held for a minute rather than trusted for long.
        expires_at = token_set.access_expires_at or now + 60.0
        _cache[key] = (expires_at, token_set.access_token)
        _cache[_key(rotated)] = (expires_at, token_set.access_token)

        # Keep the session in step with what Keycloak just issued, like the renewal in
        # `app.auth.deps` does: the next request must present the newest refresh token.
        request.session["user"] = token_set.claims.to_dict()
        request.session["oidc"] = {"refresh_token": rotated}
        return token_set.access_token
