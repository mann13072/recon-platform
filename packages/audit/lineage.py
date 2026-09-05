"""Data lineage (spec section 59).

For every canonical field, record where the value came from and what transformed
it. During an audit the question is never "what does the system think" but
"where did this number come from", and this is the answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from pydantic import BaseModel, ConfigDict

__all__ = ["LineageRecord", "LineageStore", "TransactionLineage"]


class LineageRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    canonical_field: str
    value: str
    source_record_id: str
    source_field: str
    transformation: str


class TransactionLineage(BaseModel):
    """Full provenance for one canonical transaction."""

    model_config = ConfigDict(frozen=True)

    transaction_id: UUID
    tenant_id: UUID
    source_file_id: UUID | None = None
    source_connection_id: UUID | None = None
    source_record_id: str
    source_checksum: str
    row_number: int | None = None
    mapping_version: str = "v1"
    records: tuple[LineageRecord, ...] = ()

    def for_field(self, canonical_field: str) -> LineageRecord | None:
        for record in self.records:
            if record.canonical_field == canonical_field:
                return record
        return None


@dataclass(slots=True)
class LineageStore:
    """In-memory lineage index, mirrored by the ``transaction_lineage`` table."""

    _by_transaction: dict[UUID, TransactionLineage] = field(default_factory=dict)

    def put(self, lineage: TransactionLineage) -> None:
        self._by_transaction[lineage.transaction_id] = lineage

    def get(self, transaction_id: UUID) -> TransactionLineage | None:
        return self._by_transaction.get(transaction_id)

    def explain(self, transaction_id: UUID, canonical_field: str) -> str:
        """A one-line answer to 'where did this value come from?'."""
        lineage = self.get(transaction_id)
        if lineage is None:
            return "No lineage recorded for this transaction."
        record = lineage.for_field(canonical_field)
        if record is None:
            return (
                f"Field '{canonical_field}' was not populated from the source file "
                f"(source record {lineage.source_record_id})."
            )
        return (
            f"'{record.value}' came from column '{record.source_field}' of source "
            f"record {record.source_record_id}, transformed by "
            f"{record.transformation}."
        )
