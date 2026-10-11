"""Unit tests for OIDC auth helpers."""
from __future__ import annotations

import base64
import hashlib
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from app.auth.oidc import (
    OIDCClaims,
    OIDCClient,
    OIDCError,
    TokenSet,
    _algorithm_for_key,
    _extract_claims,
    _find_jwk,
    _pkce_pair,
)
from app.routers.auth import _safe_next

# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------


def test_pkce_pair_produces_s256_challenge() -> None:
    verifier, challenge = _pkce_pair()
    # challenge must equal BASE64URL(SHA256(verifier as ASCII bytes))
    raw = verifier.encode("ascii")
    expected = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b"=").decode()
    assert challenge == expected


def test_pkce_verifier_url_safe() -> None:
    verifier, _ = _pkce_pair()
    assert set(verifier) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def test_pkce_pair_unique() -> None:
    pairs = [_pkce_pair() for _ in range(50)]
    verifiers = [p[0] for p in pairs]
    assert len(set(verifiers)) == 50


# ---------------------------------------------------------------------------
# OIDCClaims serialisation
# ---------------------------------------------------------------------------


def _make_claims(**overrides: object) -> OIDCClaims:
    defaults: dict[str, object] = {
        "sub": "user-abc",
        "preferred_username": "alice",
        "roles": ["admin"],
        "exp": int(time.time()) + 3600,
    }
    defaults.update(overrides)
    return OIDCClaims(**defaults)  # type: ignore[arg-type]


def test_claims_roundtrip() -> None:
    c = _make_claims()
    assert OIDCClaims.from_dict(c.to_dict()).sub == c.sub
    assert OIDCClaims.from_dict(c.to_dict()).preferred_username == c.preferred_username
    assert OIDCClaims.from_dict(c.to_dict()).roles == c.roles
    assert OIDCClaims.from_dict(c.to_dict()).exp == c.exp


def test_claims_not_expired() -> None:
    assert not _make_claims(exp=int(time.time()) + 60).is_expired


def test_claims_expired() -> None:
    assert _make_claims(exp=int(time.time()) - 1).is_expired


def test_from_dict_falls_back_username_to_sub() -> None:
    data = {"sub": "u1", "exp": int(time.time()) + 100}
    c = OIDCClaims.from_dict(data)
    assert c.preferred_username == "u1"


def test_from_dict_empty_roles_default() -> None:
    data = {"sub": "u1", "exp": int(time.time()) + 100}
    c = OIDCClaims.from_dict(data)
    assert c.roles == []


# ---------------------------------------------------------------------------
# _extract_claims
# ---------------------------------------------------------------------------


def test_extract_claims_missing_sub() -> None:
    with pytest.raises(OIDCError, match="sub"):
        _extract_claims({"exp": 9999}, "roles")


def test_extract_claims_missing_exp() -> None:
    with pytest.raises(OIDCError, match="exp"):
        _extract_claims({"sub": "u1"}, "roles")


def test_extract_claims_reads_custom_role_claim() -> None:
    payload = {
        "sub": "u2",
        "exp": int(time.time()) + 100,
        "custom_roles": ["manager-admin", "viewer"],
    }
    claims = _extract_claims(payload, "custom_roles")
    assert "manager-admin" in claims.roles


def test_extract_claims_non_list_role_claim_ignored() -> None:
    payload = {"sub": "u3", "exp": int(time.time()) + 100, "roles": "not-a-list"}
    claims = _extract_claims(payload, "roles")
    assert claims.roles == []


# ---------------------------------------------------------------------------
# _find_jwk
# ---------------------------------------------------------------------------


def _jwks(*keys: dict[str, object]) -> dict[str, object]:
    return {"keys": list(keys)}


def test_find_jwk_by_kid() -> None:
    key = {"kid": "k1", "kty": "RSA", "use": "sig"}
    result = _find_jwk(_jwks(key), "k1")  # type: ignore[arg-type]
    assert result is key


def test_find_jwk_missing_kid_returns_none() -> None:
    key = {"kid": "k1", "kty": "RSA"}
    assert _find_jwk(_jwks(key), "k2") is None  # type: ignore[arg-type]


