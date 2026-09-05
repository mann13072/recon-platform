"""Exception workflow endpoints (spec section 37)."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status

from apps.api.app.api.schemas import (
    AssignExceptionRequest,
    CommentRequest,
    CommentResponse,
    EvidenceResponse,
    ExceptionResponse,
    ProposeResolutionRequest,
    TransitionExceptionRequest,
)
from apps.api.app.dependencies import Context, require_permission
from apps.api.app.services.exceptions import ExceptionService, ExceptionServiceError
from packages.controls.permissions import Permission, PermissionDenied
from packages.domain.enums import ExceptionStatus

router = APIRouter(prefix="/exceptions", tags=["exceptions"])


def _handle(exc: Exception) -> HTTPException:
    if isinstance(exc, ExceptionServiceError):
        code = {
            "not_found": status.HTTP_404_NOT_FOUND,
            "VERSION_CONFLICT": status.HTTP_409_CONFLICT,
            "NO_OP": status.HTTP_409_CONFLICT,
            "HUMAN_REQUIRED": status.HTTP_403_FORBIDDEN,
        }.get(exc.code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        return HTTPException(status_code=code, detail=str(exc))
    if isinstance(exc, PermissionDenied):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    raise exc


@router.get("", response_model=list[ExceptionResponse])
def list_exceptions(
    context: Context,
    run_id: UUID | None = None,
    exception_status: str | None = Query(None, alias="status"),
    category: str | None = None,
    severity: str | None = None,
    owner_user_id: UUID | None = None,
    limit: int = Query(100, le=1000),
    offset: int = 0,
    _: Annotated[object, Depends(require_permission(Permission.VIEW_EXCEPTIONS))] = None,
) -> list[ExceptionResponse]:
    records = ExceptionService(context).repository.list(
        run_id=run_id,
        status=exception_status,
        category=category,
        severity=severity,
        owner_user_id=owner_user_id,
        limit=limit,
        offset=offset,
    )
    return [ExceptionResponse.model_validate(r.model_dump()) for r in records]


@router.get("/aging")
def exception_aging(
    context: Context,
    run_id: UUID | None = None,
    _: Annotated[object, Depends(require_permission(Permission.VIEW_EXCEPTIONS))] = None,
) -> dict:
    return ExceptionService(context).aging(run_id)


@router.get("/escalations")
def exception_escalations(
    context: Context,
    run_id: UUID | None = None,
    _: Annotated[object, Depends(require_permission(Permission.VIEW_EXCEPTIONS))] = None,
) -> list[dict]:
    return ExceptionService(context).escalations(run_id)


@router.get("/{exception_id}", response_model=ExceptionResponse)
def get_exception(
    context: Context,
    exception_id: UUID,
    _: Annotated[object, Depends(require_permission(Permission.VIEW_EXCEPTIONS))] = None,
) -> ExceptionResponse:
    try:
        record = ExceptionService(context).get(exception_id)
    except Exception as exc:
        raise _handle(exc) from exc
    return ExceptionResponse.model_validate(record.model_dump())


@router.post("/{exception_id}/assign", response_model=ExceptionResponse)
def assign_exception(
    context: Context,
    exception_id: UUID,
    payload: AssignExceptionRequest,
    _: Annotated[object, Depends(require_permission(Permission.ASSIGN_EXCEPTION))] = None,
) -> ExceptionResponse:
    service = ExceptionService(context)
    try:
        record = service.get(exception_id)
        if record.status is ExceptionStatus.OPEN:
            record = service.transition(
                exception_id,
                ExceptionStatus.TRIAGED,
                expected_version=payload.expected_version,
            )
        record = service.transition(
            exception_id,
            ExceptionStatus.ASSIGNED,
            owner_user_id=payload.owner_user_id,
        )
    except Exception as exc:
        raise _handle(exc) from exc
    return ExceptionResponse.model_validate(record.model_dump())


@router.post("/{exception_id}/transition", response_model=ExceptionResponse)
def transition_exception(
    context: Context,
    exception_id: UUID,
    payload: TransitionExceptionRequest,
    _: Annotated[object, Depends(require_permission(Permission.COMMENT_EXCEPTION))] = None,
) -> ExceptionResponse:
    """Move an exception through the state machine.

    Illegal transitions are refused with the legal next states listed, and
    RESOLVED/CLOSED require an authenticated human.
    """
    try:
        record = ExceptionService(context).transition(
            exception_id,
            ExceptionStatus(payload.status),
            expected_version=payload.expected_version,
            reason=payload.reason,
            owner_user_id=payload.owner_user_id,
            resolution_code=payload.resolution_code,
            proposed_resolution=payload.proposed_resolution,
        )
    except Exception as exc:
        raise _handle(exc) from exc
    return ExceptionResponse.model_validate(record.model_dump())


@router.post("/{exception_id}/propose-resolution", response_model=ExceptionResponse)
def propose_resolution(
    context: Context,
    exception_id: UUID,
    payload: ProposeResolutionRequest,
    _: Annotated[object, Depends(require_permission(Permission.PROPOSE_RESOLUTION))] = None,
) -> ExceptionResponse:
    try:
        record = ExceptionService(context).transition(
            exception_id,
            ExceptionStatus.PROPOSED_RESOLUTION,
            expected_version=payload.expected_version,
            proposed_resolution=payload.proposed_resolution,
            resolution_code=payload.resolution_code,
        )
    except Exception as exc:
        raise _handle(exc) from exc
    return ExceptionResponse.model_validate(record.model_dump())


@router.post("/{exception_id}/approve-resolution", response_model=ExceptionResponse)
def approve_resolution(
    context: Context,
    exception_id: UUID,
    payload: ProposeResolutionRequest,
    _: Annotated[object, Depends(require_permission(Permission.APPROVE_RESOLUTION))] = None,
) -> ExceptionResponse:
    try:
        record = ExceptionService(context).transition(
            exception_id,
            ExceptionStatus.RESOLVED,
            expected_version=payload.expected_version,
            resolution_code=payload.resolution_code,
            reason=payload.proposed_resolution,
        )
    except Exception as exc:
        raise _handle(exc) from exc
    return ExceptionResponse.model_validate(record.model_dump())


@router.post("/{exception_id}/close", response_model=ExceptionResponse)
def close_exception(
    context: Context,
    exception_id: UUID,
    _: Annotated[object, Depends(require_permission(Permission.CLOSE_EXCEPTION))] = None,
) -> ExceptionResponse:
    """Close a resolved exception. Only an authenticated human may do this."""
    try:
        record = ExceptionService(context).transition(
            exception_id, ExceptionStatus.CLOSED
        )
    except Exception as exc:
        raise _handle(exc) from exc
    return ExceptionResponse.model_validate(record.model_dump())


@router.post(
    "/{exception_id}/comment",
    response_model=CommentResponse,
    status_code=status.HTTP_201_CREATED,
)
def comment(
    context: Context,
    exception_id: UUID,
    payload: CommentRequest,
    _: Annotated[object, Depends(require_permission(Permission.COMMENT_EXCEPTION))] = None,
) -> CommentResponse:
    try:
        row = ExceptionService(context).comment(exception_id, payload.body)
    except Exception as exc:
        raise _handle(exc) from exc
    return CommentResponse.model_validate(row, from_attributes=True)


@router.get("/{exception_id}/comments", response_model=list[CommentResponse])
def list_comments(
    context: Context,
    exception_id: UUID,
    _: Annotated[object, Depends(require_permission(Permission.VIEW_EXCEPTIONS))] = None,
) -> list[CommentResponse]:
    rows = ExceptionService(context).comments(exception_id)
    return [CommentResponse.model_validate(r, from_attributes=True) for r in rows]


@router.post(
    "/{exception_id}/evidence",
    response_model=EvidenceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_evidence(
    context: Context,
    exception_id: UUID,
    file: Annotated[UploadFile, File()],
    _: Annotated[object, Depends(require_permission(Permission.ADD_EVIDENCE))] = None,
) -> EvidenceResponse:
    data = await file.read()
    try:
        evidence = ExceptionService(context).add_evidence(
            exception_id,
            filename=file.filename or "evidence",
            mime_type=file.content_type or "application/octet-stream",
            data=data,
        )
    except Exception as exc:
        raise _handle(exc) from exc
    return EvidenceResponse.model_validate(evidence.model_dump())


@router.get("/{exception_id}/evidence", response_model=list[EvidenceResponse])
def list_evidence(
    context: Context,
    exception_id: UUID,
    _: Annotated[object, Depends(require_permission(Permission.VIEW_EXCEPTIONS))] = None,
) -> list[EvidenceResponse]:
    rows = ExceptionService(context).evidence_for(exception_id)
    return [
        EvidenceResponse(
            id=row.id,
            exception_id=row.exception_id,
            filename=row.filename,
            mime_type=row.mime_type,
            byte_size=row.byte_size,
            sha256=row.sha256,
            kind=row.kind,
            uploaded_by=row.uploaded_by,
            uploaded_at=row.uploaded_at,
            download_url=context.storage.presigned_url(row.storage_key),
        )
        for row in rows
    ]


@router.post("/{exception_id}/ai-classification")
def request_ai_classification(
    context: Context,
    exception_id: UUID,
    _: Annotated[object, Depends(require_permission(Permission.USE_AI_SUGGESTIONS))] = None,
) -> dict:
    """Ask the assistant for an opinion.

    The response is advisory: the exception's category and status are unchanged
    whatever the model says.
    """
    try:
        return ExceptionService(context).request_ai_classification(exception_id)
    except Exception as exc:
        raise _handle(exc) from exc


@router.post("/{exception_id}/ai-decision")
def record_ai_decision(
    context: Context,
    exception_id: UUID,
    accepted: bool,
    _: Annotated[object, Depends(require_permission(Permission.USE_AI_SUGGESTIONS))] = None,
) -> dict:
    """Record whether the reviewer took the suggestion (spec section 46)."""
    try:
        ExceptionService(context).record_ai_decision(exception_id, accepted=accepted)
    except Exception as exc:
        raise _handle(exc) from exc
    return {"recorded": True, "accepted": accepted}
