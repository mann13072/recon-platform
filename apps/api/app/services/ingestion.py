"""File ingestion (spec section 10).

    Upload -> scan -> checksum -> save raw -> detect encoding -> detect
    delimiter/sheet -> profile -> suggest mappings -> user confirms -> validate
    -> normalize -> persist -> quality report

The user confirms the mapping before anything is normalised. Nothing on this
path repairs data: problems become findings on the quality report.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from apps.api.app.dependencies import RequestContext
from apps.api.app.infrastructure.models import SourceFileRow, SourceRecordRow
from apps.api.app.infrastructure.repositories import (
    SourceFileRepository,
    TransactionRepository,
)
from apps.api.app.infrastructure.storage import safe_filename
from packages.audit.evidence import sha256_bytes
from packages.audit.lineage import LineageRecord, TransactionLineage
from packages.domain.enums import AuditAction, QualityLevel
from packages.domain.models.transaction import CanonicalTransaction
from packages.ingestion.mapping import (
    ColumnMapping,
    MappingError,
    SourceMapping,
    apply_mapping,
    suggest_mapping,
)
from packages.ingestion.normalization import NumberFormat
from packages.ingestion.parsers import ParsedTable, parse_any
from packages.ingestion.profiler import FileProfile, profile_table
from packages.ingestion.quality import QualityReport, run_quality_checks

__all__ = ["IngestionError", "IngestionResult", "IngestionService"]

# Extensions the platform will parse. Anything else is refused at upload rather
# than stored and discovered later (spec section 53: secure file upload).
ALLOWED_EXTENSIONS = (".csv", ".tsv", ".txt", ".xlsx", ".xlsm", ".json", ".jsonl", ".ndjson")

# Byte signatures of formats that must never be accepted as a data file.
_FORBIDDEN_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"MZ", "a Windows executable"),
    (b"\x7fELF", "an ELF executable"),
    (b"%PDF", "a PDF"),
    (b"\xca\xfe\xba\xbe", "a Java class file"),
)


class IngestionError(ValueError):
    """Raised when a file cannot be accepted or mapped."""

    def __init__(self, message: str, code: str = "ingestion_failed") -> None:
        super().__init__(message)
        self.code = code


@dataclass(slots=True)
class IngestionResult:
    file_id: UUID
    transactions_created: int
    duplicates_skipped: int
    rows_failed: int
    quality: QualityReport
    already_ingested: bool = False


@dataclass(slots=True)
class IngestionService:
    context: RequestContext

    @property
    def files(self) -> SourceFileRepository:
        return SourceFileRepository(session=self.context.session, tenant_id=self.context.tenant_id)

    @property
    def transactions(self) -> TransactionRepository:
        return TransactionRepository(session=self.context.session, tenant_id=self.context.tenant_id)

    # -- upload ------------------------------------------------------------
    def upload(
        self,
        *,
        filename: str,
        data: bytes,
        mime_type: str,
        connection_id: UUID | None = None,
    ) -> SourceFileRow:
        """Store the raw file and profile it. Nothing is normalised yet."""
        self._screen(filename, data)

        checksum = sha256_bytes(data)
        existing = self.files.by_checksum(checksum)
        if existing is not None:
            # A byte-identical re-upload is the same file, not a second one.
            return existing

        key, _ = self.context.storage.put_content(
            self.context.tenant_id,
            "uploads",
            data,
            extension=filename.rsplit(".", 1)[-1] if "." in filename else "",
            content_type=mime_type,
        )

        table = self._parse(filename, data)
        profile = profile_table(table)
        suggestion = suggest_mapping(profile, _source_system_for(filename))

        row = self.files.add(
            SourceFileRow(
                id=uuid4(),
                tenant_id=self.context.tenant_id,
                connection_id=connection_id,
                filename=safe_filename(filename),
                mime_type=mime_type,
                byte_size=len(data),
                checksum=checksum,
                storage_key=key,
                status="PROFILED",
                encoding=table.encoding,
                delimiter=table.delimiter,
                sheet_name=table.sheet_name,
                row_count=len(table.rows),
                profile=_profile_to_json(profile),
                mapping=_mapping_to_json(suggestion),
                malware_scan="SCANNED_BASIC",
                uploaded_by=self.context.principal.user_id,
            )
        )

        self.context.audit.record(
            self.context.audit_context(),
            AuditAction.FILE_UPLOADED,
            "SOURCE_FILE",
            row.id,
            after={"checksum": checksum, "rows": len(table.rows)},
            metadata={"filename": row.filename, "byte_size": len(data)},
        )
        return row

    def _screen(self, filename: str, data: bytes) -> None:
        """Reject what should never have been uploaded."""
        if not data:
            raise IngestionError("The uploaded file is empty.", "empty_file")
        if len(data) > self.context.settings.max_upload_bytes:
            limit_mb = self.context.settings.max_upload_bytes // (1024 * 1024)
            raise IngestionError(
                f"The file exceeds the {limit_mb}MB upload limit.", "file_too_large"
            )
        lowered = filename.lower()
        if not lowered.endswith(ALLOWED_EXTENSIONS):
            raise IngestionError(
                "Only CSV, TSV, XLSX and JSON files can be imported. "
                f"'{safe_filename(filename)}' is not one of those.",
                "unsupported_type",
            )
        for signature, description in _FORBIDDEN_SIGNATURES:
            if data.startswith(signature):
                raise IngestionError(
                    f"The upload is {description}, not a data file.", "forbidden_content"
                )

    def _parse(self, filename: str, data: bytes) -> ParsedTable:
        try:
            return parse_any(data, filename)
        except (UnicodeDecodeError, ValueError, RuntimeError) as exc:
            raise IngestionError(f"The file could not be parsed: {exc}", "parse_failed") from exc

    # -- profile / mapping -------------------------------------------------
    def profile(self, file_id: UUID) -> dict[str, Any]:
        row = self._require_file(file_id)
        if row.profile is None:
            data = self.context.storage.get(row.storage_key)
            profile = profile_table(self._parse(row.filename, data))
            row.profile = _profile_to_json(profile)
            self.context.session.flush()
        return dict(row.profile)

    def set_mapping(self, file_id: UUID, mapping: SourceMapping) -> SourceFileRow:
        """Record the user's confirmed mapping. Validated, not applied yet."""
        row = self._require_file(file_id)
        try:
            mapping.validate()
        except MappingError as exc:
            raise IngestionError(str(exc), "invalid_mapping") from exc

        row.mapping = _mapping_to_json(mapping)
        row.status = "MAPPED"
        self.context.session.flush()

        self.context.audit.record(
            self.context.audit_context(),
            AuditAction.FILE_MAPPED,
            "SOURCE_FILE",
            row.id,
            after=row.mapping,
        )
        return row

    # -- ingest ------------------------------------------------------------
    def ingest(self, file_id: UUID, *, connection_id: UUID | None = None) -> IngestionResult:
        """Normalise and persist. Idempotent: re-running creates no duplicates."""
        row = self._require_file(file_id)
        if row.mapping is None:
            raise IngestionError(
                "Confirm the column mapping before ingesting this file.",
                "mapping_required",
            )

        data = self.context.storage.get(row.storage_key)
        table = self._parse(row.filename, data)
        mapping = _mapping_from_json(row.mapping)
        connection = connection_id or row.connection_id or row.id

        result = apply_mapping(
            table.rows,
            mapping,
            tenant_id=self.context.tenant_id,
            source_connection_id=connection,
        )

        profile = profile_table(table)
        quality = run_quality_checks(
            result.transactions,
            row_errors=result.row_errors,
            profile=profile,
            previous_row_count=row.row_count if row.status == "INGESTED" else None,
        )

        row.quality_report = _quality_to_json(quality)

        if quality.blocking:
            row.status = "QUALITY_FAILED"
            self.context.session.flush()
            self.context.audit.record(
                self.context.audit_context(),
                AuditAction.FILE_INGESTED,
                "SOURCE_FILE",
                row.id,
                reason="blocked by data-quality errors",
                metadata={"level": quality.level.value},
            )
            return IngestionResult(
                file_id=row.id,
                transactions_created=0,
                duplicates_skipped=0,
                rows_failed=len(result.row_errors),
                quality=quality,
            )

        existing = self.transactions.existing_identity_keys(
            [(connection, t.source_record_id, t.source_checksum) for t in result.transactions]
        )
        fresh = [
            t
            for t in result.transactions
            if (connection, t.source_record_id, t.source_checksum) not in existing
        ]
        duplicates = len(result.transactions) - len(fresh)

        self._persist_source_records(row, fresh)
        self.transactions.bulk_insert(fresh)
        self._persist_lineage(result, fresh, row)

        row.status = "INGESTED"
        row.row_count = len(table.rows)
        self.context.session.flush()

        self.context.audit.record(
            self.context.audit_context(),
            AuditAction.FILE_INGESTED,
            "SOURCE_FILE",
            row.id,
            after={"created": len(fresh), "skipped": duplicates},
            metadata={
                "quality_level": quality.level.value,
                "rows_failed": len(result.row_errors),
            },
        )

        return IngestionResult(
            file_id=row.id,
            transactions_created=len(fresh),
            duplicates_skipped=duplicates,
            rows_failed=len(result.row_errors),
            quality=quality,
            already_ingested=duplicates > 0 and not fresh,
        )

    def _persist_source_records(
        self, file_row: SourceFileRow, transactions: list[CanonicalTransaction]
    ) -> None:
        """Keep the original rows verbatim (spec section 1.3)."""
        rows = [
            SourceRecordRow(
                id=uuid4(),
                tenant_id=self.context.tenant_id,
                source_system=transaction.source_system,
                source_connection_id=transaction.source_connection_id,
                source_file_id=file_row.id,
                source_record_id=transaction.source_record_id,
                payload=transaction.raw_payload,
                checksum=transaction.source_checksum,
                imported_at=transaction.imported_at,
            )
            for transaction in transactions
        ]
        self.files.add_source_records(rows)

    def _persist_lineage(
        self, result: Any, kept: list[CanonicalTransaction], file_row: SourceFileRow
    ) -> None:
        """Store per-field provenance for the transactions actually created."""
        from apps.api.app.infrastructure.models import TransactionLineageRow

        by_record: dict[str, list[Any]] = {}
        for record in result.lineage:
            by_record.setdefault(record.source_record_id, []).append(record)

        for transaction in kept:
            for record in by_record.get(transaction.source_record_id, []):
                self.context.session.add(
                    TransactionLineageRow(
                        id=uuid4(),
                        tenant_id=self.context.tenant_id,
                        transaction_id=transaction.id,
                        canonical_field=record.canonical_field,
                        value=record.value[:4000],
                        source_record_id=record.source_record_id,
                        source_field=record.source_field,
                        transformation=record.transformation,
                    )
                )
        del file_row
        self.context.session.flush()

    def lineage_for(self, transaction_id: UUID) -> TransactionLineage | None:
        from apps.api.app.infrastructure.models import TransactionLineageRow

        transaction = self.transactions.get(transaction_id)
        if transaction is None:
            return None
        rows = (
            self.context.session.query(TransactionLineageRow)
            .filter(
                TransactionLineageRow.tenant_id == self.context.tenant_id,
                TransactionLineageRow.transaction_id == transaction_id,
            )
            .all()
        )
        return TransactionLineage(
            transaction_id=transaction_id,
            tenant_id=self.context.tenant_id,
            source_connection_id=transaction.source_connection_id,
            source_record_id=transaction.source_record_id,
            source_checksum=transaction.source_checksum,
            records=tuple(
                LineageRecord(
                    canonical_field=row.canonical_field,
                    value=row.value,
                    source_record_id=row.source_record_id,
                    source_field=row.source_field,
                    transformation=row.transformation,
                )
                for row in rows
            ),
        )

    def _require_file(self, file_id: UUID) -> SourceFileRow:
        row = self.files.get(file_id)
        if row is None:
            raise IngestionError("No such file.", "not_found")
        return row


