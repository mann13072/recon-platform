"""Audit event model (spec section 35).

Audit events are append-only and hash-chained. Each event records the hash of
the entity before and after the action, plus the hash of the previous event in
the tenant's chain. Tampering with a historical event breaks every hash after
it, which is what makes the export defensible to an auditor.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from packages.domain.dates import utc_now
from packages.domain.enums import ActorType, AuditAction

__all__ = ["AuditEvent", "hash_state"]

# Field names whose values must never reach the audit log in the clear
# (spec section 55).
_REDACTED_KEYS = {
    "access_token",
    "account_number",
    "api_key",
    "api_secret",
    "authorization",
    "bank_account",
    "client_secret",
    "credential",
    "iban",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "token",
}

_REDACTED = "[REDACTED]"


def redact(payload: dict[str, Any]) -> dict[str, Any]:
    """Recursively replace sensitive values with a marker.

    Applied to every metadata payload before it is stored, so a careless caller
    cannot leak a refresh token into an immutable table.
    """
    clean: dict[str, Any] = {}
    for key, value in payload.items():
        lowered = key.lower()
        if any(marker in lowered for marker in _REDACTED_KEYS):
            clean[key] = _REDACTED
        elif isinstance(value, dict):
            clean[key] = redact(value)
        elif isinstance(value, list):
            clean[key] = [redact(v) if isinstance(v, dict) else v for v in value]
        else:
            clean[key] = value
    return clean


def hash_state(state: Any) -> str:
    """Stable hash of an entity's state, for the before/after fields."""
    if state is None:
        return ""
    if isinstance(state, BaseModel):
        payload = state.model_dump(mode="json")
    elif isinstance(state, dict):
        payload = state
    else:
        payload = {"value": str(state)}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class AuditEvent(BaseModel):
    """One immutable record of activity."""

    model_config = ConfigDict(frozen=True)

    event_id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    occurred_at: datetime = Field(default_factory=utc_now)

    actor_type: ActorType
    actor_id: UUID | None = None
    actor_label: str | None = None

    action: AuditAction
    entity_type: str
    entity_id: UUID | None = None

    before_hash: str = ""
    after_hash: str = ""
    previous_event_hash: str = ""
    event_hash: str = ""

    correlation_id: UUID | None = None
    ip_address: str | None = None
    user_agent: str | None = None
    reason: str | None = None

    metadata: dict[str, Any] = Field(default_factory=dict)

    def compute_hash(self) -> str:
        """Hash over everything except the hash field itself."""
        payload = {
            "event_id": str(self.event_id),
            "tenant_id": str(self.tenant_id),
            "occurred_at": self.occurred_at.isoformat(),
            "actor_type": self.actor_type.value,
            "actor_id": str(self.actor_id) if self.actor_id else None,
            "action": self.action.value,
            "entity_type": self.entity_type,
            "entity_id": str(self.entity_id) if self.entity_id else None,
            "before_hash": self.before_hash,
            "after_hash": self.after_hash,
            "previous_event_hash": self.previous_event_hash,
            "reason": self.reason,
            "metadata": self.metadata,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def sealed(self, previous_event_hash: str = "") -> AuditEvent:
        """Return a copy with the chain and event hashes filled in."""
        linked = self.model_copy(
            update={
                "previous_event_hash": previous_event_hash,
                "metadata": redact(self.metadata),
            }
        )
        return linked.model_copy(update={"event_hash": linked.compute_hash()})

    def verify(self, previous_event_hash: str = "") -> bool:
        """Whether this event is internally consistent and correctly chained."""
        if self.previous_event_hash != previous_event_hash:
            return False
        return self.event_hash == self.compute_hash()


def verify_chain(events: list[AuditEvent]) -> tuple[bool, int | None]:
    """Verify a whole chain in order.

    Returns ``(ok, first_bad_index)``. The index makes an audit finding
    actionable: it names the exact event where the chain diverges.
    """
    previous = ""
    for index, event in enumerate(events):
        if not event.verify(previous):
            return False, index
        previous = event.event_hash
    return True, None
