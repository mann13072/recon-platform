"""Transaction browsing and lineage (spec sections 37 and 59)."""

from __future__ import annotations

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from apps.api.app.api.schemas import LineageResponse, TransactionResponse
from apps.api.app.dependencies import Context, require_permission
from apps.api.app.services.ingestion import IngestionService
from packages.controls.permissions import Permission

router = APIRouter(prefix="/transactions", tags=["transactions"])

# Account numbers are masked in every response by default (spec section 55).
_MASK_KEEP = 4


def _mask_account(value: str | None) -> str | None:
    if not value or len(value) <= _MASK_KEEP:
        return value
    return "*" * (len(value) - _MASK_KEEP) + value[-_MASK_KEEP:]


@router.get("", response_model=list[TransactionResponse])
def list_transactions(
    context: Context,
    connection_id: UUID | None = None,
    source_system: str | None = None,
    currency: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    search: str | None = None,
    limit: int = Query(100, le=1000),
    offset: int = 0,
    _: Annotated[object, Depends(require_permission(Permission.VIEW_TRANSACTIONS))] = None,
) -> list[TransactionResponse]:
    transactions = IngestionService(context).transactions.list(
        connection_id=connection_id,
        source_system=source_system,
        currency=currency,
        date_from=date_from,
        date_to=date_to,
        search=search,
        limit=limit,
        offset=offset,
    )
    return [TransactionResponse.model_validate(t.model_dump()) for t in transactions]


@router.get("/{transaction_id}", response_model=TransactionResponse)
def get_transaction(
    context: Context,
    transaction_id: UUID,
    _: Annotated[object, Depends(require_permission(Permission.VIEW_TRANSACTIONS))] = None,
) -> TransactionResponse:
    transaction = IngestionService(context).transactions.get(transaction_id)
    if transaction is None:
        raise HTTPException(status_code=404, detail="No such transaction.")
    payload = transaction.model_dump()
    payload["counterparty_account"] = _mask_account(transaction.counterparty_account)
    return TransactionResponse.model_validate(payload)


@router.get("/{transaction_id}/lineage", response_model=LineageResponse)
def get_lineage(
    context: Context,
    transaction_id: UUID,
    _: Annotated[object, Depends(require_permission(Permission.VIEW_TRANSACTIONS))] = None,
) -> LineageResponse:
    """Where every canonical field on this transaction came from."""
    lineage = IngestionService(context).lineage_for(transaction_id)
    if lineage is None:
        raise HTTPException(status_code=404, detail="No such transaction.")
    return LineageResponse(
        transaction_id=lineage.transaction_id,
        source_record_id=lineage.source_record_id,
        source_checksum=lineage.source_checksum,
        records=[
            {
                "canonical_field": record.canonical_field,
                "value": record.value,
                "source_field": record.source_field,
                "transformation": record.transformation,
            }
            for record in lineage.records
        ],
    )
