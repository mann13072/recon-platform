"""Database-backed audit log (spec section 35).

Append-only by contract: this class exposes ``append`` and readers, and nothing
else. There is no update or delete method to call, and in production the
application's database role is granted only INSERT and SELECT on
``audit_events``.

Each tenant's events form a hash chain. The per-tenant ``sequence`` column is
allocated under a row lock so two concurrent writers cannot produce two events
claiming the same predecessor.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.api.app.infrastructure.mappers import audit_event_to_row, row_to_audit_event
from apps.api.app.infrastructure.models import AuditEventRow
from packages.audit.events import AuditEvent
from packages.audit.logger import AuditLog

__all__ = ["DatabaseAuditLog"]


@dataclass(slots=True)
class DatabaseAuditLog(AuditLog):
    session: Session

    def append(self, event: AuditEvent) -> AuditEvent:
        previous = self._last_row(event.tenant_id, for_update=True)
        previous_hash = previous.event_hash if previous else ""
        sequence = (previous.sequence + 1) if previous else 1

        sealed = event.sealed(previous_hash)
        self.session.add(audit_event_to_row(sealed, sequence))
        self.session.flush()
        return sealed

    def last_hash(self, tenant_id: UUID) -> str:
        row = self._last_row(tenant_id)
        return row.event_hash if row else ""

    def events_for(
        self, tenant_id: UUID, *, limit: int | None = None, offset: int = 0
    ) -> list[AuditEvent]:
        statement = (
            select(AuditEventRow)
            .where(AuditEventRow.tenant_id == tenant_id)
            .order_by(AuditEventRow.sequence)
            .offset(offset)
        )
        if limit is not None:
            statement = statement.limit(limit)
        return [row_to_audit_event(row) for row in self.session.execute(statement).scalars()]

    def events_for_entity(
        self, tenant_id: UUID, entity_type: str, entity_id: UUID
    ) -> list[AuditEvent]:
        rows = self.session.execute(
            select(AuditEventRow)
            .where(
                AuditEventRow.tenant_id == tenant_id,
                AuditEventRow.entity_type == entity_type,
                AuditEventRow.entity_id == entity_id,
            )
            .order_by(AuditEventRow.sequence)
        ).scalars()
        return [row_to_audit_event(row) for row in rows]

    def count(self, tenant_id: UUID) -> int:
        return int(
            self.session.execute(
                select(func.count())
                .select_from(AuditEventRow)
                .where(AuditEventRow.tenant_id == tenant_id)
            ).scalar_one()
        )

    def _last_row(self, tenant_id: UUID, *, for_update: bool = False) -> AuditEventRow | None:
        statement = (
            select(AuditEventRow)
            .where(AuditEventRow.tenant_id == tenant_id)
            .order_by(AuditEventRow.sequence.desc())
            .limit(1)
        )
        if for_update and self.session.bind is not None:
            # SQLite has no row locks; it serialises writes anyway. On
            # PostgreSQL this is what stops two writers claiming one sequence.
            if self.session.bind.dialect.name != "sqlite":
                statement = statement.with_for_update()
        return self.session.execute(statement).scalar_one_or_none()
