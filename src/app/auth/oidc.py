"""OIDC Authorization Code Flow client with PKCE (S256)."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx
from jose import jwt
from jose.exceptions import ExpiredSignatureError, JWTError

logger = logging.getLogger(__name__)

# Only asymmetric algorithms accepted; HS* and 'none' are explicitly excluded
# to prevent algorithm-confusion attacks.
_ALLOWED_ALGS: frozenset[str] = frozenset(
    {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512"}
)


class OIDCError(Exception):
    """Raised when an OIDC operation cannot be completed."""


class OIDCClaims:
    """Validated OIDC claims stored in the manager session."""

    __slots__ = ("sub", "preferred_username", "roles", "exp")

    def __init__(
        self,
        *,
        sub: str,
        preferred_username: str,
        roles: list[str],
        exp: int,
    ) -> None:
        self.sub = sub
        self.preferred_username = preferred_username
        self.roles = roles
        self.exp = exp

    @property
    def is_expired(self) -> bool:
        return time.time() > self.exp

    def to_dict(self) -> dict[str, Any]:
        return {
            "sub": self.sub,
            "preferred_username": self.preferred_username,
            "roles": self.roles,
            "exp": self.exp,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OIDCClaims:
        return cls(
            sub=str(data["sub"]),
            preferred_username=str(data.get("preferred_username", data["sub"])),
            roles=[str(r) for r in data.get("roles", [])],
            exp=int(data["exp"]),
        )


class OIDCClient:
    """OIDC Authorization Code + PKCE S256 client for papaia-manager.

    One instance is created at startup and shared across requests.
    JWKS responses are cached with a configurable TTL and refreshed once
    on unknown `kid` to handle key rotation gracefully.
    """

    def __init__(
        self,
        *,
        auth_endpoint: str,
        token_endpoint: str,
        jwks_endpoint: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        role_claim: str = "roles",
        jwks_cache_ttl: int = 300,
        http_timeout: float = 10.0,
        ssl_cert_file: str | None = None,
    ) -> None:
        self._auth_endpoint = auth_endpoint
        self._token_endpoint = token_endpoint
        self._jwks_endpoint = jwks_endpoint
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._role_claim = role_claim
        self._jwks_cache_ttl = jwks_cache_ttl
        self._http_timeout = http_timeout
        self._ssl_verify: bool | str = ssl_cert_file if ssl_cert_file else True

        self._jwks: dict[str, Any] | None = None
        self._jwks_fetched_at: float = 0.0
        self._jwks_lock = asyncio.Lock()

    def build_auth_redirect(self) -> tuple[str, str, str]:
        """Return (redirect_url, state, code_verifier) for the login redirect.

        state and code_verifier must be stored in the session until the callback.
        """
        state = secrets.token_urlsafe(32)
        verifier, challenge = _pkce_pair()
        params = {
            "response_type": "code",
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "scope": "openid profile",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        url = f"{self._auth_endpoint}?{urlencode(params)}"
        return url, state, verifier

    def build_auth_url(self, state: str, challenge: str) -> str:
        """Return an authorization URL with caller-supplied state and PKCE challenge."""
        params = {
            "response_type": "code",
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "scope": "openid profile",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return f"{self._auth_endpoint}?{urlencode(params)}"

    async def exchange_code(self, *, code: str, code_verifier: str) -> OIDCClaims:
        """Exchange an authorization code for validated OIDC claims."""
        token_response = await self._fetch_tokens(code=code, code_verifier=code_verifier)
        id_token = token_response.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            raise OIDCError("Token response missing id_token")
        return await self._validate_id_token(id_token)

    async def _fetch_tokens(self, *, code: str, code_verifier: str) -> dict[str, Any]:
        data = {
            "grant_type": "authorization_code",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "redirect_uri": self._redirect_uri,
            "code": code,
            "code_verifier": code_verifier,
        }
        async with httpx.AsyncClient(
            timeout=self._http_timeout, verify=self._ssl_verify
        ) as client:
            response = await client.post(self._token_endpoint, data=data)
        if response.status_code != 200:
            logger.warning("token endpoint returned HTTP %d", response.status_code)
            raise OIDCError("token exchange failed")
        result: dict[str, Any] = response.json()
        return result

    async def _validate_id_token(self, id_token: str) -> OIDCClaims:
        try:
            header = jwt.get_unverified_header(id_token)
        except JWTError as exc:
            raise OIDCError("malformed id_token header") from exc

        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise OIDCError("id_token header missing kid")

        key = await self._resolve_key(kid)
        alg = _algorithm_for_key(key)

        try:
            unverified = jwt.get_unverified_claims(id_token)
            logger.info(
                "id_token claims: aud=%r iss=%r sub=%r",
                unverified.get("aud"),
                unverified.get("iss"),
                unverified.get("sub"),
            )
        except JWTError:
            pass

        try:
            payload = jwt.decode(
                id_token,
                key,
                algorithms=[alg],
                audience=self._client_id,
                options={
                    "verify_signature": True,
                    "verify_aud": True,
                    "verify_exp": True,
                },
            )
        except ExpiredSignatureError as exc:
            raise OIDCError("id_token expired") from exc
        except JWTError as exc:
            logger.warning(
                "id_token validation failed: %s: %s (client_id=%r)",
                exc.__class__.__name__,
                exc,
                self._client_id,
            )
            raise OIDCError("id_token validation failed") from exc

        return _extract_claims(payload, self._role_claim)

    async def _resolve_key(self, kid: str) -> dict[str, Any]:
        jwks = await self._get_jwks()
        key = _find_jwk(jwks, kid)
        if key is None:
            logger.info("unknown kid %s, refreshing JWKS", kid)
            jwks = await self._get_jwks(force_refresh=True)
            key = _find_jwk(jwks, kid)
        if key is None:
            raise OIDCError("signing key not found in JWKS")
        return key

    async def _get_jwks(self, *, force_refresh: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if (
            not force_refresh
            and self._jwks is not None
            and (now - self._jwks_fetched_at) < self._jwks_cache_ttl
        ):
            return self._jwks

        async with self._jwks_lock:
            if (
                not force_refresh
                and self._jwks is not None
                and (time.monotonic() - self._jwks_fetched_at) < self._jwks_cache_ttl
            ):
                return self._jwks

            async with httpx.AsyncClient(
                timeout=self._http_timeout, verify=self._ssl_verify
            ) as client:
                response = await client.get(self._jwks_endpoint)
                response.raise_for_status()
                self._jwks = response.json()
                self._jwks_fetched_at = time.monotonic()

        assert self._jwks is not None
        return self._jwks


def _pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE S256."""
    raw = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=")
    verifier = raw.decode()
    digest = hashlib.sha256(raw).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _find_jwk(jwks: dict[str, Any], kid: str) -> dict[str, Any] | None:
    for key in jwks.get("keys", []):
        if key.get("kid") != kid:
            continue
        # Skip encryption-only keys; use=None means unspecified (accept as sig).
        if key.get("use") not in (None, "sig"):
            continue
        return key  # type: ignore[no-any-return]
    return None


def _algorithm_for_key(key: dict[str, Any]) -> str:
    """Derive signing algorithm from the JWK, never from the token header."""
    alg = key.get("alg")
    if alg is None:
        kty = key.get("kty")
        if kty == "RSA":
            return "RS256"
        if kty == "EC":
            return "ES256"
        raise OIDCError(f"unsupported JWK key type: {kty!r}")
    if alg not in _ALLOWED_ALGS:
        raise OIDCError(f"JWK algorithm not permitted: {alg!r}")
    return str(alg)


def _extract_claims(payload: dict[str, Any], role_claim: str) -> OIDCClaims:
    sub = payload.get("sub")
    if not isinstance(sub, str) or not sub:
        raise OIDCError("id_token missing required sub claim")

    raw_roles = payload.get(role_claim, [])
    roles: list[str] = [str(r) for r in raw_roles] if isinstance(raw_roles, list) else []

    exp = payload.get("exp")
    if not isinstance(exp, (int, float)):
        raise OIDCError("id_token missing exp claim")

    preferred_username = str(payload.get("preferred_username", sub))

    return OIDCClaims(
        sub=sub,
        preferred_username=preferred_username,
        roles=roles,
        exp=int(exp),
    )
