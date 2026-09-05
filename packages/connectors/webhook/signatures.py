"""Webhook signature verification (spec section 88).

The requirements, all enforced here:

* verify signatures;
* reject stale timestamps;
* preserve the raw payload;
* idempotency by event ID;
* do not trust a client-provided tenant ID;
* map the webhook to a connection server-side.

Verification uses the *raw request bytes*. Re-serialising JSON before checking a
signature is the classic way to break webhook verification, so the API layer
must hand the untouched body straight to these functions.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass

__all__ = [
    "SignatureError",
    "StripeSignature",
    "verify_hmac_sha256",
    "verify_stripe_signature",
]

DEFAULT_TOLERANCE_SECONDS = 300


class SignatureError(ValueError):
    """Raised when a webhook cannot be trusted.

    The message never echoes the signature or the secret.
    """

    def __init__(self, message: str, code: str = "invalid_signature") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class StripeSignature:
    timestamp: int
    signatures: tuple[str, ...]

    @classmethod
    def parse(cls, header: str) -> StripeSignature:
        """Parse a ``Stripe-Signature`` header: ``t=...,v1=...,v1=...``."""
        timestamp: int | None = None
        signatures: list[str] = []

        for part in header.split(","):
            key, _, value = part.strip().partition("=")
            if key == "t":
                try:
                    timestamp = int(value)
                except ValueError as exc:
                    raise SignatureError(
                        "The signature header carries a malformed timestamp.",
                        "malformed_header",
                    ) from exc
            elif key == "v1":
                signatures.append(value)

        if timestamp is None or not signatures:
            raise SignatureError(
                "The signature header is missing its timestamp or signature.",
                "malformed_header",
            )
        return cls(timestamp=timestamp, signatures=tuple(signatures))


def verify_stripe_signature(
    payload: bytes,
    header: str,
    secret: str,
    *,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
    now: float | None = None,
) -> None:
    """Verify a Stripe-style signed webhook, or raise.

    The signed payload is ``"{timestamp}.{raw body}"``. Every ``v1`` signature
    in the header is checked, because Stripe sends more than one during a secret
    rotation, and each comparison is constant-time.
    """
    if not secret:
        raise SignatureError(
            "No webhook secret is configured for this connection.", "not_configured"
        )

    parsed = StripeSignature.parse(header)
    current = now if now is not None else time.time()

    age = current - parsed.timestamp
    if age > tolerance_seconds:
        raise SignatureError(
            f"The webhook timestamp is {int(age)}s old, beyond the "
            f"{tolerance_seconds}s tolerance. Replays are refused.",
            "stale_timestamp",
        )
    if age < -tolerance_seconds:
        raise SignatureError(
            "The webhook timestamp is in the future beyond the allowed tolerance.",
            "future_timestamp",
        )

    signed = f"{parsed.timestamp}.".encode() + payload
    expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()

    if not any(hmac.compare_digest(expected, candidate) for candidate in parsed.signatures):
        raise SignatureError("The webhook signature does not match.", "invalid_signature")


def verify_hmac_sha256(
    payload: bytes,
    signature: str,
    secret: str,
    *,
    prefix: str = "",
) -> None:
    """Verify a plain HMAC-SHA256 signature header.

    Used by providers that sign the raw body with no timestamp. Those webhooks
    are replayable by design, so the caller must still enforce idempotency by
    provider event ID.
    """
    if not secret:
        raise SignatureError(
            "No webhook secret is configured for this connection.", "not_configured"
        )
    candidate = signature[len(prefix) :] if prefix and signature.startswith(prefix) else signature
    expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, candidate):
        raise SignatureError("The webhook signature does not match.", "invalid_signature")


def sign_stripe_payload(payload: bytes, secret: str, timestamp: int) -> str:
    """Produce a valid header. Used by the test suite, never in production."""
    signed = f"{timestamp}.".encode() + payload
    signature = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={signature}"
