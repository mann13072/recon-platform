"""The canonical transaction model (spec section 7).

Everything downstream of ingestion speaks this shape. Source-specific fields
that do not fit are preserved verbatim in ``raw_payload`` rather than being
forced into a field that means something else.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from packages.domain.dates import DateField
from packages.domain.money import Money

__all__ = ["CanonicalTransaction", "SourceRecord", "compute_checksum"]


def compute_checksum(payload: dict[str, Any]) -> str:
    """Stable SHA-256 over a source payload.

    Sorted keys and a fixed separator make the checksum reproducible across
    processes and Python versions, which is what makes re-ingestion idempotent
    (spec sections 51 and 63).
    """
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class SourceRecord(BaseModel):
    """An immutable copy of one upstream row (spec section 1.3).

    Source records are never updated or deleted. A correction from the upstream
    system arrives as a new record with a different checksum.
    """

    model_config = ConfigDict(frozen=True)

    id: UUID
    tenant_id: UUID
    source_system: str
    source_connection_id: UUID | None = None
    source_file_id: UUID | None = None
    source_record_id: str
    row_number: int | None = None
    payload: dict[str, Any]
    checksum: str
    imported_at: datetime

    @classmethod
    def checksum_for(cls, payload: dict[str, Any]) -> str:
        return compute_checksum(payload)


class CanonicalTransaction(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    tenant_id: UUID

    source_system: str
    source_connection_id: UUID
    source_record_id: str
    source_account_id: str | None = None

    transaction_type: str | None = None

    transaction_date: date | None = None
    posting_date: date | None = None
    value_date: date | None = None
    settlement_date: date | None = None

    amount: Decimal
    currency: str = Field(min_length=3, max_length=3)

    debit_credit: Literal["debit", "credit", "unknown"] = "unknown"

    description: str | None = None
    normalized_description: str | None = None

    reference: str | None = None
    normalized_reference: str | None = None
    external_transaction_id: str | None = None
    bank_reference: str | None = None

    counterparty_name: str | None = None
    normalized_counterparty: str | None = None
    counterparty_account: str | None = None
    counterparty_entity_id: UUID | None = None

    customer_id: str | None = None
    vendor_id: str | None = None
    invoice_number: str | None = None
    normalized_invoice_number: str | None = None
    purchase_order: str | None = None
    check_number: str | None = None

    batch_id: str | None = None
    settlement_id: str | None = None
    payout_id: str | None = None

    gross_amount: Decimal | None = None
    fee_amount: Decimal | None = None
    tax_amount: Decimal | None = None
    net_amount: Decimal | None = None

    original_amount: Decimal | None = None
    original_currency: str | None = None
    functional_amount: Decimal | None = None
    functional_currency: str | None = None
    fx_rate: Decimal | None = None
    fx_rate_source: str | None = None
    fx_rate_date: date | None = None

    status: str | None = None

    document_refs: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)

    raw_payload: dict[str, Any]
    source_checksum: str

    imported_at: datetime

    @field_validator("currency", "original_currency", "functional_currency")
    @classmethod
    def _upper_currency(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().upper()
        if len(value) != 3 or not value.isalpha():
            raise ValueError("currency must be a 3-letter alphabetic ISO code")
        return value

    @field_validator(
        "amount",
        "gross_amount",
        "fee_amount",
        "tax_amount",
        "net_amount",
        "original_amount",
        "functional_amount",
        "fx_rate",
        mode="before",
    )
    @classmethod
    def _reject_float(cls, value: Any) -> Any:
        if isinstance(value, float):
            raise TypeError("float amounts are rejected; use Decimal or str")
        return value

    # -- convenience -------------------------------------------------------
    @property
    def money(self) -> Money:
        return Money(self.amount, self.currency)

    def date_for(self, field: DateField) -> date | None:
        return {
            DateField.TRANSACTION: self.transaction_date,
            DateField.POSTING: self.posting_date,
            DateField.VALUE: self.value_date,
            DateField.SETTLEMENT: self.settlement_date,
        }[field]

    @property
    def best_date(self) -> date | None:
        """Preferred date when a rule does not name one explicitly."""
        return self.transaction_date or self.posting_date or self.value_date

    @property
    def identity_key(self) -> tuple[UUID, UUID, str, str]:
        """Uniqueness tuple from spec section 8.

        Upstream IDs are not assumed globally unique, so tenant, connection and
        content checksum all participate.
        """
        return (
            self.tenant_id,
            self.source_connection_id,
            self.source_record_id,
            self.source_checksum,
        )
