"""Request and response schemas for /api/v1.

Money crosses the wire as a decimal *string*, never a JSON number: a JSON number
is a float in most clients, and 982.45 does not survive that round trip.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer

__all__ = [
    "ApproveMatchRequest",
    "CloseRunRequest",
    "CreateReconciliationRequest",
    "EvidenceResponse",
    "ExceptionResponse",
    "ManualMatchRequest",
    "MappingRequest",
    "MatchResponse",
    "Problem",
    "ReconciliationResponse",
    "RejectMatchRequest",
    "ReopenRunRequest",
    "RunResponse",
    "RunSummaryResponse",
    "StartRunRequest",
    "TransactionResponse",
    "TransitionExceptionRequest",
]


class Base(BaseModel):
    model_config = ConfigDict(populate_by_name=True)


class Problem(Base):
    """RFC 7807-shaped error body."""

    type: str = "about:blank"
    title: str
    status: int
    detail: str
    code: str = ""
    instance: str | None = None


class MoneyMixin(BaseModel):
    @field_serializer("*", when_used="json")
    def _serialise_decimal(self, value: Any) -> Any:
        return str(value) if isinstance(value, Decimal) else value


# ---------------------------------------------------------------------------
# Files and mapping
# ---------------------------------------------------------------------------


class ColumnMappingRequest(Base):
    source_column: str
    canonical_field: str
    date_format: str | None = None
    number_format: str | None = None
    negate: bool = False


class MappingRequest(Base):
    source_system: str
    columns: list[ColumnMappingRequest]
    static_values: dict[str, str] = Field(default_factory=dict)
    debit_column: str | None = None
    credit_column: str | None = None


class FileResponse(Base):
    id: UUID
    filename: str
    byte_size: int
    checksum: str
    status: str
    row_count: int | None
    encoding: str | None
    delimiter: str | None
    sheet_name: str | None
    created_at: datetime


class IngestResponse(Base):
    file_id: UUID
    transactions_created: int
    duplicates_skipped: int
    rows_failed: int
    quality_level: str
    quality_blocking: bool
    findings: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


class TransactionResponse(MoneyMixin, Base):
    id: UUID
    source_system: str
    source_record_id: str
    transaction_date: date | None
    posting_date: date | None
    value_date: date | None
    amount: Decimal
    currency: str
    debit_credit: str
    description: str | None
    reference: str | None
    counterparty_name: str | None
    invoice_number: str | None
    settlement_id: str | None
    external_transaction_id: str | None
    gross_amount: Decimal | None
    fee_amount: Decimal | None
    net_amount: Decimal | None
    status: str | None
    imported_at: datetime


class LineageResponse(Base):
    transaction_id: UUID
    source_record_id: str
    source_checksum: str
    records: list[dict[str, str]]


# ---------------------------------------------------------------------------
# Reconciliations and runs
# ---------------------------------------------------------------------------


class CreateReconciliationRequest(Base):
    slug: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=1, max_length=255)
    template: str | None = None
    config: dict[str, Any] | None = None
    side_a_connection_id: UUID | None = None
    side_b_connection_id: UUID | None = None
    entity: str = "default"


class ReconciliationResponse(Base):
    id: UUID
    slug: str
    name: str
    template: str | None
    config: dict[str, Any]
    config_version: str
    config_hash: str
    entity: str
    version: int
    created_at: datetime


class StartRunRequest(Base):
    period_start: date | None = None
    period_end: date | None = None


class RunResponse(Base):
    id: UUID
    reconciliation_id: UUID
    status: str
    period_start: date | None
    period_end: date | None
    started_at: datetime | None
    finished_at: datetime | None
    result_hash: str | None
    version: int
    replayed: bool = False


class RunSummaryResponse(MoneyMixin, Base):
    run_id: UUID
    reconciliation_id: UUID
    status: str
    period_start: date | None
    period_end: date | None
    currency: str
    side_a_balance: Decimal
    side_b_balance: Decimal
    difference: Decimal
    matched_amount: Decimal
    matched_transaction_count: int
    auto_matched_count: int
    human_approved_count: int
    suggested_count: int
    exception_count: int
    high_risk_exception_count: int
    unmatched_a_count: int
    unmatched_b_count: int
    oldest_exception_age_days: int | None
    completion_pct: float
    auto_match_rate: float
    # Never shown without the false-match rate beside it (spec section 48).
    false_match_rate: float | None
    manual_review_rate: float


# ---------------------------------------------------------------------------
# Matches
# ---------------------------------------------------------------------------


class MatchMemberResponse(MoneyMixin, Base):
    transaction_id: UUID
    side: str
    allocated_amount: Decimal


class MatchReasonResponse(Base):
    code: str
    contribution: float
    description: str


class MatchResponse(MoneyMixin, Base):
    id: UUID
    run_id: UUID
    cardinality: str
    status: str
    decision: str
    confidence: float
    score: float
    currency: str
    total_amount: Decimal
    rule_id: str | None
    rule_version: str | None
    rule_set_version: str | None
    model_version: str | None
    engine_stage: str
    competing_candidate_count: int
    members: list[MatchMemberResponse]
    reasons: list[MatchReasonResponse]
    warnings: list[dict[str, str]]
    approved_by: UUID | None
    approved_at: datetime | None
    override_reason: str | None
    version: int
    explanation: str = ""


class ApproveMatchRequest(Base):
    expected_version: int = Field(ge=1)
    reason: str | None = None


class RejectMatchRequest(Base):
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=3, max_length=2000)


class UnmatchRequest(Base):
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=3, max_length=2000)


class ManualMatchRequest(Base):
    run_id: UUID
    side_a_ids: list[UUID] = Field(min_length=1)
    side_b_ids: list[UUID] = Field(min_length=1)
    reason: str = Field(min_length=3, max_length=2000)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ExceptionResponse(MoneyMixin, Base):
    id: UUID
    reconciliation_run_id: UUID
    transaction_ids: list[UUID]
    category: str
    severity: str
    status: str
    amount_exposure: Decimal | None
    currency: str | None
    title: str
    detail: str
    reason_codes: list[str]
    owner_user_id: UUID | None
    first_detected_at: datetime
    due_at: datetime | None
    proposed_resolution: str | None
    resolution_code: str | None
    proposed_by_actor_type: str | None
    requires_approval: bool
    closed_by: UUID | None
    closed_at: datetime | None
    version: int


class TransitionExceptionRequest(Base):
    status: Literal[
        "TRIAGED",
        "ASSIGNED",
        "INVESTIGATING",
        "PROPOSED_RESOLUTION",
        "AWAITING_APPROVAL",
        "RESOLVED",
        "CLOSED",
        "BLOCKED",
        "ESCALATED",
        "REOPENED",
    ]
    expected_version: int | None = None
    reason: str | None = None
    owner_user_id: UUID | None = None
    resolution_code: str | None = None
    proposed_resolution: str | None = None


class AssignExceptionRequest(Base):
    owner_user_id: UUID
    expected_version: int | None = None


class CommentRequest(Base):
    body: str = Field(min_length=1, max_length=8000)


class CommentResponse(Base):
    id: UUID
    exception_id: UUID
    author_id: UUID
    body: str
    created_at: datetime


class EvidenceResponse(Base):
    id: UUID
    exception_id: UUID | None
    filename: str
    mime_type: str
    byte_size: int
    sha256: str
    kind: str
    uploaded_by: UUID
    uploaded_at: datetime
    download_url: str | None = None


class ProposeResolutionRequest(Base):
    proposed_resolution: str = Field(min_length=3, max_length=4000)
    resolution_code: str | None = None
    expected_version: int | None = None


# ---------------------------------------------------------------------------
# Close
# ---------------------------------------------------------------------------


class CloseRunRequest(Base):
    expected_version: int = Field(ge=1)


class ReopenRunRequest(Base):
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=3, max_length=2000)


class CloseCertificateResponse(MoneyMixin, Base):
    run_id: UUID
    period: str
    status: str
    book_balance: Decimal
    external_balance: Decimal
    difference: Decimal
    unresolved_items: int
    closed_by: UUID
    closed_at: datetime
    config_version: str
    snapshot_hash: str
    certificate_hash: str


class InvestigationRequest(Base):
    question: str = Field(min_length=3, max_length=500)
