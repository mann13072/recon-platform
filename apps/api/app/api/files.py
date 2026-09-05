"""File upload, profiling, mapping and ingestion (spec section 37)."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from apps.api.app.api.schemas import (
    FileResponse,
    IngestResponse,
    MappingRequest,
)
from apps.api.app.dependencies import Context, require_permission
from apps.api.app.services.ingestion import IngestionError, IngestionService
from packages.controls.permissions import Permission
from packages.ingestion.mapping import ColumnMapping, SourceMapping
from packages.ingestion.normalization import NumberFormat

router = APIRouter(prefix="/files", tags=["files"])


def _problem(exc: IngestionError) -> HTTPException:
    code = (
        status.HTTP_404_NOT_FOUND
        if exc.code == "not_found"
        else status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
        if exc.code == "file_too_large"
        else status.HTTP_422_UNPROCESSABLE_ENTITY
    )
    return HTTPException(status_code=code, detail=str(exc))


@router.post("", response_model=FileResponse, status_code=status.HTTP_201_CREATED)
async def upload_file(
    context: Context,
    file: Annotated[UploadFile, File()],
    connection_id: Annotated[UUID | None, Form()] = None,
    _: Annotated[object, Depends(require_permission(Permission.UPLOAD_FILE))] = None,
) -> FileResponse:
    """Upload a source file. It is stored and profiled, not yet ingested."""
    data = await file.read()
    try:
        row = IngestionService(context).upload(
            filename=file.filename or "upload.csv",
            data=data,
            mime_type=file.content_type or "text/csv",
            connection_id=connection_id,
        )
    except IngestionError as exc:
        raise _problem(exc) from exc

    return FileResponse.model_validate(row, from_attributes=True)


@router.get("", response_model=list[FileResponse])
def list_files(
    context: Context,
    limit: int = 50,
    offset: int = 0,
    _: Annotated[object, Depends(require_permission(Permission.VIEW_TRANSACTIONS))] = None,
) -> list[FileResponse]:
    rows = IngestionService(context).files.list(limit=min(limit, 200), offset=offset)
    return [FileResponse.model_validate(row, from_attributes=True) for row in rows]


@router.get("/{file_id}/profile")
def get_profile(
    context: Context,
    file_id: UUID,
    _: Annotated[object, Depends(require_permission(Permission.UPLOAD_FILE))] = None,
) -> dict:
    """Column profile plus the platform's suggested mapping."""
    service = IngestionService(context)
    try:
        profile = service.profile(file_id)
    except IngestionError as exc:
        raise _problem(exc) from exc
    row = service.files.get(file_id)
    return {"profile": profile, "suggested_mapping": row.mapping if row else None}


@router.post("/{file_id}/mapping", response_model=FileResponse)
def set_mapping(
    context: Context,
    file_id: UUID,
    payload: MappingRequest,
    _: Annotated[object, Depends(require_permission(Permission.INGEST_FILE))] = None,
) -> FileResponse:
    """Confirm the column mapping. Nothing is normalised until this is set."""
    mapping = SourceMapping(
        source_system=payload.source_system,
        static_values=payload.static_values,
        debit_column=payload.debit_column,
        credit_column=payload.credit_column,
        columns=[
            ColumnMapping(
                source_column=column.source_column,
                canonical_field=column.canonical_field,
                date_format=column.date_format,
                number_format=(
                    NumberFormat(column.number_format) if column.number_format else None
                ),
                negate=column.negate,
                confidence=1.0,
                rationale="confirmed by the user",
            )
            for column in payload.columns
        ],
    )
    try:
        row = IngestionService(context).set_mapping(file_id, mapping)
    except IngestionError as exc:
        raise _problem(exc) from exc
    return FileResponse.model_validate(row, from_attributes=True)


@router.post("/{file_id}/ingest", response_model=IngestResponse)
def ingest_file(
    context: Context,
    file_id: UUID,
    _: Annotated[object, Depends(require_permission(Permission.INGEST_FILE))] = None,
) -> IngestResponse:
    """Normalise and persist. Safe to call twice: it creates no duplicates."""
    try:
        result = IngestionService(context).ingest(file_id)
    except IngestionError as exc:
        raise _problem(exc) from exc

    return IngestResponse(
        file_id=result.file_id,
        transactions_created=result.transactions_created,
        duplicates_skipped=result.duplicates_skipped,
        rows_failed=result.rows_failed,
        quality_level=result.quality.level.value,
        quality_blocking=result.quality.blocking,
        findings=[
            {
                "code": finding.code,
                "level": finding.level.value,
                "message": finding.message,
                "affected_count": finding.affected_count,
                "sample_rows": list(finding.sample_rows),
            }
            for finding in result.quality.findings
        ],
    )
