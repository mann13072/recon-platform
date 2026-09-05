"""Audit trail, export packages and governed investigation.

Spec sections 35, 71 and 91.
"""

from __future__ import annotations

import io
import zipfile
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from apps.api.app.api.schemas import InvestigationRequest
from apps.api.app.dependencies import Context, require_permission
from apps.api.app.services.close import CloseError, CloseService
from apps.api.app.services.reconciliation import ReconciliationService
from packages.ai.investigation import gather_evidence, investigate
from packages.audit.events import verify_chain
from packages.controls.permissions import Permission
from packages.domain.models.reconciliation import RunSummary

router = APIRouter(tags=["audit"])


@router.get("/audit/events")
def list_audit_events(
    context: Context,
    limit: int = Query(200, le=2000),
    offset: int = 0,
    _: Annotated[object, Depends(require_permission(Permission.VIEW_AUDIT))] = None,
) -> dict[str, Any]:
    events = context.audit.events_for(context.tenant_id, limit=limit, offset=offset)
    return {
        "total": context.audit.count(context.tenant_id),
        "events": [event.model_dump(mode="json") for event in events],
    }


@router.get("/audit/verify")
def verify_audit_chain(
    context: Context,
    _: Annotated[object, Depends(require_permission(Permission.VIEW_AUDIT))] = None,
) -> dict[str, Any]:
    """Confirm the tenant's audit chain has not been tampered with.

    Any edit to a historical event breaks every hash after it, so this reports
    the exact index where the chain diverges.
    """
    events = context.audit.events_for(context.tenant_id)
    ok, index = verify_chain(events)
    return {
        "verified": ok,
        "event_count": len(events),
        "first_invalid_index": index,
        "first_invalid_event_id": (
            str(events[index].event_id) if index is not None and index < len(events) else None
        ),
    }


@router.get("/audit/entity/{entity_type}/{entity_id}")
def entity_history(
    context: Context,
    entity_type: str,
    entity_id: UUID,
    _: Annotated[object, Depends(require_permission(Permission.VIEW_AUDIT))] = None,
) -> list[dict[str, Any]]:
    """Everything that ever happened to one entity."""
    events = context.audit.events_for_entity(context.tenant_id, entity_type.upper(), entity_id)
    return [event.model_dump(mode="json") for event in events]


@router.get("/runs/{run_id}/audit-package")
def download_audit_package(
    context: Context,
    run_id: UUID,
    _: Annotated[object, Depends(require_permission(Permission.EXPORT_AUDIT_PACKAGE))] = None,
) -> Response:
    """The full evidence package as a zip (spec section 91)."""
    try:
        package = CloseService(context).audit_package(run_id)
    except CloseError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(package.files.items()):
            archive.writestr(name, content)

    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="audit-package-{run_id}.zip"'},
    )


@router.post("/runs/{run_id}/investigate")
def investigate_run(
    context: Context,
    run_id: UUID,
    payload: InvestigationRequest,
    _: Annotated[object, Depends(require_permission(Permission.VIEW_DASHBOARD))] = None,
) -> dict[str, Any]:
    """Answer a question about a run.

        Question -> governed query functions -> structured evidence -> explanation

    The model never sees the database and never writes SQL. It is handed a typed
    evidence bundle and asked to phrase it; with AI disabled the deterministic
    narrative is returned instead, and it is always included so the reader can
    check the wording against the numbers.
    """
    service = ReconciliationService(context)
    run = service.runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="No such run.")
    if not run.summary:
        raise HTTPException(status_code=409, detail="This run has not completed yet.")

    summary = RunSummary.model_validate(run.summary)
    evidence = gather_evidence(
        payload.question,
        difference=summary.difference,
        currency=summary.currency,
        exceptions=service.exceptions.list(run_id=run_id, limit=1000),
        unmatched=[],
        match_groups=service.matches.list_for_run(run_id, limit=1000),
    )
    answer = investigate(
        evidence,
        tenant_id=context.tenant_id,
        provider=context.ai,
        settings=context.ai_settings,
    )
    return answer.as_dict()