# ---------------------------------------------------------------------------
# JSON projections
# ---------------------------------------------------------------------------


def _source_system_for(filename: str) -> str:
    lowered = filename.lower()
    for token, system in (
        ("bank", "bank"),
        ("statement", "bank"),
        ("ledger", "ledger"),
        ("gl", "ledger"),
        ("stripe", "processor"),
        ("payout", "processor"),
    ):
        if token in lowered:
            return system
    return "unknown"


def _profile_to_json(profile: FileProfile) -> dict[str, Any]:
    return {
        "row_count": profile.row_count,
        "column_count": profile.column_count,
        "encoding": profile.encoding,
        "delimiter": profile.delimiter,
        "sheet_name": profile.sheet_name,
        "skipped_rows": [{"line": n, "reason": r} for n, r in profile.skipped_rows[:50]],
        "columns": [
            {
                "name": column.name,
                "inferred_type": column.inferred_type.value,
                "non_empty_count": column.non_empty_count,
                "empty_count": column.empty_count,
                "distinct_count": column.distinct_count,
                "fill_rate": round(column.fill_rate, 4),
                "sample_values": column.sample_values,
                "min_value": column.min_value,
                "max_value": column.max_value,
                "number_format": column.number_format.value if column.number_format else None,
                "date_format": column.date_format,
                "duplicate_ratio": round(column.duplicate_ratio, 4),
                "negative_count": column.negative_count,
                "warnings": column.warnings,
            }
            for column in profile.columns
        ],
    }


