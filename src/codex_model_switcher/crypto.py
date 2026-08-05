"""Encryption primitives for short-lived protocol state.

The key is deliberately supplied by the caller.  This module has no credential
store integration and never persists or exposes the key.
"""

from __future__ import annotations

from typing import Protocol

from cryptography.fernet import Fernet


class CryptoError(Exception):
    """Base error for the injected protocol-state encryption boundary."""


class InvalidSecretKeyError(CryptoError, ValueError):
    """Raised when a key provider does not return a valid Fernet key."""


class SecretKeyProvider(Protocol):
    """Provider boundary implemented by the application integration layer."""

    def get_key(self) -> bytes:
        """Return the Fernet key without persisting it in this package."""


def _load_key(provider: SecretKeyProvider) -> bytes:
    get_key = getattr(provider, "get_key", None)
    if not callable(get_key):
        raise TypeError("secret key provider must implement get_key()")

    key = get_key()
    if not isinstance(key, bytes):
        raise InvalidSecretKeyError("secret key provider must return bytes")
    try:
        Fernet(key)
    except (TypeError, ValueError) as exc:
        raise InvalidSecretKeyError("secret key provider returned an invalid Fernet key") from exc
    return key


class FernetCipher:
    """Encrypt and decrypt UTF-8 protocol fragments with an injected key."""

    def __init__(self, provider: SecretKeyProvider) -> None:
        self._fernet = Fernet(_load_key(provider))

    def encrypt_text(self, value: str) -> bytes:
        if not isinstance(value, str):
            raise TypeError("encrypted protocol fragments must be text")
        return self._fernet.encrypt(value.encode("utf-8"))

    def decrypt_text(self, token: bytes) -> str:
        if not isinstance(token, bytes):
            raise TypeError("encrypted protocol fragments must be bytes")
        return self._fernet.decrypt(token).decode("utf-8")
