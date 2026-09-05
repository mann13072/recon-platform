"""Reconciliation definitions and runs (spec section 37)."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status

from apps.api.app.api.matches import to_match_response
from apps.api.app.api.schemas import (
    CloseCertificateResponse,
    CloseRunRequest,
    CreateReconciliationRequest,
    ExceptionResponse,
    MatchResponse,
    ReconciliationResponse,
    ReopenRunRequest,
    RunResponse,
    RunSummaryResponse,
    StartRunRequest,
)
from apps.api.app.dependencies import Context, require_permission
from apps.api.app.infrastructure.repositories import ConcurrencyConflict
from apps.api.app.services.close import CloseError, CloseService
from apps.api.app.services.reconciliation import (
    ReconciliationError,
    ReconciliationService,
)
from packages.controls.permissions import Permission
from packages.controls.segregation_of_duties import SoDViolation
from packages.domain.models.reconciliation import RunSummary

router = APIRouter(tags=["reconciliations"])


def _problem(exc: ReconciliationError | CloseError) -> HTTPException:
    code = {
        "not_found": status.HTTP_404_NOT_FOUND,
        "duplicate": status.HTTP_409_CONFLICT,
        "already_closed": status.HTTP_409_CONFLICT,
        "not_closed": status.HTTP_409_CONFLICT,
    }.get(exc.code, status.HTTP_422_UNPROCESSABLE_ENTITY)
    return HTTPException(status_code=code, detail=str(exc))


# ---------------------------------------------------------------------------
# Definitions
# ---------------------------------------------------------------------------


@router.post(
    "/reconciliations",
    response_model=ReconciliationResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_reconciliation(
    context: Context,
    payload: CreateReconciliationRequest,
    _: Annotated[object, Depends(require_permission(Permission.CREATE_RECONCILIATION))] = None,
) -> ReconciliationResponse:
    try:
        row = ReconciliationService(context).create(
            slug=payload.slug,
            name=payload.name,
            template=payload.template,
            config=payload.config,
            side_a_connection_id=payload.side_a_connection_id,
            side_b_connection_id=payload.side_b_connection_id,
            entity=payload.entity,
        )
    except ReconciliationError as exc:
        raise _problem(exc) from exc
    return ReconciliationResponse.model_validate(row, from_attributes=True)


@router.get("/reconciliations", response_model=list[ReconciliationResponse])
def list_reconciliations(
    context: Context,
    limit: int = Query(50, le=200),
    offset: int = 0,
    _: Annotated[object, Depends(require_permission(Permission.VIEW_DASHBOARD))] = None,
) -> list[ReconciliationResponse]:
    rows = ReconciliationService(context).definitions.list(limit=limit, offset=offset)
    return [ReconciliationResponse.model_validate(r, from_attributes=True) for r in rows]


@router.get("/reconciliations/{reconciliation_id}", response_model=ReconciliationResponse)
def get_reconciliation(
    context: Context,
    reconciliation_id: UUID,
    _: Annotated[object, Depends(require_permission(Permission.VIEW_DASHBOARD))] = None,
) -> ReconciliationResponse:
    row = ReconciliationService(context).definitions.get(reconciliation_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No such reconciliation.")
    return ReconciliationResponse.model_validate(row, from_attributes=True)


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


@router.post(
    "/reconciliations/{reconciliation_id}/runs",
    response_model=RunResponse,
    status_code=status.HTTP_201_CREATED,
)
def start_run(
    context: Context,
    reconciliation_id: UUID,
    payload: StartRunRequest,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    _: Annotated[object, Depends(require_permission(Permission.RUN_RECONCILIATION))] = None,
) -> RunResponse:
    """Run the reconciliation.

    Supply ``Idempotency-Key`` to make a retry safe: a repeated request returns
    the original run rather than reconciling twice (spec section 63).
    """
    try:
        outcome = ReconciliationService(context).start_run(
            reconciliation_id,
            period_start=payload.period_start,
            period_end=payload.period_end,
            idempotency_key=idempotency_key,
        )
    except ReconciliationError as exc:
        raise _problem(exc) from exc

    response = RunResponse.model_validate(outcome.run, from_attributes=True)
    return response.model_copy(update={"replayed": outcome.replayed})


@router.get("/runs/{run_id}", response_model=RunResponse)
def get_run(
    context: Context,
    run_id: UUID,
    _: Annotated[object, Depends(require_permission(Permission.VIEW_DASHBOARD))] = None,
) -> RunResponse:
    row = ReconciliationService(context).runs.get(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No such run.")
    return RunResponse.model_validate(row, from_attributes=True)


@router.get("/runs/{run_id}/summary", response_model=RunSummaryResponse)
def get_run_summary(
    context: Context,
    run_id: UUID,
    _: Annotated[object, Depends(require_permission(Permission.VIEW_DASHBOARD))] = None,
) -> RunSummaryResponse:
    """The dashboard numbers: are my accounts reconciled, and if not, why not?"""
    row = ReconciliationService(context).runs.get(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No such run.")
    if not row.summary:
        raise HTTPException(status_code=409, detail="This run has not completed yet.")
    summary = RunSummary.model_validate(row.summary)
    return RunSummaryResponse.model_validate(summary.model_dump())


@router.get("/runs/{run_id}/matches", response_model=list[MatchResponse])
def list_run_matches(
    context: Context,
    run_id: UUID,
    match_status: str | None = Query(None, alias="status"),
    decision: str | None = None,
    limit: int = Query(200, le=1000),
    offset: int = 0,
    _: Annotated[object, Depends(require_permission(Permission.VIEW_MATCHES))] = None,
) -> list[MatchResponse]:
    groups = ReconciliationService(context).matches.list_for_run(
        run_id, status=match_status, decision=decision, limit=limit, offset=offset
    )
    return [to_match_response(group) for group in groups]


@router.get("/runs/{run_id}/exceptions", response_model=list[ExceptionResponse])
def list_run_exceptions(
    context: Context,
    run_id: UUID,
    exception_status: str | None = Query(None, alias="status"),
    category: str | None = None,
    severity: str | None = None,
    limit: int = Query(100, le=1000),
    offset: int = 0,
    _: Annotated[object, Depends(require_permission(Permission.VIEW_EXCEPTIONS))] = None,
) -> list[ExceptionResponse]:
    records = ReconciliationService(context).exceptions.list(
        run_id=run_id,
        status=exception_status,
        category=category,
        severity=severity,
        limit=limit,
        offset=offset,
    )
    return [ExceptionResponse.model_validate(r.model_dump()) for r in records]


@router.get("/runs/{run_id}/close-preflight")
def close_preflight(
    context: Context,
    run_id: UUID,
    _: Annotated[object, Depends(require_permission(Permission.VIEW_DASHBOARD))] = None,
) -> dict[str, Any]:
    """What still stands between this run and being closed."""
    try:
        return CloseService(context).preflight(run_id)
    except CloseError as exc:
        raise _problem(exc) from exc


@router.post("/runs/{run_id}/close", response_model=CloseCertificateResponse)
def close_run(
    context: Context,
    run_id: UUID,
    payload: CloseRunRequest,
    _: Annotated[object, Depends(require_permission(Permission.CLOSE_RUN))] = None,
) -> CloseCertificateResponse:
    try:
        certificate = CloseService(context).close(run_id, expected_version=payload.expected_version)
    except CloseError as exc:
        raise _problem(exc) from exc
    except SoDViolation as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ConcurrencyConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return CloseCertificateResponse.model_validate(certificate.model_dump())


@router.post("/runs/{run_id}/reopen", response_model=RunResponse)
def reopen_run(
    context: Context,
    run_id: UUID,
    payload: ReopenRunRequest,
    _: Annotated[object, Depends(require_permission(Permission.REOPEN_RUN))] = None,
) -> RunResponse:
    try:
        row = CloseService(context).reopen(
            run_id, expected_version=payload.expected_version, reason=payload.reason
        )
    except CloseError as exc:
        raise _problem(exc) from exc
    except SoDViolation as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ConcurrencyConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return RunResponse.model_validate(row, from_attributes=True)


@router.post("/runs/{run_id}/replay")
def replay_run(
    context: Context,
    run_id: UUID,
    _: Annotated[object, Depends(require_permission(Permission.RUN_RECONCILIATION))] = None,
) -> dict[str, Any]:
    """Re-execute against the frozen snapshot and compare result hashes.

    This is the reproducibility guarantee made checkable in production
    (spec sections 36 and 51).
    """
    service = ReconciliationService(context)
    try:
        outcome = service.rerun(run_id)
    except ReconciliationError as exc:
        raise _problem(exc) from exc

    original = service.runs.get(run_id)
    stored_hash = original.result_hash if original else None
    return {
        "run_id": str(run_id),
        "stored_result_hash": stored_hash,
        "replayed_result_hash": outcome.result_hash,
        "reproducible": stored_hash == outcome.result_hash,
        "matches": outcome.matches_created,
    }
