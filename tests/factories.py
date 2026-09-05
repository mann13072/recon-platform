"""Test helpers for building canonical transactions without a database."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid5

from packages.domain.dates import utc_now
from packages.domain.models.transaction import CanonicalTransaction, compute_checksum
from packages.ingestion.normalization import (
    normalize_bank_description,
    normalize_invoice_number,
    normalize_name,
    normalize_reference,
)

TEST_NAMESPACE = UUID("1b3f0a44-1c26-4c1e-9a2e-6d51e3b0c001")
TENANT_ID = uuid5(TEST_NAMESPACE, "tenant")
CONNECTION_A = uuid5(TEST_NAMESPACE, "connection-a")
CONNECTION_B = uuid5(TEST_NAMESPACE, "connection-b")
RECONCILIATION_ID = uuid5(TEST_NAMESPACE, "reconciliation")
RUN_ID = uuid5(TEST_NAMESPACE, "run")


def make_transaction(
    source_record_id: str,
    amount: str | Decimal,
    *,
    currency: str = "EUR",
    source_system: str = "bank",
    connection_id: UUID | None = None,
    transaction_date: date | None = None,
    description: str | None = None,
    reference: str | None = None,
    invoice_number: str | None = None,
    counterparty_name: str | None = None,
    external_transaction_id: str | None = None,
    settlement_id: str | None = None,
    payout_id: str | None = None,
    batch_id: str | None = None,
    gross_amount: str | Decimal | None = None,
    fee_amount: str | Decimal | None = None,
    net_amount: str | Decimal | None = None,
    tenant_id: UUID = TENANT_ID,
    imported_at: datetime | None = None,
    **extra: Any,
) -> CanonicalTransaction:
    """Build a canonical transaction with realistic normalised fields.

    The ID is derived from the source record ID so fixtures are reproducible:
    two runs of the same test produce identical transaction IDs, which is what
    lets the reproducibility assertions compare result hashes.
    """
    payload = {"source_record_id": source_record_id, "amount": str(amount)}
    connection = connection_id or (
        CONNECTION_A if source_system == "bank" else CONNECTION_B
    )

    def _decimal(value: str | Decimal | None) -> Decimal | None:
        return None if value is None else Decimal(str(value))

    return CanonicalTransaction(
        id=uuid5(TEST_NAMESPACE, f"{source_system}:{source_record_id}"),
        tenant_id=tenant_id,
        source_system=source_system,
        source_connection_id=connection,
        source_record_id=source_record_id,
        transaction_date=transaction_date,
        amount=Decimal(str(amount)),
        currency=currency,
        description=description,
        normalized_description=normalize_bank_description(description),
        reference=reference,
        normalized_reference=normalize_reference(reference),
        external_transaction_id=external_transaction_id,
        counterparty_name=counterparty_name,
        normalized_counterparty=normalize_name(counterparty_name),
        invoice_number=invoice_number,
        normalized_invoice_number=normalize_invoice_number(invoice_number),
        settlement_id=settlement_id,
        payout_id=payout_id,
        batch_id=batch_id,
        gross_amount=_decimal(gross_amount),
        fee_amount=_decimal(fee_amount),
        net_amount=_decimal(net_amount),
        raw_payload=payload,
        source_checksum=compute_checksum(payload),
        imported_at=imported_at or utc_now(),
        **extra,
    )
