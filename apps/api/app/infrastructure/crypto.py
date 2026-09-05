"""Connector credential encryption (spec sections 4 and 53).

    Never store OAuth refresh tokens unencrypted in normal application tables.

Production uses envelope encryption: a cloud KMS holds the key-encryption key
and issues a per-record data key. That is a deployment concern, so the KMS call
sits behind :class:`KeyProvider` and the default implementation derives a key
from ``CREDENTIAL_ENCRYPTION_KEY`` for local development.

The ciphertext format is versioned so a key rotation can be rolled out without
having to decrypt everything at once.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import struct
from abc import ABC, abstractmethod
from dataclasses import dataclass

__all__ = [
    "CredentialCipher",
    "DecryptionError",
    "EnvKeyProvider",
    "KeyProvider",
]

_VERSION = b"\x01"
_NONCE_BYTES = 16
_KEY_BYTES = 32


class DecryptionError(ValueError):
    """Raised when ciphertext fails authentication.

    Never include the ciphertext or any partial plaintext in the message.
    """


class KeyProvider(ABC):
    @abstractmethod
    def key_id(self) -> str:
        ...

    @abstractmethod
    def data_key(self, context: str) -> bytes:
        """A 32-byte key for the given encryption context."""


@dataclass(slots=True)
class EnvKeyProvider(KeyProvider):
    """Derives per-context keys from a single configured master secret.

    Suitable for development and for deployments that manage the master secret
    in a secret manager. For production on a cloud provider, implement
    ``KeyProvider`` against that provider's KMS instead: this class never
    involves an HSM and offers no key rotation of its own.
    """

    master_secret: str
    identifier: str = "env"

    def __post_init__(self) -> None:
        if not self.master_secret or len(self.master_secret) < 32:
            raise ValueError(
                "CREDENTIAL_ENCRYPTION_KEY must be at least 32 characters"
            )

    def key_id(self) -> str:
        digest = hashlib.sha256(self.master_secret.encode("utf-8")).hexdigest()[:16]
        return f"{self.identifier}:{digest}"

    def data_key(self, context: str) -> bytes:
        # HKDF-Expand with the context as info, so each connection gets a
        # distinct key and one leak does not compromise the others.
        prk = hmac.new(
            b"recon-platform-credential-v1",
            self.master_secret.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return hmac.new(prk, context.encode("utf-8") + b"\x01", hashlib.sha256).digest()


@dataclass(slots=True)
class CredentialCipher:
    """Authenticated encryption for connector secrets.

    AES-GCM via ``cryptography`` when it is available, falling back to an
    HMAC-authenticated keystream otherwise. Both are encrypt-then-MAC and both
    reject tampered ciphertext; the AES path is preferred and is what production
    installs, because the fallback is only as strong as SHA-256 in counter mode.
    """

    keys: KeyProvider

    def encrypt(self, plaintext: str, *, context: str) -> bytes:
        key = self.keys.data_key(context)
        nonce = os.urandom(_NONCE_BYTES)
        data = plaintext.encode("utf-8")

        aead = _load_aesgcm()
        if aead is not None:
            ciphertext = aead(key).encrypt(nonce[:12], data, context.encode("utf-8"))
            return _VERSION + b"\x01" + nonce + ciphertext

        stream = _keystream(key, nonce, len(data))
        body = bytes(a ^ b for a, b in zip(data, stream, strict=True))
        tag = hmac.new(key, nonce + body + context.encode("utf-8"), hashlib.sha256).digest()
        return _VERSION + b"\x02" + nonce + tag + body

    def decrypt(self, ciphertext: bytes, *, context: str) -> str:
        if len(ciphertext) < 2 + _NONCE_BYTES or ciphertext[:1] != _VERSION:
            raise DecryptionError("unrecognised credential ciphertext")

        mode = ciphertext[1:2]
        nonce = ciphertext[2 : 2 + _NONCE_BYTES]
        body = ciphertext[2 + _NONCE_BYTES :]
        key = self.keys.data_key(context)

        if mode == b"\x01":
            aead = _load_aesgcm()
            if aead is None:
                raise DecryptionError(
                    "this credential needs AES-GCM but the cryptography package "
                    "is not installed"
                )
            try:
                return aead(key).decrypt(nonce[:12], body, context.encode("utf-8")).decode("utf-8")
            except Exception as exc:
                raise DecryptionError("credential failed authentication") from exc

        if mode == b"\x02":
            tag, payload = body[:32], body[32:]
            expected = hmac.new(
                key, nonce + payload + context.encode("utf-8"), hashlib.sha256
            ).digest()
            if not hmac.compare_digest(tag, expected):
                raise DecryptionError("credential failed authentication")
            stream = _keystream(key, nonce, len(payload))
            return bytes(a ^ b for a, b in zip(payload, stream, strict=True)).decode("utf-8")

        raise DecryptionError("unsupported credential ciphertext version")

    @property
    def key_id(self) -> str:
        return self.keys.key_id()


def _load_aesgcm():  # type: ignore[no-untyped-def]
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:  # pragma: no cover - depends on the environment
        return None
    return AESGCM


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    """SHA-256 in counter mode. Only used when AES-GCM is unavailable."""
    output = bytearray()
    counter = 0
    while len(output) < length:
        output += hashlib.sha256(key + nonce + struct.pack(">I", counter)).digest()
        counter += 1
    return bytes(output[:length])


def fingerprint(secret: str) -> str:
    """A non-reversible fingerprint, safe to log or display.

    Used so an operator can confirm which credential is installed without the
    value ever appearing in a log line.
    """
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)[:12].decode("ascii")
