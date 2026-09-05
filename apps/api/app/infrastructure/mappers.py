"""Translation between ORM rows and domain models.

The domain layer must not know about SQLAlchemy (spec section 82: domain
objects separate from ORM). All conversion happens here, in one file, so the
boundary is visible.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from packages.audit.events import AuditEvent
from packages.domain.dates import ensure_utc
from packages.domain.enums import (
    ActorType,
    AuditAction,
    DecisionOutcome,
    ExceptionCategory,
    ExceptionSeverity,
    ExceptionStatus,
    MatchCardinality,
    MatchGroupStatus,
    Side,
)
from packages.domain.models.exceptions import ExceptionRecord
from packages.domain.models.matching import (
    MatchGroup,
    MatchGroupMember,
    MatchReason,
    MatchWarning,
)
from packages.domain.models.transaction import CanonicalTransaction
from apps.api.app.infrastructure.models import (
    AuditEventRow,
    ExceptionRow,
    MatchGroupMemberRow,
    MatchGroupRow,
    TransactionRow,
)

__all__ = [
    "audit_event_to_row",
    "exception_to_row",
    "match_group_to_rows",
    "row_to_audit_event",
    "row_to_exception",
    "row_to_match_group",
    "row_to_transaction",
    "transaction_to_row",
]


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------

_TRANSACTION_FIELDS = (
    "source_system",
    "source_connection_id",
    "source_record_id",
    "source_account_id",
    "transaction_type",
    "transaction_date",
    "posting_date",
    "value_date",
    "settlement_date",
    "amount",
    "currency",
    "debit_credit",
    "description",
    "normalized_description",
    "reference",
    "normalized_reference",
    "external_transaction_id",
    "bank_reference",
    "counterparty_name",
    "normalized_counterparty",
    "counterparty_account",
    "counterparty_entity_id",
    "customer_id",
    "vendor_id",
    "invoice_number",
    "normalized_invoice_number",
    "purchase_order",
    "check_number",
    "batch_id",
    "settlement_id",
    "payout_id",
    "gross_amount",
    "fee_amount",
    "tax_amount",
    "net_amount",
    "original_amount",
    "original_currency",
    "functional_amount",
    "functional_currency",
    "fx_rate",
    "fx_rate_source",
    "fx_rate_date",
    "status",
    "raw_payload",
    "source_checksum",
    "imported_at",
)


def transaction_to_row(
    transaction: CanonicalTransaction,
    *,
    source_record_row_id: UUID | None = None,
) -> TransactionRow:
    values = {name: getattr(transaction, name) for name in _TRANSACTION_FIELDS}
    return TransactionRow(
        id=transaction.id,
        tenant_id=transaction.tenant_id,
        source_record_row_id=source_record_row_id,
        document_refs=list(transaction.document_refs),
        tags=list(transaction.tags),
        **values,
    )


def row_to_transaction(row: TransactionRow) -> CanonicalTransaction:
    values = {name: getattr(row, name) for name in _TRANSACTION_FIELDS}
    return CanonicalTransaction(
        id=row.id,
        tenant_id=row.tenant_id,
        document_refs=list(row.document_refs or []),
        tags=list(row.tags or []),
        **values,
    )


# ---------------------------------------------------------------------------
# Match groups
# ---------------------------------------------------------------------------


def match_group_to_rows(group: MatchGroup) -> MatchGroupRow:
    row = MatchGroupRow(
        id=group.id,
        tenant_id=group.tenant_id,
        run_id=group.run_id,
        reconciliation_id=group.reconciliation_id,
        cardinality=group.cardinality.value,
        status=group.status.value,
        decision=group.decision.value,
        confidence=group.confidence,
        score=group.score,
        currency=group.currency,
        total_amount=group.total_amount,
        rule_id=group.rule_id,
        rule_version=group.rule_version,
        rule_set_version=group.rule_set_version,
        model_version=group.model_version,
        engine_stage=group.engine_stage,
        reasons=[r.model_dump(mode="json") for r in group.reasons],
        warnings=[w.model_dump(mode="json") for w in group.warnings],
        competing_candidate_count=group.competing_candidate_count,
        extra=dict(group.metadata),
        created_by_actor_type=group.created_by_actor_type,
        approved_by=group.approved_by,
        approved_at=group.approved_at,
        override_reason=group.override_reason,
        version=group.version,
    )
    row.members = [
        MatchGroupMemberRow(
            match_group_id=group.id,
            transaction_id=member.transaction_id,
            tenant_id=group.tenant_id,
            side=member.side.value,
            allocated_amount=member.allocated_amount,
        )
        for member in group.members
    ]
    return row


def row_to_match_group(row: MatchGroupRow) -> MatchGroup:
    return MatchGroup(
        id=row.id,
        tenant_id=row.tenant_id,
        run_id=row.run_id,
        reconciliation_id=row.reconciliation_id,
        members=tuple(
            MatchGroupMember(
                transaction_id=member.transaction_id,
                side=Side(member.side),
                allocated_amount=member.allocated_amount,
            )
            for member in sorted(row.members, key=lambda m: (m.side, str(m.transaction_id)))
        ),
        cardinality=MatchCardinality(row.cardinality),
        status=MatchGroupStatus(row.status),
        decision=DecisionOutcome(row.decision),
        confidence=row.confidence,
        score=row.score,
        currency=row.currency,
        rule_id=row.rule_id,
        rule_version=row.rule_version,
        rule_set_version=row.rule_set_version,
        model_version=row.model_version,
        engine_stage=row.engine_stage,
        reasons=tuple(MatchReason(**r) for r in (row.reasons or [])),
        warnings=tuple(MatchWarning(**w) for w in (row.warnings or [])),
        competing_candidate_count=row.competing_candidate_count,
        created_by_actor_type=row.created_by_actor_type,
        created_at=row.created_at,
        approved_by=row.approved_by,
        approved_at=row.approved_at,
        override_reason=row.override_reason,
        version=row.version,
        metadata={str(k): str(v) for k, v in (row.extra or {}).items()},
    )


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


def exception_to_row(record: ExceptionRecord) -> ExceptionRow:
    return ExceptionRow(
        id=record.id,
        tenant_id=record.tenant_id,
        reconciliation_run_id=record.reconciliation_run_id,
        transaction_ids=[str(i) for i in record.transaction_ids],
        category=record.category.value,
        severity=record.severity.value,
        status=record.status.value,
        amount_exposure=record.amount_exposure,
        currency=record.currency,
        title=record.title,
        detail=record.detail,
        reason_codes=list(record.reason_codes),
        owner_user_id=record.owner_user_id,
        first_detected_at=record.first_detected_at,
        due_at=record.due_at,
        proposed_resolution=record.proposed_resolution,
        resolution_code=record.resolution_code,
        proposed_by_actor_type=record.proposed_by_actor_type,
        ai_suggestion_id=record.ai_suggestion_id,
        requires_approval=record.requires_approval,
        closed_by=record.closed_by,
        closed_at=record.closed_at,
        version=record.version,
    )


def row_to_exception(row: ExceptionRow) -> ExceptionRecord:
    return ExceptionRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        reconciliation_run_id=row.reconciliation_run_id,
        transaction_ids=tuple(UUID(str(i)) for i in (row.transaction_ids or [])),
        category=ExceptionCategory(row.category),
        severity=ExceptionSeverity(row.severity),
        status=ExceptionStatus(row.status),
        amount_exposure=row.amount_exposure,
        currency=row.currency,
        title=row.title,
        detail=row.detail,
        reason_codes=tuple(row.reason_codes or []),
        owner_user_id=row.owner_user_id,
        first_detected_at=row.first_detected_at,
        due_at=row.due_at,
        proposed_resolution=row.proposed_resolution,
        resolution_code=row.resolution_code,
        proposed_by_actor_type=row.proposed_by_actor_type,
        ai_suggestion_id=row.ai_suggestion_id,
        requires_approval=row.requires_approval,
        closed_by=row.closed_by,
        closed_at=row.closed_at,
        version=row.version,
    )


def apply_exception_updates(row: ExceptionRow, record: ExceptionRecord) -> None:
    """Copy a mutated domain record back onto its row, in place."""
    row.status = record.status.value
    row.owner_user_id = record.owner_user_id
    row.proposed_resolution = record.proposed_resolution
    row.resolution_code = record.resolution_code
    row.proposed_by_actor_type = record.proposed_by_actor_type
    row.ai_suggestion_id = record.ai_suggestion_id
    row.closed_by = record.closed_by
    row.closed_at = record.closed_at
    row.version = record.version


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def audit_event_to_row(event: AuditEvent, sequence: int) -> AuditEventRow:
    return AuditEventRow(
        event_id=event.event_id,
        tenant_id=event.tenant_id,
        sequence=sequence,
        occurred_at=event.occurred_at,
        actor_type=event.actor_type.value,
        actor_id=event.actor_id,
        actor_label=event.actor_label,
        action=event.action.value,
        entity_type=event.entity_type,
        entity_id=event.entity_id,
        before_hash=event.before_hash,
        after_hash=event.after_hash,
        previous_event_hash=event.previous_event_hash,
        event_hash=event.event_hash,
        correlation_id=event.correlation_id,
        ip_address=event.ip_address,
        user_agent=event.user_agent,
        reason=event.reason,
        event_metadata=_jsonable(event.metadata),
    )


def row_to_audit_event(row: AuditEventRow) -> AuditEvent:
    """Rebuild an event from its row.

    ``occurred_at`` is forced back to UTC because SQLite has no timezone type
    and returns a naive datetime. The event hash covers the ISO timestamp, so a
    dropped offset would break the chain on read even though nothing was
    tampered with.
    """
    return AuditEvent(
        event_id=row.event_id,
        tenant_id=row.tenant_id,
        occurred_at=ensure_utc(row.occurred_at),
        actor_type=ActorType(row.actor_type),
        actor_id=row.actor_id,
        actor_label=row.actor_label,
        action=AuditAction(row.action),
        entity_type=row.entity_type,
        entity_id=row.entity_id,
        before_hash=row.before_hash,
        after_hash=row.after_hash,
        previous_event_hash=row.previous_event_hash,
        event_hash=row.event_hash,
        correlation_id=row.correlation_id,
        ip_address=row.ip_address,
        user_agent=row.user_agent,
        reason=row.reason,
        metadata=dict(row.event_metadata or {}),
    )


def _jsonable(value: Any) -> Any:
    """Make a metadata payload safe for a JSON column."""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, Decimal | UUID):
        return str(value)
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)