def _mapping_to_json(mapping: SourceMapping) -> dict[str, Any]:
    return {
        "source_system": mapping.source_system,
        "mapping_version": mapping.mapping_version,
        "debit_column": mapping.debit_column,
        "credit_column": mapping.credit_column,
        "static_values": mapping.static_values,
        "columns": [
            {
                "source_column": column.source_column,
                "canonical_field": column.canonical_field,
                "date_format": column.date_format,
                "number_format": column.number_format.value if column.number_format else None,
                "negate": column.negate,
                "confidence": column.confidence,
                "rationale": column.rationale,
            }
            for column in mapping.columns
        ],
    }


def _mapping_from_json(payload: dict[str, Any]) -> SourceMapping:
    return SourceMapping(
        source_system=payload.get("source_system", "unknown"),
        mapping_version=payload.get("mapping_version", "v1"),
        debit_column=payload.get("debit_column"),
        credit_column=payload.get("credit_column"),
        static_values=dict(payload.get("static_values") or {}),
        columns=[
            ColumnMapping(
                source_column=column["source_column"],
                canonical_field=column["canonical_field"],
                date_format=column.get("date_format"),
                number_format=(
                    NumberFormat(column["number_format"]) if column.get("number_format") else None
                ),
                negate=bool(column.get("negate", False)),
                confidence=float(column.get("confidence", 0.0)),
                rationale=column.get("rationale", ""),
            )
            for column in payload.get("columns", [])
        ],
    )


def _quality_to_json(report: QualityReport) -> dict[str, Any]:
    return {
        "level": report.level.value,
        "blocking": report.blocking,
        "row_count": report.row_count,
        "total_by_currency": {k: str(v) for k, v in report.total_by_currency.items()},
        "findings": [
            {
                "code": finding.code,
                "level": finding.level.value,
                "message": finding.message,
                "affected_count": finding.affected_count,
                "sample_rows": list(finding.sample_rows),
            }
            for finding in report.findings
        ],
    }


def quality_is_blocking(payload: dict[str, Any] | None) -> bool:
    return bool(payload and payload.get("level") == QualityLevel.FAIL.value)
