"""Exception, evidence, approval and accounting-proposal models."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from packages.domain.enums import (
    ExceptionCategory,
    ExceptionSeverity,
    ExceptionStatus,
)

__all__ = [
    "AccountingActionProposal",
    "Approval",
    "Evidence",
    "ExceptionComment",
    "ExceptionRecord",
    "JournalLine",
]


class ExceptionRecord(BaseModel):
    """Spec section 32."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    tenant_id: UUID
    reconciliation_run_id: UUID
    transaction_ids: tuple[UUID, ...]

    category: ExceptionCategory
    severity: ExceptionSeverity

    amount_exposure: Decimal | None = None
    currency: str | None = None

    owner_user_id: UUID | None = None
    status: ExceptionStatus = ExceptionStatus.OPEN

    first_detected_at: datetime
    due_at: datetime | None = None

    title: str = ""
    detail: str = ""
    reason_codes: tuple[str, ...] = ()

    proposed_resolution: str | None = None
    resolution_code: str | None = None
    proposed_by_actor_type: str | None = None

    requires_approval: bool = False
    closed_by: UUID | None = None
    closed_at: datetime | None = None

    ai_suggestion_id: UUID | None = None
    version: int = 1

    @property
    def age_days(self) -> int:
        from packages.domain.dates import utc_now

        return (utc_now() - self.first_detected_at).days


class ExceptionComment(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    tenant_id: UUID
    exception_id: UUID
    author_id: UUID
    body: str
    created_at: datetime


class Evidence(BaseModel):
    """Spec section 92."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    tenant_id: UUID
    exception_id: UUID | None = None
    match_group_id: UUID | None = None
    filename: str
    mime_type: str
    byte_size: int
    sha256: str
    storage_key: str
    uploaded_by: UUID
    uploaded_at: datetime
    retention_policy: str = "default-7y"
    kind: str = "document"


class Approval(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    tenant_id: UUID
    subject_type: str
    subject_id: UUID
    requested_by: UUID
    requested_at: datetime
    approver_id: UUID | None = None
    decided_at: datetime | None = None
    decision: str | None = None
    reason: str | None = None
    required_role: str | None = None


class JournalLine(BaseModel):
    model_config = ConfigDict(frozen=True)

    account: str
    side: str
    amount: Decimal
    currency: str
    description: str | None = None
    entity: str | None = None

    @model_validator(mode="after")
    def _validate(self) -> JournalLine:
        if self.side not in {"debit", "credit"}:
            raise ValueError("journal line side must be 'debit' or 'credit'")
        if self.amount < 0:
            raise ValueError("journal line amounts are unsigned; use the side field")
        return self


class AccountingActionProposal(BaseModel):
    """A proposed adjustment. Never posted automatically in the MVP (spec 45)."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    tenant_id: UUID
    exception_id: UUID | None = None
    run_id: UUID | None = None

    journal_date: str
    entity: str
    currency: str
    lines: tuple[JournalLine, ...]
    description: str
    reason_code: str

    preparer_id: UUID
    required_approver_role: str
    approver_id: UUID | None = None
    status: str = "PROPOSED"
    created_at: datetime | None = None
    posted_at: datetime | None = None

    ai_assisted: bool = False
    ai_suggestion_id: UUID | None = None
    metadata: dict[str, str] = Field(default_factory=dict)
