"""Encryption of the api-keys in `connections.yaml`, in the ingester's own format.

The ingester stores a connection's api-key as `enc:1:` followed by a Fernet token. The
Fernet key is derived from `QI_CONNECTIONS_SECRET` (SHA-256, then URL-safe base64), so
any random string works as the secret. Both services have to derive the same key, byte
for byte: a secret with a stray space or a pair of quotes is a different key, and every
stored value then reads as unreadable on the other side.

The manager reads that secret from the RAG module's `.env`. Its parser keeps quotes
(see `envfile.py`) and Compose strips them, so a quoted value would be derived
differently by the two services. The core writes the secret unquoted.

Nothing here puts a token or a plaintext key into an exception message.
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

# The scheme version is part of the stored value, so a later change of the cipher stays
# distinguishable from what is already on disk.
ENC_PREFIX = "enc:1:"


class SecretError(Exception):
    """The secret is missing, or a stored key cannot be read with it."""


def _fernet(secret: str) -> Fernet:
    if not secret:
        raise SecretError("QI_CONNECTIONS_SECRET is not set")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


def is_encrypted(value: str) -> bool:
    return value.startswith(ENC_PREFIX)


def encrypt(plaintext: str, secret: str) -> str:
    """The `enc:1:` value for a plaintext api-key."""
    token = _fernet(secret).encrypt(plaintext.encode("utf-8")).decode("ascii")
    return ENC_PREFIX + token


def decrypt(stored: str, secret: str) -> str:
    """The plaintext of an `enc:1:` value.

    Raises `SecretError` when the secret is unset, the value is not a token, or the
    token does not decrypt with this secret.
    """
    if not is_encrypted(stored):
        raise SecretError("the stored api-key is not an enc:1: token")
    token = stored[len(ENC_PREFIX) :]
    try:
        return _fernet(secret).decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise SecretError(
            "the stored api-key could not be decrypted; QI_CONNECTIONS_SECRET may have changed"
        ) from exc
