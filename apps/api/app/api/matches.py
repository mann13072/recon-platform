"""Match review endpoints (spec section 37)."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from apps.api.app.api.schemas import (
    ApproveMatchRequest,
    ManualMatchRequest,
    MatchMemberResponse,
    MatchReasonResponse,
    MatchResponse,
    RejectMatchRequest,
    UnmatchRequest,
)
from apps.api.app.dependencies import Context, require_permission
from apps.api.app.infrastructure.repositories import ConcurrencyConflict
from apps.api.app.services.review import ReviewError, ReviewService
from packages.controls.permissions import Permission, PermissionDenied
from packages.controls.period_lock import PeriodLocked
from packages.controls.segregation_of_duties import SoDViolation
from packages.domain.models.matching import MatchGroup

router = APIRouter(prefix="/matches", tags=["matches"])

__all__ = ["router", "to_match_response"]


def to_match_response(group: MatchGroup) -> MatchResponse:
    return MatchResponse(
        id=group.id,
        run_id=group.run_id,
        cardinality=group.cardinality.value,
        status=group.status.value,
        decision=group.decision.value,
        confidence=group.confidence,
        score=group.score,
        currency=group.currency,
        total_amount=group.total_amount,
        rule_id=group.rule_id,
        rule_version=group.rule_version,
        rule_set_version=group.rule_set_version,
        model_version=group.model_version,
        engine_stage=group.engine_stage,
        competing_candidate_count=group.competing_candidate_count,
        members=[
            MatchMemberResponse(
                transaction_id=m.transaction_id,
                side=m.side.value,
                allocated_amount=m.allocated_amount,
            )
            for m in group.members
        ],
        reasons=[
            MatchReasonResponse(
                code=r.code, contribution=r.contribution, description=r.description
            )
            for r in group.reasons
        ],
        warnings=[{"code": w.code, "description": w.description} for w in group.warnings],
        approved_by=group.approved_by,
        approved_at=group.approved_at,
        override_reason=group.override_reason,
        version=group.version,
        explanation=group.metadata.get("explanation", ""),
    )


def _handle(exc: Exception) -> HTTPException:
    if isinstance(exc, ReviewError):
        code = {
            "not_found": status.HTTP_404_NOT_FOUND,
            "already_decided": status.HTTP_409_CONFLICT,
            "already_matched": status.HTTP_409_CONFLICT,
        }.get(exc.code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        return HTTPException(status_code=code, detail=str(exc))
    if isinstance(exc, PeriodLocked | SoDViolation):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, PermissionDenied):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    if isinstance(exc, ConcurrencyConflict):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    raise exc


@router.get("/{match_id}", response_model=MatchResponse)
def get_match(
    context: Context,
    match_id: UUID,
    _: Annotated[object, Depends(require_permission(Permission.VIEW_MATCHES))] = None,
) -> MatchResponse:
    group = ReviewService(context).matches.get(match_id)
    if group is None:
        raise HTTPException(status_code=404, detail="No such match.")
    return to_match_response(group)


@router.post("/{match_id}/approve", response_model=MatchResponse)
def approve_match(
    context: Context,
    match_id: UUID,
    payload: ApproveMatchRequest,
    _: Annotated[object, Depends(require_permission(Permission.APPROVE_MATCH))] = None,
) -> MatchResponse:
    """Approve a match. Maker-checker and materiality apply."""
    try:
        group = ReviewService(context).approve(
            match_id, expected_version=payload.expected_version, reason=payload.reason
        )
    except Exception as exc:
        raise _handle(exc) from exc
    return to_match_response(group)


@router.post("/{match_id}/reject", response_model=MatchResponse)
def reject_match(
    context: Context,
    match_id: UUID,
    payload: RejectMatchRequest,
    _: Annotated[object, Depends(require_permission(Permission.REJECT_MATCH))] = None,
) -> MatchResponse:
    try:
        group = ReviewService(context).reject(
            match_id, expected_version=payload.expected_version, reason=payload.reason
        )
    except Exception as exc:
        raise _handle(exc) from exc
    return to_match_response(group)


@router.post("/{match_id}/unmatch", response_model=MatchResponse)
def unmatch(
    context: Context,
    match_id: UUID,
    payload: UnmatchRequest,
    _: Annotated[object, Depends(require_permission(Permission.UNMATCH))] = None,
) -> MatchResponse:
    """Release an approved match. The group is retained, never deleted."""
    try:
        group = ReviewService(context).unmatch(
            match_id, expected_version=payload.expected_version, reason=payload.reason
        )
    except Exception as exc:
        raise _handle(exc) from exc
    return to_match_response(group)


@router.post("/manual", response_model=MatchResponse, status_code=status.HTTP_201_CREATED)
def create_manual_match(
    context: Context,
    payload: ManualMatchRequest,
    _: Annotated[object, Depends(require_permission(Permission.CREATE_MANUAL_MATCH))] = None,
) -> MatchResponse:
    """Match by hand. The result is PROPOSED and still needs a second person."""
    try:
        group = ReviewService(context).create_manual(
            run_id=payload.run_id,
            side_a_ids=payload.side_a_ids,
            side_b_ids=payload.side_b_ids,
            reason=payload.reason,
        )
    except Exception as exc:
        raise _handle(exc) from exc
    return to_match_response(group)
