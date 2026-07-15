"""Unit tests for OIDC auth helpers."""
from __future__ import annotations

import base64
import hashlib
import time

import pytest

from app.auth.oidc import (
    OIDCClaims,
    OIDCError,
    _algorithm_for_key,
    _extract_claims,
    _find_jwk,
    _pkce_pair,
)


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
