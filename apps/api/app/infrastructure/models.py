"""The database schema (spec section 8).

Design rules that hold across every table:

* every financially meaningful table carries ``tenant_id``, and the repository
  layer refuses to query without it (spec section 54);
* source data is immutable: ``source_records`` has no updated_at and nothing
  updates it (spec section 1.3);
* money is ``Money`` (NUMERIC(24,8) that reads back as ``Decimal``), never float;
* ``audit_events`` is append-only and hash-chained;
* rows that can be edited concurrently carry ``version`` for optimistic locking
  (spec section 64).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from apps.api.app.infrastructure.types import GUID, JSONColumn, Money

__all__ = ["Base"] + [
    "AuditEventRow",
    "CandidateMatchRow",
    "ConnectionRow",
    "ConnectorCredentialRow",
    "EntityAliasRow",
    "EvidenceRow",
    "ExceptionCommentRow",
    "ExceptionRow",
    "IdempotencyKeyRow",
    "JournalProposalRow",
    "MatchGroupMemberRow",
    "MatchGroupRow",
    "PeriodLockRow",
    "ReconciliationRow",
    "RuleSetRow",
    "RunRow",
    "RunSnapshotRow",
    "SourceFileRow",
    "SourceRecordRow",
    "TenantRow",
    "TransactionLineageRow",
    "TransactionRow",
    "UserRoleRow",
    "UserRow",
]


class Base(DeclarativeBase):
    pass


def _pk() -> Mapped[UUID]:
    return mapped_column(GUID, primary_key=True, default=uuid4)


def _tenant_fk() -> Mapped[UUID]:
    return mapped_column(
        GUID, ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False, index=True
    )


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


# ---------------------------------------------------------------------------
# Tenancy and identity
# ---------------------------------------------------------------------------


class TenantRow(Base):
    __tablename__ = "tenants"

    id: Mapped[UUID] = _pk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # Per-tenant AI policy (spec section 56). Defaults to off.
    ai_policy: Mapped[str] = mapped_column(String(32), nullable=False, default="AI_DISABLED")
    ai_allowed_regions: Mapped[list[Any]] = mapped_column(
        JSONColumn, nullable=False, default=lambda: ["eu"]
    )
    data_region: Mapped[str] = mapped_column(String(16), nullable=False, default="eu")
    created_at: Mapped[datetime] = _created_at()


class UserRow(Base):
    """A local projection of the identity provider's user.

    Authentication is bought (spec section 4); this row exists only so that
    approvals, comments and audit events can reference a stable internal ID.
    No password or credential is ever stored here.
    """

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("tenant_id", "external_subject", name="uq_users_tenant_subject"),
        UniqueConstraint("tenant_id", "email", name="uq_users_tenant_email"),
    )

    id: Mapped[UUID] = _pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    external_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    approval_limit: Mapped[Decimal | None] = mapped_column(Money)
    created_at: Mapped[datetime] = _created_at()

    roles: Mapped[list[UserRoleRow]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )


class UserRoleRow(Base):
    __tablename__ = "user_roles"
    __table_args__ = (UniqueConstraint("user_id", "role", name="uq_user_roles_user_role"),)

    id: Mapped[UUID] = _pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    user_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(48), nullable=False)
    granted_by: Mapped[UUID | None] = mapped_column(GUID)
    created_at: Mapped[datetime] = _created_at()

    user: Mapped[UserRow] = relationship(back_populates="roles")


# ---------------------------------------------------------------------------
# Connections and source data
# ---------------------------------------------------------------------------


class ConnectionRow(Base):
    __tablename__ = "connections"
    __table_args__ = (UniqueConstraint("tenant_id", "slug", name="uq_connections_tenant_slug"),)

    id: Mapped[UUID] = _pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    connector_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_system: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="HEALTHY")
    state_detail: Mapped[str | None] = mapped_column(Text)
    cursor: Mapped[str | None] = mapped_column(String(512))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    config: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False, default=dict)
    created_at: Mapped[datetime] = _created_at()
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class ConnectorCredentialRow(Base):
    """Encrypted connector secrets.

    The ciphertext column is the only place a token may live, and it is written
    exclusively through the KMS-backed helper. Spec section 4: never store OAuth
    refresh tokens unencrypted in normal application tables.
    """

    __tablename__ = "connector_credentials"
    __table_args__ = (
        UniqueConstraint("connection_id", "kind", name="uq_credentials_connection_kind"),
    )

    id: Mapped[UUID] = _pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    connection_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("connections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(nullable=False)
    kms_key_id: Mapped[str] = mapped_column(String(255), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()


class SourceFileRow(Base):
    __tablename__ = "source_files"
    __table_args__ = (
        # Re-uploading a byte-identical file is a no-op, not a second import.
        UniqueConstraint("tenant_id", "checksum", name="uq_source_files_tenant_checksum"),
    )

    id: Mapped[UUID] = _pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    connection_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("connections.id", ondelete="SET NULL")
    )
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="UPLOADED")
    encoding: Mapped[str | None] = mapped_column(String(32))
    delimiter: Mapped[str | None] = mapped_column(String(8))
    sheet_name: Mapped[str | None] = mapped_column(String(255))
    row_count: Mapped[int | None] = mapped_column(Integer)
    profile: Mapped[dict[str, Any] | None] = mapped_column(JSONColumn)
    mapping: Mapped[dict[str, Any] | None] = mapped_column(JSONColumn)
    quality_report: Mapped[dict[str, Any] | None] = mapped_column(JSONColumn)
    malware_scan: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")
    uploaded_by: Mapped[UUID | None] = mapped_column(GUID)
    created_at: Mapped[datetime] = _created_at()


class SourceRecordRow(Base):
    """Immutable copy of one upstream row (spec section 1.3).

    Nothing in the codebase updates or deletes a row in this table. The unique
    constraint is what makes re-ingestion idempotent at the database level
    rather than by application convention.
    """

    __tablename__ = "source_records"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "source_system",
            "source_record_id",
            "checksum",
            name="uq_source_records_identity",
        ),
        Index("ix_source_records_file", "source_file_id"),
    )

    id: Mapped[UUID] = _pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    source_system: Mapped[str] = mapped_column(String(64), nullable=False)
    source_connection_id: Mapped[UUID | None] = mapped_column(GUID)
    source_file_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("source_files.id", ondelete="RESTRICT")
    )
    source_record_id: Mapped[str] = mapped_column(String(255), nullable=False)
    row_number: Mapped[int | None] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    imported_at: Mapped[datetime] = _created_at()


class TransactionRow(Base):
    """The canonical transaction (spec section 7)."""

    __tablename__ = "canonical_transactions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "source_connection_id",
            "source_record_id",
            "source_checksum",
            name="uq_transactions_identity",
        ),
        # Candidate generation indexes (spec section 39).
        Index("ix_tx_tenant_currency_date", "tenant_id", "currency", "transaction_date"),
        Index("ix_tx_tenant_amount", "tenant_id", "currency", "amount"),
        Index("ix_tx_tenant_reference", "tenant_id", "normalized_reference"),
        Index("ix_tx_tenant_invoice", "tenant_id", "normalized_invoice_number"),
        Index("ix_tx_tenant_settlement", "tenant_id", "settlement_id"),
        Index("ix_tx_tenant_external", "tenant_id", "external_transaction_id"),
        Index("ix_tx_tenant_counterparty", "tenant_id", "normalized_counterparty"),
        Index("ix_tx_tenant_connection", "tenant_id", "source_connection_id"),
        CheckConstraint("length(currency) = 3", name="ck_tx_currency_length"),
    )

    id: Mapped[UUID] = _pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    source_record_row_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("source_records.id", ondelete="RESTRICT")
    )

    source_system: Mapped[str] = mapped_column(String(64), nullable=False)
    source_connection_id: Mapped[UUID] = mapped_column(GUID, nullable=False)
    source_record_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_account_id: Mapped[str | None] = mapped_column(String(128))

    transaction_type: Mapped[str | None] = mapped_column(String(64))
    transaction_date: Mapped[Any | None] = mapped_column(Date)
    posting_date: Mapped[Any | None] = mapped_column(Date)
    value_date: Mapped[Any | None] = mapped_column(Date)
    settlement_date: Mapped[Any | None] = mapped_column(Date)

    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    debit_credit: Mapped[str] = mapped_column(String(8), nullable=False, default="unknown")

    description: Mapped[str | None] = mapped_column(Text)
    normalized_description: Mapped[str | None] = mapped_column(Text)
    reference: Mapped[str | None] = mapped_column(String(255))
    normalized_reference: Mapped[str | None] = mapped_column(String(255))
    external_transaction_id: Mapped[str | None] = mapped_column(String(255))
    bank_reference: Mapped[str | None] = mapped_column(String(255))

    counterparty_name: Mapped[str | None] = mapped_column(String(512))
    normalized_counterparty: Mapped[str | None] = mapped_column(String(512))
    counterparty_account: Mapped[str | None] = mapped_column(String(128))
    counterparty_entity_id: Mapped[UUID | None] = mapped_column(GUID)

    customer_id: Mapped[str | None] = mapped_column(String(128))
    vendor_id: Mapped[str | None] = mapped_column(String(128))
    invoice_number: Mapped[str | None] = mapped_column(String(128))
    normalized_invoice_number: Mapped[str | None] = mapped_column(String(128))
    purchase_order: Mapped[str | None] = mapped_column(String(128))
    check_number: Mapped[str | None] = mapped_column(String(64))

    batch_id: Mapped[str | None] = mapped_column(String(128))
    settlement_id: Mapped[str | None] = mapped_column(String(128))
    payout_id: Mapped[str | None] = mapped_column(String(128))

    gross_amount: Mapped[Decimal | None] = mapped_column(Money)
    fee_amount: Mapped[Decimal | None] = mapped_column(Money)
    tax_amount: Mapped[Decimal | None] = mapped_column(Money)
    net_amount: Mapped[Decimal | None] = mapped_column(Money)

    original_amount: Mapped[Decimal | None] = mapped_column(Money)
    original_currency: Mapped[str | None] = mapped_column(String(3))
    functional_amount: Mapped[Decimal | None] = mapped_column(Money)
    functional_currency: Mapped[str | None] = mapped_column(String(3))
    fx_rate: Mapped[Decimal | None] = mapped_column(Money)
    fx_rate_source: Mapped[str | None] = mapped_column(String(128))
    fx_rate_date: Mapped[Any | None] = mapped_column(Date)

    status: Mapped[str | None] = mapped_column(String(64))
    document_refs: Mapped[list[Any]] = mapped_column(JSONColumn, nullable=False, default=list)
    tags: Mapped[list[Any]] = mapped_column(JSONColumn, nullable=False, default=list)

    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False)
    source_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    imported_at: Mapped[datetime] = _created_at()

    # Reversal chain (spec section 65). Never a delete.
    reversal_of_transaction_id: Mapped[UUID | None] = mapped_column(GUID)
    reversal_reason: Mapped[str | None] = mapped_column(Text)


class TransactionLineageRow(Base):
    """Per-field provenance (spec section 59)."""

    __tablename__ = "transaction_lineage"
    __table_args__ = (Index("ix_lineage_transaction", "tenant_id", "transaction_id"),)

    id: Mapped[UUID] = _pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    transaction_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("canonical_transactions.id", ondelete="CASCADE"), nullable=False
    )
    canonical_field: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    source_record_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_field: Mapped[str] = mapped_column(String(255), nullable=False)
    transformation: Mapped[str] = mapped_column(String(128), nullable=False)


# ---------------------------------------------------------------------------
# Reconciliation configuration and runs
# ---------------------------------------------------------------------------


class RuleSetRow(Base):
    __tablename__ = "matching_rule_sets"
    __table_args__ = (
        UniqueConstraint("tenant_id", "slug", "version", name="uq_rule_sets_identity"),
    )

    id: Mapped[UUID] = _pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    definition: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[UUID | None] = mapped_column(GUID)
    change_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class ReconciliationRow(Base):
    __tablename__ = "reconciliation_definitions"
    __table_args__ = (UniqueConstraint("tenant_id", "slug", name="uq_reconciliations_tenant_slug"),)

    id: Mapped[UUID] = _pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    template: Mapped[str | None] = mapped_column(String(64))
    config: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False)
    config_version: Mapped[str] = mapped_column(String(32), nullable=False, default="v1")
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    rule_set_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("matching_rule_sets.id", ondelete="RESTRICT")
    )
    side_a_connection_id: Mapped[UUID | None] = mapped_column(GUID)
    side_b_connection_id: Mapped[UUID | None] = mapped_column(GUID)
    entity: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    created_by: Mapped[UUID | None] = mapped_column(GUID)
    created_at: Mapped[datetime] = _created_at()
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class RunRow(Base):
    __tablename__ = "reconciliation_runs"
    __table_args__ = (
        Index("ix_runs_tenant_reconciliation", "tenant_id", "reconciliation_id"),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_runs_tenant_idempotency"),
    )

    id: Mapped[UUID] = _pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    reconciliation_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("reconciliation_definitions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="DRAFT")
    period_start: Mapped[Any | None] = mapped_column(Date)
    period_end: Mapped[Any | None] = mapped_column(Date)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    initiated_by: Mapped[UUID | None] = mapped_column(GUID)
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    result_hash: Mapped[str | None] = mapped_column(String(64))
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSONColumn)
    close_certificate: Mapped[dict[str, Any] | None] = mapped_column(JSONColumn)
    closed_by: Mapped[UUID | None] = mapped_column(GUID)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reopen_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class RunSnapshotRow(Base):
    """Spec section 36: a run must be reproducible from its snapshot alone.

    The transaction ID lists are frozen here at run start. Later imports cannot
    change what an old run reconciled.
    """

    __tablename__ = "run_transaction_snapshots"

    id: Mapped[UUID] = _pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    run_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("reconciliation_runs.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    side_a_transaction_ids: Mapped[list[Any]] = mapped_column(JSONColumn, nullable=False)
    side_b_transaction_ids: Mapped[list[Any]] = mapped_column(JSONColumn, nullable=False)
    source_checksums: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False)
    rule_set_version: Mapped[str] = mapped_column(String(32), nullable=False)
    rule_versions: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False)
    config_version: Mapped[str] = mapped_column(String(32), nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    model_version: Mapped[str | None] = mapped_column(String(64))
    fx_rate_source: Mapped[str | None] = mapped_column(String(128))
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = _created_at()


# ---------------------------------------------------------------------------
# Matching results
# ---------------------------------------------------------------------------


class CandidateMatchRow(Base):
    """Scored candidates, retained so a reviewer can see the alternatives."""

    __tablename__ = "candidate_matches"
    __table_args__ = (Index("ix_candidates_run", "tenant_id", "run_id"),)

    id: Mapped[UUID] = _pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    run_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("reconciliation_runs.id", ondelete="CASCADE"), nullable=False
    )
    side_a_ids: Mapped[list[Any]] = mapped_column(JSONColumn, nullable=False)
    side_b_ids: Mapped[list[Any]] = mapped_column(JSONColumn, nullable=False)
    cardinality: Mapped[str] = mapped_column(String(8), nullable=False)
    generated_by: Mapped[str] = mapped_column(String(64), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    features: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False, default=dict)
    reasons: Mapped[list[Any]] = mapped_column(JSONColumn, nullable=False, default=list)
    warnings: Mapped[list[Any]] = mapped_column(JSONColumn, nullable=False, default=list)
    rule_id: Mapped[str | None] = mapped_column(String(128))
    rule_version: Mapped[str | None] = mapped_column(String(32))
    decision: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = _created_at()


class MatchGroupRow(Base):
    __tablename__ = "match_groups"
    __table_args__ = (
        Index("ix_match_groups_run", "tenant_id", "run_id"),
        Index("ix_match_groups_status", "tenant_id", "status"),
    )

    id: Mapped[UUID] = _pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    run_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("reconciliation_runs.id", ondelete="CASCADE"), nullable=False
    )
    reconciliation_id: Mapped[UUID] = mapped_column(GUID, nullable=False)

    cardinality: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))

    rule_id: Mapped[str | None] = mapped_column(String(128))
    rule_version: Mapped[str | None] = mapped_column(String(32))
    rule_set_version: Mapped[str | None] = mapped_column(String(32))
    model_version: Mapped[str | None] = mapped_column(String(64))
    engine_stage: Mapped[str] = mapped_column(String(32), nullable=False, default="")

    reasons: Mapped[list[Any]] = mapped_column(JSONColumn, nullable=False, default=list)
    warnings: Mapped[list[Any]] = mapped_column(JSONColumn, nullable=False, default=list)
    competing_candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    extra: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False, default=dict)

    created_by_actor_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="MATCH_ENGINE"
    )
    created_by: Mapped[UUID | None] = mapped_column(GUID)
    approved_by: Mapped[UUID | None] = mapped_column(GUID)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_by: Mapped[UUID | None] = mapped_column(GUID)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    override_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    members: Mapped[list[MatchGroupMemberRow]] = relationship(
        back_populates="group", cascade="all, delete-orphan", lazy="selectin"
    )


class MatchGroupMemberRow(Base):
    """Arbitrary cardinality: 1:1, 1:N, N:1 and N:N (spec section 8)."""

    __tablename__ = "match_group_members"
    __table_args__ = (
        CheckConstraint("side IN ('A', 'B')", name="ck_member_side"),
        Index("ix_members_transaction", "tenant_id", "transaction_id"),
    )

    match_group_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("match_groups.id", ondelete="CASCADE"), primary_key=True
    )
    transaction_id: Mapped[UUID] = mapped_column(GUID, primary_key=True)
    tenant_id: Mapped[UUID] = _tenant_fk()
    side: Mapped[str] = mapped_column(String(1), nullable=False)
    allocated_amount: Mapped[Decimal] = mapped_column(Money, nullable=False)

    group: Mapped[MatchGroupRow] = relationship(back_populates="members")


# ---------------------------------------------------------------------------
# Exceptions and evidence
# ---------------------------------------------------------------------------


class ExceptionRow(Base):
    __tablename__ = "exceptions"
    __table_args__ = (
        Index("ix_exceptions_run", "tenant_id", "reconciliation_run_id"),
        Index("ix_exceptions_status", "tenant_id", "status"),
        Index("ix_exceptions_owner", "tenant_id", "owner_user_id"),
    )

    id: Mapped[UUID] = _pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    reconciliation_run_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("reconciliation_runs.id", ondelete="CASCADE"), nullable=False
    )
    transaction_ids: Mapped[list[Any]] = mapped_column(JSONColumn, nullable=False, default=list)

    category: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="OPEN")

    amount_exposure: Mapped[Decimal | None] = mapped_column(Money)
    currency: Mapped[str | None] = mapped_column(String(3))

    title: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    reason_codes: Mapped[list[Any]] = mapped_column(JSONColumn, nullable=False, default=list)

    owner_user_id: Mapped[UUID | None] = mapped_column(GUID)
    first_detected_at: Mapped[datetime] = _created_at()
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    proposed_resolution: Mapped[str | None] = mapped_column(Text)
    resolution_code: Mapped[str | None] = mapped_column(String(64))
    proposed_by_actor_type: Mapped[str | None] = mapped_column(String(32))
    ai_suggestion_id: Mapped[UUID | None] = mapped_column(GUID)

    requires_approval: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    closed_by: Mapped[UUID | None] = mapped_column(GUID)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class ExceptionCommentRow(Base):
    __tablename__ = "exception_comments"
    __table_args__ = (Index("ix_comments_exception", "tenant_id", "exception_id"),)

    id: Mapped[UUID] = _pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    exception_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("exceptions.id", ondelete="CASCADE"), nullable=False
    )
    author_id: Mapped[UUID] = mapped_column(GUID, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created_at()


class EvidenceRow(Base):
    __tablename__ = "exception_evidence"
    __table_args__ = (
        Index("ix_evidence_exception", "tenant_id", "exception_id"),
        Index("ix_evidence_match", "tenant_id", "match_group_id"),
    )

    id: Mapped[UUID] = _pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    exception_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("exceptions.id", ondelete="CASCADE")
    )
    match_group_id: Mapped[UUID | None] = mapped_column(GUID)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False, default="document")
    retention_policy: Mapped[str] = mapped_column(String(64), nullable=False, default="default-7y")
    extraction: Mapped[dict[str, Any] | None] = mapped_column(JSONColumn)
    uploaded_by: Mapped[UUID] = mapped_column(GUID, nullable=False)
    uploaded_at: Mapped[datetime] = _created_at()


class JournalProposalRow(Base):
    __tablename__ = "accounting_action_proposals"
    __table_args__ = (Index("ix_proposals_run", "tenant_id", "run_id"),)

    id: Mapped[UUID] = _pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    exception_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("exceptions.id", ondelete="SET NULL")
    )
    run_id: Mapped[UUID | None] = mapped_column(GUID)
    journal_date: Mapped[str] = mapped_column(String(32), nullable=False)
    entity: Mapped[str] = mapped_column(String(64), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    lines: Mapped[list[Any]] = mapped_column(JSONColumn, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PROPOSED")
    preparer_id: Mapped[UUID] = mapped_column(GUID, nullable=False)
    required_approver_role: Mapped[str] = mapped_column(String(48), nullable=False)
    approver_id: Mapped[UUID | None] = mapped_column(GUID)
    ai_assisted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ai_suggestion_id: Mapped[UUID | None] = mapped_column(GUID)
    created_at: Mapped[datetime] = _created_at()
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class EntityAliasRow(Base):
    """Counterparty aliases (spec section 25). Never applied without approval."""

    __tablename__ = "entity_aliases"
    __table_args__ = (
        UniqueConstraint("tenant_id", "normalized_alias", name="uq_alias_tenant_value"),
    )

    id: Mapped[UUID] = _pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    entity_id: Mapped[UUID] = mapped_column(GUID, nullable=False)
    entity_name: Mapped[str] = mapped_column(String(512), nullable=False)
    alias: Mapped[str] = mapped_column(String(512), nullable=False)
    normalized_alias: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
    approved_by: Mapped[UUID | None] = mapped_column(GUID)
    created_at: Mapped[datetime] = _created_at()


class PeriodLockRow(Base):
    __tablename__ = "period_locks"
    __table_args__ = (Index("ix_period_locks_entity", "tenant_id", "entity"),)

    id: Mapped[UUID] = _pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    entity: Mapped[str] = mapped_column(String(64), nullable=False)
    period_start: Mapped[Any] = mapped_column(Date, nullable=False)
    period_end: Mapped[Any] = mapped_column(Date, nullable=False)
    locked_by: Mapped[UUID] = mapped_column(GUID, nullable=False)
    locked_at: Mapped[datetime] = _created_at()
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(Text)


# ---------------------------------------------------------------------------
# Audit and idempotency
# ---------------------------------------------------------------------------


class AuditEventRow(Base):
    """Append-only. Nothing in the codebase updates or deletes these rows.

    In production the database role used by the application is granted INSERT
    and SELECT on this table only - see docs/security.md.
    """

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_tenant_time", "tenant_id", "occurred_at"),
        Index("ix_audit_entity", "tenant_id", "entity_type", "entity_id"),
        Index("ix_audit_sequence", "tenant_id", "sequence"),
        UniqueConstraint("tenant_id", "sequence", name="uq_audit_tenant_sequence"),
    )

    event_id: Mapped[UUID] = mapped_column(GUID, primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = _tenant_fk()
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    occurred_at: Mapped[datetime] = _created_at()

    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[UUID | None] = mapped_column(GUID)
    actor_label: Mapped[str | None] = mapped_column(String(255))

    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[UUID | None] = mapped_column(GUID)

    before_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    after_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    previous_event_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    correlation_id: Mapped[UUID | None] = mapped_column(GUID)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    reason: Mapped[str | None] = mapped_column(Text)
    event_metadata: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False, default=dict)


class AICallRow(Base):
    """Audit metadata for every AI call (spec section 29)."""

    __tablename__ = "ai_calls"
    __table_args__ = (Index("ix_ai_calls_tenant_time", "tenant_id", "occurred_at"),)

    id: Mapped[UUID] = _pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    task: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy: Mapped[str] = mapped_column(String(32), nullable=False)
    redacted_field_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    cost_usd: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    schema_valid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    output_summary: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    user_decision: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    subject_type: Mapped[str | None] = mapped_column(String(64))
    subject_id: Mapped[UUID | None] = mapped_column(GUID)
    occurred_at: Mapped[datetime] = _created_at()


class IdempotencyKeyRow(Base):
    """Spec section 63: an API write replayed with the same key returns the
    original result instead of doing the work twice."""

    __tablename__ = "idempotency_keys"
    __table_args__ = (
        UniqueConstraint("tenant_id", "endpoint", "key", name="uq_idempotency_identity"),
    )

    id: Mapped[UUID] = _pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    endpoint: Mapped[str] = mapped_column(String(255), nullable=False)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False)
    created_at: Mapped[datetime] = _created_at()
