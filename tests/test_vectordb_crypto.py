"""The encryption of connection api-keys, in the ingester's format.

Both services have to derive the same key from `QI_CONNECTIONS_SECRET` and read each
other's tokens, and nothing on the ingester's side pins that for the manager. So the
reference values here were produced by the ingester's own `connections/crypto.py`; a
change of the scheme there must fail these tests before it silently makes every stored
key unreadable.
"""
from __future__ import annotations

import base64
import hashlib

import pytest

from app.core.vectordb import crypto

_SECRET = "contract-test-secret"
_PLAIN = "qdrant-api-key-value"
# Produced by the ingester's `encrypt(_PLAIN, _SECRET)`. A Fernet token is randomised, so
# this one value stands for "a token the ingester wrote".
_FROM_INGEST = (
    "enc:1:gAAAAABqwpvpj_FILR3HfdPw9FBXgkwHsb9zovVoZ9TbYcHaOHWtrqu-cuBqa18OtzBzKqvKR7YYsFO4Ea"
    "xWLaB4W6JqUBXxy9wnblPITXsqcBowCh-aNSg="
)


def test_the_key_is_derived_the_way_the_ingester_derives_it() -> None:
    # SHA-256 of the secret, URL-safe base64. Pinned as a literal as well as computed.
    expected = base64.urlsafe_b64encode(hashlib.sha256(_SECRET.encode()).digest())

    assert expected == b"PrLZ26VsMLyl82Xk9ElDovsnCfaMeFY4FDx-ATQoVNk="
    assert crypto.decrypt(_FROM_INGEST, _SECRET) == _PLAIN


def test_a_value_round_trips_and_carries_the_scheme_prefix() -> None:
    stored = crypto.encrypt("s3cret", _SECRET)

    assert stored.startswith("enc:1:")
    assert "s3cret" not in stored
    assert crypto.is_encrypted(stored)
    assert crypto.decrypt(stored, _SECRET) == "s3cret"


def test_encrypting_twice_gives_two_tokens() -> None:
    # Why an unchanged key keeps its stored token instead of being encrypted again.
    assert crypto.encrypt("s3cret", _SECRET) != crypto.encrypt("s3cret", _SECRET)


def test_non_ascii_keys_survive() -> None:
    assert crypto.decrypt(crypto.encrypt("schlüssel-äöü-✓", _SECRET), _SECRET) == "schlüssel-äöü-✓"


@pytest.mark.parametrize("secret", ["", "another-secret", _SECRET + " "])
def test_the_wrong_secret_is_an_error(secret: str) -> None:
    with pytest.raises(crypto.SecretError):
        crypto.decrypt(_FROM_INGEST, secret)


def test_a_missing_secret_cannot_encrypt() -> None:
    with pytest.raises(crypto.SecretError, match="QI_CONNECTIONS_SECRET"):
        crypto.encrypt("s3cret", "")


@pytest.mark.parametrize("stored", ["plain-text", "enc:1:not-a-token", "enc:1:", "", "enc:2:abc"])
def test_a_value_that_is_not_a_token_is_an_error(stored: str) -> None:
    with pytest.raises(crypto.SecretError):
        crypto.decrypt(stored, _SECRET)


def test_a_tampered_token_is_an_error() -> None:
    flipped = _FROM_INGEST[:-8] + ("A" if _FROM_INGEST[-8] != "A" else "B") + _FROM_INGEST[-7:]

    with pytest.raises(crypto.SecretError):
        crypto.decrypt(flipped, _SECRET)


def test_an_error_never_carries_the_token_or_the_key() -> None:
    with pytest.raises(crypto.SecretError) as caught:
        crypto.decrypt(_FROM_INGEST, "another-secret")

    text = str(caught.value)
    assert _FROM_INGEST not in text and _FROM_INGEST[6:] not in text
    assert _PLAIN not in text and "another-secret" not in text