def test_find_jwk_skips_enc_key() -> None:
    enc_key = {"kid": "k1", "kty": "RSA", "use": "enc"}
    sig_key = {"kid": "k2", "kty": "RSA", "use": "sig"}
    assert _find_jwk(_jwks(enc_key), "k1") is None  # type: ignore[arg-type]
    assert _find_jwk(_jwks(sig_key), "k2") is sig_key  # type: ignore[arg-type]


def test_find_jwk_accepts_no_use() -> None:
    key = {"kid": "k1", "kty": "RSA"}
    assert _find_jwk(_jwks(key), "k1") is key  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _algorithm_for_key
# ---------------------------------------------------------------------------


def test_algorithm_rsa_default() -> None:
    assert _algorithm_for_key({"kty": "RSA"}) == "RS256"


def test_algorithm_ec_default() -> None:
    assert _algorithm_for_key({"kty": "EC"}) == "ES256"


def test_algorithm_explicit_allowed() -> None:
    assert _algorithm_for_key({"alg": "RS384"}) == "RS384"


def test_algorithm_explicit_not_allowed() -> None:
    with pytest.raises(OIDCError, match="not permitted"):
        _algorithm_for_key({"alg": "HS256"})


def test_algorithm_unknown_kty() -> None:
    with pytest.raises(OIDCError, match="unsupported"):
        _algorithm_for_key({"kty": "oct"})


# ---------------------------------------------------------------------------
# Token exchange and refresh
# ---------------------------------------------------------------------------


_CLAIMS = OIDCClaims(
    sub="u-1",
    preferred_username="alice",
    roles=["admin"],
    exp=int(time.time()) + 3600,
)


def _client() -> OIDCClient:
    return OIDCClient(
        auth_endpoint="https://kc.example/auth",
        token_endpoint="https://kc.example/token",
        jwks_endpoint="https://kc.example/certs",
        client_id="papaia-manager",
        client_secret="secret",
        redirect_uri="https://manager.example/auth/callback",
    )


def _areturn(value: object):
    """A stand-in for an async method that just resolves to `value`."""

    async def _inner(*_args: object, **_kwargs: object) -> object:
        return value

    return _inner


