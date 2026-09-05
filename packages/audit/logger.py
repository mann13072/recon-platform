"""The audit logger (spec section 35).

Two implementations share one interface:

* :class:`InMemoryAuditLog` - used by tests and by the pure engine;
* the database-backed sink in ``apps.api.app.infrastructure`` - used in production.

Both maintain the per-tenant hash chain. The sink is append-only by contract:
there is deliberately no update or delete method to call.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from packages.audit.events import AuditEvent, hash_state, verify_chain
from packages.domain.enums import ActorType, AuditAction

__all__ = ["AuditLog", "AuditContext", "InMemoryAuditLog", "record"]


@dataclass(frozen=True, slots=True)
class AuditContext:
    """Who is acting, and under which correlation."""

    tenant_id: UUID
    actor_type: ActorType
    actor_id: UUID | None = None
    actor_label: str | None = None
    correlation_id: UUID | None = None
    ip_address: str | None = None
    user_agent: str | None = None

    @classmethod
    def system(cls, tenant_id: UUID, label: str = "system") -> AuditContext:
        return cls(tenant_id=tenant_id, actor_type=ActorType.SYSTEM, actor_label=label)

    @classmethod
    def engine(cls, tenant_id: UUID, correlation_id: UUID | None = None) -> AuditContext:
        return cls(
            tenant_id=tenant_id,
            actor_type=ActorType.MATCH_ENGINE,
            actor_label="matching-engine",
            correlation_id=correlation_id,
        )

    @classmethod
    def ai(cls, tenant_id: UUID, model: str) -> AuditContext:
        return cls(
            tenant_id=tenant_id,
            actor_type=ActorType.AI_ASSISTANT,
            actor_label=model,
        )


class AuditLog(ABC):
    """Append-only audit sink."""

    @abstractmethod
    def append(self, event: AuditEvent) -> AuditEvent:
        """Seal and persist one event, returning the sealed copy."""

    @abstractmethod
    def last_hash(self, tenant_id: UUID) -> str:
        """The hash of the most recent event for a tenant, or ''."""

    @abstractmethod
    def events_for(self, tenant_id: UUID) -> list[AuditEvent]:
        """Every event for a tenant, in insertion order."""

    def record(
        self,
        context: AuditContext,
        action: AuditAction,
        entity_type: str,
        entity_id: UUID | None = None,
        *,
        before: Any = None,
        after: Any = None,
        reason: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AuditEvent:
        """Build, seal and append an event in one call."""
        event = AuditEvent(
            tenant_id=context.tenant_id,
            actor_type=context.actor_type,
            actor_id=context.actor_id,
            actor_label=context.actor_label,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            before_hash=hash_state(before),
            after_hash=hash_state(after),
            correlation_id=context.correlation_id,
            ip_address=context.ip_address,
            user_agent=context.user_agent,
            reason=reason,
            metadata=metadata or {},
        )
        return self.append(event)

    def verify(self, tenant_id: UUID) -> tuple[bool, int | None]:
        return verify_chain(self.events_for(tenant_id))


@dataclass(slots=True)
class InMemoryAuditLog(AuditLog):
    """Non-persistent audit log for tests and for the pure engine."""

    _events: dict[UUID, list[AuditEvent]] = field(default_factory=lambda: defaultdict(list))

    def append(self, event: AuditEvent) -> AuditEvent:
        sealed = event.sealed(self.last_hash(event.tenant_id))
        self._events[event.tenant_id].append(sealed)
        return sealed

    def last_hash(self, tenant_id: UUID) -> str:
        events = self._events.get(tenant_id)
        return events[-1].event_hash if events else ""

    def events_for(self, tenant_id: UUID) -> list[AuditEvent]:
        return list(self._events.get(tenant_id, []))

    def clear(self) -> None:
        self._events.clear()


def record(
    log: AuditLog,
    context: AuditContext,
    action: AuditAction,
    entity_type: str,
    entity_id: UUID | None = None,
    **kwargs: Any,
) -> AuditEvent:
    """Convenience wrapper so call sites read as one line."""
    return log.record(context, action, entity_type, entity_id, **kwargs)
