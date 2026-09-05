"""Tenant-scoped repositories (spec section 54).

The rule the spec states, enforced structurally here:

    Bad:    session.query(Transaction).filter(Transaction.id == tx_id)
    Better: session.query(Transaction).filter(
                Transaction.id == tx_id, Transaction.tenant_id == tenant_id
            )

A repository is constructed *with* a tenant and cannot be constructed without
one. Every statement it builds starts from :meth:`_scoped`, which applies the
tenant predicate before anything else. There is no method that takes a bare ID
and skips the tenant filter, so a cross-tenant read is not something a caller
can express by accident.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, TypeVar
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from apps.api.app.infrastructure.mappers import (
    exception_to_row,
    match_group_to_rows,
    row_to_exception,
    row_to_match_group,
    row_to_transaction,
    transaction_to_row,
)
from apps.api.app.infrastructure.models import (
    Base,
    ExceptionRow,
    MatchGroupRow,
    ReconciliationRow,
    RunRow,
    RunSnapshotRow,
    SourceFileRow,
    SourceRecordRow,
    TransactionRow,
)
from packages.domain.models.exceptions import ExceptionRecord
from packages.domain.models.matching import MatchGroup
from packages.domain.models.transaction import CanonicalTransaction

__all__ = [
    "ConcurrencyConflict",
    "ExceptionRepository",
    "MatchRepository",
    "ReconciliationRepository",
    "RunRepository",
    "SourceFileRepository",
    "TenantScopedRepository",
    "TransactionRepository",
]

RowT = TypeVar("RowT", bound=Base)


class ConcurrencyConflict(RuntimeError):
    """Raised when an optimistic-locking update matched no rows (spec section 64)."""

    def __init__(self, entity: str, entity_id: UUID, expected_version: int) -> None:
        super().__init__(
            f"{entity} {entity_id} changed since you loaded it (you expected "
            f"version {expected_version}). Reload it and review the change "
            "before retrying."
        )
        self.entity = entity
        self.entity_id = entity_id
        self.expected_version = expected_version


@dataclass(slots=True)
class TenantScopedRepository:
    """Base class. A repository always knows which tenant it speaks for."""

    session: Session
    tenant_id: UUID

    def __post_init__(self) -> None:
        if self.tenant_id is None:
            raise ValueError("a repository cannot be created without a tenant")

    def _scoped(self, model: type[RowT]) -> Select[tuple[RowT]]:
        """The only way a statement is started in this layer."""
        return select(model).where(model.tenant_id == self.tenant_id)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------


def _effective_date() -> Any:
    """The SQL equivalent of ``CanonicalTransaction.best_date``.

    Transaction date, else posting date, else value date. Kept as one function
    so the SQL and the Python definitions cannot drift apart.
    """
    return func.coalesce(
        TransactionRow.transaction_date,
        TransactionRow.posting_date,
        TransactionRow.value_date,
    )


@dataclass(slots=True)
class TransactionRepository(TenantScopedRepository):
    def get(self, transaction_id: UUID) -> CanonicalTransaction | None:
        row = self.session.execute(
            self._scoped(TransactionRow).where(TransactionRow.id == transaction_id)
        ).scalar_one_or_none()
        return row_to_transaction(row) if row else None

    def get_many(self, ids: Sequence[UUID]) -> list[CanonicalTransaction]:
        if not ids:
            return []
        rows = (
            self.session.execute(
                self._scoped(TransactionRow).where(TransactionRow.id.in_(list(ids)))
            )
            .scalars()
            .all()
        )
        return [row_to_transaction(row) for row in rows]

    def list(
        self,
        *,
        connection_id: UUID | None = None,
        source_system: str | None = None,
        currency: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        search: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[CanonicalTransaction]:
        statement = self._scoped(TransactionRow)
        if connection_id is not None:
            statement = statement.where(TransactionRow.source_connection_id == connection_id)
        if source_system:
            statement = statement.where(TransactionRow.source_system == source_system)
        if currency:
            statement = statement.where(TransactionRow.currency == currency.upper())
        # A period filter must use whichever date the record actually carries.
        # Filtering on transaction_date alone silently drops ledger exports that
        # only supply a posting date, which would leave a whole side of the
        # reconciliation empty without saying why.
        if date_from is not None:
            statement = statement.where(_effective_date() >= date_from)
        if date_to is not None:
            statement = statement.where(_effective_date() <= date_to)
        if search:
            pattern = f"%{search.upper()}%"
            statement = statement.where(
                func.upper(TransactionRow.description).like(pattern)
                | func.upper(TransactionRow.reference).like(pattern)
                | func.upper(TransactionRow.counterparty_name).like(pattern)
            )
        # A stable secondary sort so paging cannot repeat or skip a row.
        statement = (
            statement.order_by(_effective_date().desc(), TransactionRow.id)
            .limit(limit)
            .offset(offset)
        )
        return [row_to_transaction(row) for row in self.session.execute(statement).scalars()]

    def count(self, *, connection_id: UUID | None = None) -> int:
        statement = (
            select(func.count())
            .select_from(TransactionRow)
            .where(TransactionRow.tenant_id == self.tenant_id)
        )
        if connection_id is not None:
            statement = statement.where(TransactionRow.source_connection_id == connection_id)
        return int(self.session.execute(statement).scalar_one())

    def existing_identity_keys(
        self, keys: Sequence[tuple[UUID, str, str]]
    ) -> set[tuple[UUID, str, str]]:
        """Which (connection, source_record_id, checksum) triples already exist.

        Used to make ingestion idempotent without relying on catching a unique
        violation per row.
        """
        if not keys:
            return set()
        record_ids = {key[1] for key in keys}
        rows = (
            self.session.execute(
                self._scoped(TransactionRow).where(
                    TransactionRow.source_record_id.in_(list(record_ids))
                )
            )
            .scalars()
            .all()
        )
        return {
            (row.source_connection_id, row.source_record_id, row.source_checksum) for row in rows
        }

    def bulk_insert(self, transactions: Sequence[CanonicalTransaction]) -> int:
        for transaction in transactions:
            if transaction.tenant_id != self.tenant_id:
                raise ValueError("refusing to write a transaction belonging to another tenant")
            self.session.add(transaction_to_row(transaction))
        self.session.flush()
        return len(transactions)

    def balance(self, transaction_ids: Sequence[UUID]) -> Decimal:
        if not transaction_ids:
            return Decimal("0")
        rows = (
            self.session.execute(
                self._scoped(TransactionRow).where(TransactionRow.id.in_(list(transaction_ids)))
            )
            .scalars()
            .all()
        )
        return sum((row.amount for row in rows), Decimal("0"))


# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SourceFileRepository(TenantScopedRepository):
    def get(self, file_id: UUID) -> SourceFileRow | None:
        return self.session.execute(
            self._scoped(SourceFileRow).where(SourceFileRow.id == file_id)
        ).scalar_one_or_none()

    def by_checksum(self, checksum: str) -> SourceFileRow | None:
        return self.session.execute(
            self._scoped(SourceFileRow).where(SourceFileRow.checksum == checksum)
        ).scalar_one_or_none()

    def list(self, limit: int = 50, offset: int = 0) -> list[SourceFileRow]:
        return list(
            self.session.execute(
                self._scoped(SourceFileRow)
                .order_by(SourceFileRow.created_at.desc(), SourceFileRow.id)
                .limit(limit)
                .offset(offset)
            ).scalars()
        )

    def add(self, row: SourceFileRow) -> SourceFileRow:
        row.tenant_id = self.tenant_id
        self.session.add(row)
        self.session.flush()
        return row

    def add_source_records(self, rows: Sequence[SourceRecordRow]) -> int:
        for row in rows:
            row.tenant_id = self.tenant_id
            self.session.add(row)
        self.session.flush()
        return len(rows)


# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ReconciliationRepository(TenantScopedRepository):
    def get(self, reconciliation_id: UUID) -> ReconciliationRow | None:
        return self.session.execute(
            self._scoped(ReconciliationRow).where(ReconciliationRow.id == reconciliation_id)
        ).scalar_one_or_none()

    def by_slug(self, slug: str) -> ReconciliationRow | None:
        return self.session.execute(
            self._scoped(ReconciliationRow).where(ReconciliationRow.slug == slug)
        ).scalar_one_or_none()

    def list(self, limit: int = 50, offset: int = 0) -> list[ReconciliationRow]:
        return list(
            self.session.execute(
                self._scoped(ReconciliationRow)
                .order_by(ReconciliationRow.name, ReconciliationRow.id)
                .limit(limit)
                .offset(offset)
            ).scalars()
        )

    def add(self, row: ReconciliationRow) -> ReconciliationRow:
        row.tenant_id = self.tenant_id
        self.session.add(row)
        self.session.flush()
        return row


# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RunRepository(TenantScopedRepository):
    def get(self, run_id: UUID) -> RunRow | None:
        return self.session.execute(
            self._scoped(RunRow).where(RunRow.id == run_id)
        ).scalar_one_or_none()

    def by_idempotency_key(self, key: str) -> RunRow | None:
        return self.session.execute(
            self._scoped(RunRow).where(RunRow.idempotency_key == key)
        ).scalar_one_or_none()

    def list(
        self, *, reconciliation_id: UUID | None = None, limit: int = 50, offset: int = 0
    ) -> list[RunRow]:
        statement = self._scoped(RunRow)
        if reconciliation_id is not None:
            statement = statement.where(RunRow.reconciliation_id == reconciliation_id)
        return list(
            self.session.execute(
                statement.order_by(RunRow.created_at.desc(), RunRow.id).limit(limit).offset(offset)
            ).scalars()
        )

    def add(self, row: RunRow) -> RunRow:
        row.tenant_id = self.tenant_id
        self.session.add(row)
        self.session.flush()
        return row

    def snapshot(self, run_id: UUID) -> RunSnapshotRow | None:
        return self.session.execute(
            self._scoped(RunSnapshotRow).where(RunSnapshotRow.run_id == run_id)
        ).scalar_one_or_none()

    def add_snapshot(self, row: RunSnapshotRow) -> RunSnapshotRow:
        row.tenant_id = self.tenant_id
        self.session.add(row)
        self.session.flush()
        return row

    def update_status(self, run_id: UUID, *, expected_version: int, **updates: Any) -> RunRow:
        """Optimistic-locking update (spec section 64).

        The version predicate is part of the WHERE clause, so a concurrent
        writer loses the race loudly instead of silently overwriting.
        """
        row = self.session.execute(
            self._scoped(RunRow).where(RunRow.id == run_id, RunRow.version == expected_version)
        ).scalar_one_or_none()
        if row is None:
            raise ConcurrencyConflict("run", run_id, expected_version)
        for key, value in updates.items():
            setattr(row, key, value)
        row.version = expected_version + 1
        self.session.flush()
        return row


# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MatchRepository(TenantScopedRepository):
    def get(self, match_id: UUID) -> MatchGroup | None:
        row = self.session.execute(
            self._scoped(MatchGroupRow).where(MatchGroupRow.id == match_id)
        ).scalar_one_or_none()
        return row_to_match_group(row) if row else None

    def get_row(self, match_id: UUID) -> MatchGroupRow | None:
        return self.session.execute(
            self._scoped(MatchGroupRow).where(MatchGroupRow.id == match_id)
        ).scalar_one_or_none()

    def list_for_run(
        self,
        run_id: UUID,
        *,
        status: str | None = None,
        decision: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[MatchGroup]:
        statement = self._scoped(MatchGroupRow).where(MatchGroupRow.run_id == run_id)
        if status:
            statement = statement.where(MatchGroupRow.status == status)
        if decision:
            statement = statement.where(MatchGroupRow.decision == decision)
        rows = self.session.execute(
            statement.order_by(MatchGroupRow.score.desc(), MatchGroupRow.id)
            .limit(limit)
            .offset(offset)
        ).scalars()
        return [row_to_match_group(row) for row in rows]

    def bulk_insert(self, groups: Sequence[MatchGroup]) -> int:
        for group in groups:
            if group.tenant_id != self.tenant_id:
                raise ValueError("refusing to write a match group for another tenant")
            self.session.add(match_group_to_rows(group))
        self.session.flush()
        return len(groups)

    def active_group_for_transaction(self, transaction_id: UUID) -> MatchGroupRow | None:
        """Whether a transaction is already claimed by an active group.

        This is the database-side half of the match-exclusivity invariant: the
        engine enforces it within a run, and this enforces it across runs and
        across manual matches.
        """
        from apps.api.app.infrastructure.models import MatchGroupMemberRow

        active = ("PROPOSED", "SUGGESTED", "AUTO_APPROVED", "APPROVED")
        return (
            self.session.execute(
                self._scoped(MatchGroupRow)
                .join(MatchGroupRow.members)
                .where(
                    MatchGroupMemberRow.transaction_id == transaction_id,
                    MatchGroupRow.status.in_(active),
                )
            )
            .scalars()
            .first()
        )

    def update(self, match_id: UUID, *, expected_version: int, **updates: Any) -> MatchGroupRow:
        row = self.session.execute(
            self._scoped(MatchGroupRow).where(
                MatchGroupRow.id == match_id,
                MatchGroupRow.version == expected_version,
            )
        ).scalar_one_or_none()
        if row is None:
            raise ConcurrencyConflict("match group", match_id, expected_version)
        for key, value in updates.items():
            setattr(row, key, value)
        row.version = expected_version + 1
        self.session.flush()
        return row

    def counts_by_status(self, run_id: UUID) -> dict[str, int]:
        rows = self.session.execute(
            select(MatchGroupRow.status, func.count())
            .where(
                MatchGroupRow.tenant_id == self.tenant_id,
                MatchGroupRow.run_id == run_id,
            )
            .group_by(MatchGroupRow.status)
        ).all()
        return {str(status): int(count) for status, count in rows}


# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ExceptionRepository(TenantScopedRepository):
    def get(self, exception_id: UUID) -> ExceptionRecord | None:
        row = self.session.execute(
            self._scoped(ExceptionRow).where(ExceptionRow.id == exception_id)
        ).scalar_one_or_none()
        return row_to_exception(row) if row else None

    def get_row(self, exception_id: UUID) -> ExceptionRow | None:
        return self.session.execute(
            self._scoped(ExceptionRow).where(ExceptionRow.id == exception_id)
        ).scalar_one_or_none()

    def list(
        self,
        *,
        run_id: UUID | None = None,
        status: str | None = None,
        category: str | None = None,
        severity: str | None = None,
        owner_user_id: UUID | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ExceptionRecord]:
        statement = self._scoped(ExceptionRow)
        if run_id is not None:
            statement = statement.where(ExceptionRow.reconciliation_run_id == run_id)
        if status:
            statement = statement.where(ExceptionRow.status == status)
        if category:
            statement = statement.where(ExceptionRow.category == category)
        if severity:
            statement = statement.where(ExceptionRow.severity == severity)
        if owner_user_id is not None:
            statement = statement.where(ExceptionRow.owner_user_id == owner_user_id)
        rows = self.session.execute(
            statement.order_by(ExceptionRow.amount_exposure.desc().nulls_last(), ExceptionRow.id)
            .limit(limit)
            .offset(offset)
        ).scalars()
        return [row_to_exception(row) for row in rows]

    def bulk_insert(self, records: Sequence[ExceptionRecord]) -> int:
        for record in records:
            if record.tenant_id != self.tenant_id:
                raise ValueError("refusing to write an exception for another tenant")
            self.session.add(exception_to_row(record))
        self.session.flush()
        return len(records)

    def counts_by_status(self, run_id: UUID) -> dict[str, int]:
        rows = self.session.execute(
            select(ExceptionRow.status, func.count())
            .where(
                ExceptionRow.tenant_id == self.tenant_id,
                ExceptionRow.reconciliation_run_id == run_id,
            )
            .group_by(ExceptionRow.status)
        ).all()
        return {str(status): int(count) for status, count in rows}