async def test_exchange_code_returns_a_token_set(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client()
    monkeypatch.setattr(
        client,
        "_token_request",
        _areturn({"id_token": "x", "access_token": "a", "refresh_token": "rt"}),
    )
    monkeypatch.setattr(client, "_validate_id_token", _areturn(_CLAIMS))

    token_set = await client.exchange_code(code="c", code_verifier="v")

    assert isinstance(token_set, TokenSet)
    assert token_set.refresh_token == "rt"
    assert token_set.claims.roles == ["admin"]


async def test_refresh_returns_the_rotated_token(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client()
    monkeypatch.setattr(
        client,
        "_token_request",
        _areturn({"id_token": "x", "access_token": "a", "refresh_token": "new-rt"}),
    )
    monkeypatch.setattr(client, "_validate_id_token", _areturn(_CLAIMS))

    token_set = await client.refresh(refresh_token="old-rt")

    assert token_set.refresh_token == "new-rt"
    assert token_set.claims.sub == "u-1"


async def test_refresh_keeps_the_current_token_when_none_is_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()
    monkeypatch.setattr(
        client, "_token_request", _areturn({"id_token": "x", "access_token": "a"})
    )
    monkeypatch.setattr(client, "_validate_id_token", _areturn(_CLAIMS))

    token_set = await client.refresh(refresh_token="old-rt")

    assert token_set.refresh_token == "old-rt"


async def test_the_access_token_and_its_expiry_travel_with_the_token_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()
    monkeypatch.setattr(
        client,
        "_token_request",
        _areturn(
            {"id_token": "x", "access_token": "a-1", "refresh_token": "rt", "expires_in": 300}
        ),
    )
    monkeypatch.setattr(client, "_validate_id_token", _areturn(_CLAIMS))

    before = time.time()
    token_set = await client.refresh(refresh_token="old-rt")

    assert token_set.access_token == "a-1"
    assert before + 299 <= token_set.access_expires_at <= time.time() + 301


async def test_the_access_token_survives_a_refresh_that_keeps_the_refresh_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()
    monkeypatch.setattr(
        client,
        "_token_request",
        _areturn({"id_token": "x", "access_token": "a-2", "expires_in": 60}),
    )
    monkeypatch.setattr(client, "_validate_id_token", _areturn(_CLAIMS))

    token_set = await client.refresh(refresh_token="old-rt")

    assert (token_set.refresh_token, token_set.access_token) == ("old-rt", "a-2")
    assert token_set.access_expires_at > 0


async def test_a_response_without_expiry_leaves_it_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()
    monkeypatch.setattr(client, "_token_request", _areturn({"id_token": "x", "access_token": "a"}))
    monkeypatch.setattr(client, "_validate_id_token", _areturn(_CLAIMS))

    assert (await client.refresh(refresh_token="r")).access_expires_at == 0.0


async def test_refresh_propagates_a_token_endpoint_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()

    async def _boom(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise OIDCError("token request failed with HTTP 400")

    monkeypatch.setattr(client, "_token_request", _boom)

    with pytest.raises(OIDCError):
        await client.refresh(refresh_token="dead-rt")


# ---------------------------------------------------------------------------
# End of the provider's session (sign-out)
# ---------------------------------------------------------------------------

_LOGOUT_URL = "https://kc.example/logout"


def _logout_client() -> OIDCClient:
    return OIDCClient(
        auth_endpoint="https://kc.example/auth",
        token_endpoint="https://kc.example/token",
        jwks_endpoint="https://kc.example/certs",
        client_id="papaia-manager",
        client_secret="secret",
        redirect_uri="https://manager.example/auth/callback",
        logout_endpoint=_LOGOUT_URL,
    )


def _serve_logout(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> list[httpx.Request]:
    """Answer the client's HTTP calls with `handler` and return the requests it made."""
    seen: list[httpx.Request] = []

    def _record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    real = httpx.AsyncClient

    def _factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        return real(*args, transport=httpx.MockTransport(_record), **kwargs)

    monkeypatch.setattr("app.auth.oidc.httpx.AsyncClient", _factory)
    return seen


async def test_end_session_posts_the_refresh_token_with_the_client_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _serve_logout(monkeypatch, lambda _request: httpx.Response(204))

    assert await _logout_client().end_session(refresh_token="rt-1") is True

    assert [(r.method, str(r.url)) for r in seen] == [("POST", _LOGOUT_URL)]
    assert parse_qs(seen[0].content.decode()) == {
        "client_id": ["papaia-manager"],
        "client_secret": ["secret"],
        "refresh_token": ["rt-1"],
    }


async def test_end_session_counts_a_session_that_was_already_over_as_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve_logout(
        monkeypatch,
        lambda _request: httpx.Response(
            400, json={"error": "invalid_grant", "error_description": "Session not active"}
        ),
    )

    assert await _logout_client().end_session(refresh_token="dead-rt") is True


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400, json={"error": "invalid_client"}),
        httpx.Response(400, text="not json"),
        httpx.Response(401),
        httpx.Response(500),
    ],
)
async def test_end_session_raises_when_the_provider_refuses(
    monkeypatch: pytest.MonkeyPatch, response: httpx.Response
) -> None:
    _serve_logout(monkeypatch, lambda _request: response)

    with pytest.raises(OIDCError):
        await _logout_client().end_session(refresh_token="rt-1")


async def test_end_session_raises_when_the_provider_cannot_be_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    _serve_logout(monkeypatch, _unreachable)

    with pytest.raises(OIDCError) as excinfo:
        await _logout_client().end_session(refresh_token="rt-secret")

    assert "rt-secret" not in str(excinfo.value)


async def test_end_session_without_a_logout_endpoint_asks_nobody(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _serve_logout(monkeypatch, lambda _request: httpx.Response(204))

    assert await _client().end_session(refresh_token="rt-1") is False
    assert seen == []


# ---------------------------------------------------------------------------
# _safe_next (post-login redirect target)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["/", "/addons", "/jobs/abc?tab=log"])
def test_safe_next_accepts_local_paths(value: str) -> None:
    assert _safe_next(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "addons",
        "//evil.com",
        "https://evil.com",
        "/\\evil.com",
        "/x\nSet-Cookie: y",
    ],
)
def test_safe_next_rejects_everything_else(value: str) -> None:
    assert _safe_next(value) is None
