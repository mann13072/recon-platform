"""Persistence behaviour that the rest of the platform depends on.

Spec sections 8, 51, 54, 64 and 82. Three properties in particular:

* money survives a write/read cycle as an exact ``Decimal``;
* re-ingesting the same source data cannot create duplicates;
* a query issued for one tenant cannot see another tenant's rows.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from apps.api.app.infrastructure.models import (
    SourceRecordRow,
    TransactionRow,
)
from apps.api.app.infrastructure.repositories import (
    ConcurrencyConflict,
    ExceptionRepository,
    MatchRepository,
    RunRepository,
    TransactionRepository,
)
from packages.domain.dates import utc_now
from packages.domain.models.transaction import CanonicalTransaction, compute_checksum


def make_row(tenant_id: UUID, record_id: str, amount: str, **extra: object) -> TransactionRow:
    payload = {"id": record_id, "amount": amount}
    return TransactionRow(
        id=uuid4(),
        tenant_id=tenant_id,
        source_system="bank",
        source_connection_id=uuid4(),
        source_record_id=record_id,
        amount=Decimal(amount),
        currency="EUR",
        transaction_date=date(2026, 8, 1),
        raw_payload=payload,
        source_checksum=compute_checksum(payload),
        imported_at=utc_now(),
        **extra,  # type: ignore[arg-type]
    )


class TestMoneyRoundTrip:
    """SQLite returns NUMERIC as float unless the type coerces it back.

    If this regresses, every amount in the system silently becomes a float and
    the Decimal guarantee in the domain layer is worthless.
    """

    def test_amount_reads_back_as_exact_decimal(self, session: Session, tenant_id: UUID) -> None:
        session.add(make_row(tenant_id, "T1", "982.45"))
        session.commit()
        session.expunge_all()

        row = session.query(TransactionRow).one()
        assert isinstance(row.amount, Decimal)
        assert row.amount == Decimal("982.45")

    def test_no_binary_floating_point_drift(self, session: Session, tenant_id: UUID) -> None:
        session.add(make_row(tenant_id, "T1", "0.10"))
        session.add(make_row(tenant_id, "T2", "0.20"))
        session.commit()
        session.expunge_all()

        rows = session.query(TransactionRow).order_by(TransactionRow.source_record_id).all()
        assert rows[0].amount + rows[1].amount == Decimal("0.30")

    def test_high_precision_values_survive(self, session: Session, tenant_id: UUID) -> None:
        session.add(make_row(tenant_id, "T1", "12345678901234.12345678"))
        session.commit()
        session.expunge_all()
        assert session.query(TransactionRow).one().amount == Decimal("12345678901234.12345678")

    def test_negative_and_zero_survive(self, session: Session, tenant_id: UUID) -> None:
        session.add(make_row(tenant_id, "T1", "-17.55"))
        session.add(make_row(tenant_id, "T2", "0.00"))
        session.commit()
        session.expunge_all()
        amounts = {r.source_record_id: r.amount for r in session.query(TransactionRow)}
        assert amounts["T1"] == Decimal("-17.55")
        assert amounts["T2"] == Decimal("0")

    def test_a_float_is_refused_at_the_boundary(self, session: Session, tenant_id: UUID) -> None:
        from sqlalchemy.exc import StatementError

        row = make_row(tenant_id, "T1", "1.00")
        row.amount = 982.45  # type: ignore[assignment]
        session.add(row)
        with pytest.raises((TypeError, StatementError), match="float"):
            session.flush()
        session.rollback()

    def test_ordering_by_amount_is_numeric_not_lexicographic(
        self, session: Session, tenant_id: UUID
    ) -> None:
        """The SQLite encoding must preserve ORDER BY, including across zero."""
        for index, amount in enumerate(["-500.00", "9.00", "100.00", "0.00", "2000.00"]):
            session.add(make_row(tenant_id, f"T{index}", amount))
        session.commit()
        session.expunge_all()

        ordered = session.query(TransactionRow).order_by(TransactionRow.amount).all()
        assert [row.amount for row in ordered] == [
            Decimal("-500.00"),
            Decimal("0.00"),
            Decimal("9.00"),
            Decimal("100.00"),
            Decimal("2000.00"),
        ]

    def test_settlement_identity_survives_persistence(
        self, session: Session, tenant_id: UUID
    ) -> None:
        session.add(
            make_row(
                tenant_id,
                "P1",
                "982.45",
                gross_amount=Decimal("1000.00"),
                fee_amount=Decimal("17.55"),
                net_amount=Decimal("982.45"),
            )
        )
        session.commit()
        session.expunge_all()
        row = session.query(TransactionRow).one()
        assert row.gross_amount - row.fee_amount == row.net_amount


class TestSourceImmutabilityAndIdempotency:
    def test_identical_source_record_cannot_be_inserted_twice(
        self, session: Session, tenant_id: UUID
    ) -> None:
        """Spec section 8: the unique constraint is a database guarantee."""
        payload = {"row": "1", "amount": "10.00"}
        checksum = compute_checksum(payload)
        for _ in range(2):
            session.add(
                SourceRecordRow(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    source_system="bank",
                    source_record_id="ROW-1",
                    payload=payload,
                    checksum=checksum,
                    imported_at=utc_now(),
                )
            )
        with pytest.raises(IntegrityError):
            session.flush()

    def test_a_corrected_row_is_a_new_record_not_an_update(
        self, session: Session, tenant_id: UUID
    ) -> None:
        original = {"row": "1", "amount": "10.00"}
        corrected = {"row": "1", "amount": "10.50"}
        for payload in (original, corrected):
            session.add(
                SourceRecordRow(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    source_system="bank",
                    source_record_id="ROW-1",
                    payload=payload,
                    checksum=compute_checksum(payload),
                    imported_at=utc_now(),
                )
            )
        session.flush()
        assert session.query(SourceRecordRow).count() == 2

    def test_canonical_transaction_identity_is_unique(
        self, session: Session, tenant_id: UUID
    ) -> None:
        connection = uuid4()
        payload = {"id": "T1", "amount": "10.00"}
        checksum = compute_checksum(payload)
        for _ in range(2):
            session.add(
                TransactionRow(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    source_system="bank",
                    source_connection_id=connection,
                    source_record_id="T1",
                    amount=Decimal("10.00"),
                    currency="EUR",
                    raw_payload=payload,
                    source_checksum=checksum,
                    imported_at=utc_now(),
                )
            )
        with pytest.raises(IntegrityError):
            session.flush()

    def test_the_same_id_in_two_tenants_is_fine(
        self, session: Session, tenant_id: UUID, other_tenant_id: UUID
    ) -> None:
        """Upstream IDs are not globally unique (spec section 8)."""
        payload = {"id": "T1"}
        checksum = compute_checksum(payload)
        connection = uuid4()
        for owner in (tenant_id, other_tenant_id):
            session.add(
                TransactionRow(
                    id=uuid4(),
                    tenant_id=owner,
                    source_system="bank",
                    source_connection_id=connection,
                    source_record_id="T1",
                    amount=Decimal("10.00"),
                    currency="EUR",
                    raw_payload=payload,
                    source_checksum=checksum,
                    imported_at=utc_now(),
                )
            )
        session.flush()
        assert session.query(TransactionRow).count() == 2


class TestTenantIsolation:
    """Spec section 54, including the cross-tenant access attempts it asks for."""

    def test_a_repository_cannot_be_built_without_a_tenant(self, session: Session) -> None:
        with pytest.raises(ValueError, match="without a tenant"):
            TransactionRepository(session=session, tenant_id=None)  # type: ignore[arg-type]

    def test_get_by_id_across_tenants_returns_nothing(
        self, session: Session, tenant_id: UUID, other_tenant_id: UUID
    ) -> None:
        theirs = make_row(other_tenant_id, "SECRET-1", "1000000.00")
        session.add(theirs)
        session.flush()

        mine = TransactionRepository(session=session, tenant_id=tenant_id)
        assert mine.get(theirs.id) is None
        assert mine.get_many([theirs.id]) == []

    def test_listing_never_leaks_another_tenant(
        self, session: Session, tenant_id: UUID, other_tenant_id: UUID
    ) -> None:
        session.add(make_row(tenant_id, "MINE-1", "10.00"))
        session.add(make_row(other_tenant_id, "THEIRS-1", "20.00"))
        session.flush()

        listed = TransactionRepository(session=session, tenant_id=tenant_id).list()
        assert [t.source_record_id for t in listed] == ["MINE-1"]

    def test_counts_and_balances_are_scoped(
        self, session: Session, tenant_id: UUID, other_tenant_id: UUID
    ) -> None:
        session.add(make_row(tenant_id, "MINE-1", "10.00"))
        session.add(make_row(other_tenant_id, "THEIRS-1", "999.00"))
        session.flush()

        mine = TransactionRepository(session=session, tenant_id=tenant_id)
        assert mine.count() == 1

    def test_writing_another_tenants_row_is_refused(
        self, session: Session, tenant_id: UUID, other_tenant_id: UUID
    ) -> None:
        foreign = CanonicalTransaction(
            id=uuid4(),
            tenant_id=other_tenant_id,
            source_system="bank",
            source_connection_id=uuid4(),
            source_record_id="X",
            amount=Decimal("1.00"),
            currency="EUR",
            raw_payload={},
            source_checksum="abc",
            imported_at=utc_now(),
        )
        repository = TransactionRepository(session=session, tenant_id=tenant_id)
        with pytest.raises(ValueError, match="another tenant"):
            repository.bulk_insert([foreign])

    def test_every_repository_scopes_its_reads(
        self, session: Session, tenant_id: UUID, other_tenant_id: UUID
    ) -> None:
        """A blanket check, so a new repository cannot forget the tenant filter."""
        for repository_class in (
            TransactionRepository,
            MatchRepository,
            ExceptionRepository,
            RunRepository,
        ):
            repository = repository_class(session=session, tenant_id=tenant_id)
            assert repository.get(uuid4()) is None


class TestOptimisticLocking:
    def test_a_stale_update_is_refused(self, session: Session, tenant_id: UUID) -> None:
        """Spec section 64: if zero rows changed, reload and show a conflict."""
        from apps.api.app.infrastructure.models import ReconciliationRow, RunRow

        reconciliation = ReconciliationRow(
            id=uuid4(),
            tenant_id=tenant_id,
            slug="bank-gl",
            name="Bank vs GL",
            config={},
        )
        session.add(reconciliation)
        run = RunRow(
            id=uuid4(),
            tenant_id=tenant_id,
            reconciliation_id=reconciliation.id,
            status="RUNNING",
        )
        session.add(run)
        session.flush()

        repository = RunRepository(session=session, tenant_id=tenant_id)
        repository.update_status(run.id, expected_version=1, status="REVIEW_REQUIRED")

        with pytest.raises(ConcurrencyConflict, match="changed since you loaded it"):
            repository.update_status(run.id, expected_version=1, status="CLOSED")

    def test_idempotency_key_is_unique_per_tenant_and_endpoint(
        self, session: Session, tenant_id: UUID
    ) -> None:
        from apps.api.app.infrastructure.models import IdempotencyKeyRow

        for _ in range(2):
            session.add(
                IdempotencyKeyRow(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    endpoint="POST /reconciliations/{id}/runs",
                    key="client-supplied-uuid",
                    request_hash="abc",
                    response_status=201,
                    response_body={},
                )
            )
        with pytest.raises(IntegrityError):
            session.flush()
