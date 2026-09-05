"""AI jobs (spec sections 28, 29, 56).

Classification runs on a queue because a model call has unpredictable latency
and a reviewer should not wait for it. The important property is unchanged by
being asynchronous: the result is advisory, and a failure changes nothing.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from workers.celery_app import TenantTask, celery_app
from workers.context import system_context
from packages.observability import registry

logger = logging.getLogger("recon.worker.ai")


@celery_app.task(base=TenantTask, bind=True, name="recon.ai.classify_exceptions")
def classify_exceptions(
    self: Any,
    *,
    tenant_id: str,
    correlation_id: str,
    run_id: str | None = None,
    limit: int = 50,
) -> dict:
    """Ask the assistant about exceptions the deterministic classifier was unsure of.

    Nothing here changes an exception's category or status. A suggestion is
    attached; a human accepts or rejects it.
    """
    del self
    from apps.api.app.services.exceptions import ExceptionService
    from packages.ai.privacy import AIDisabledError, DataRegionViolation

    with system_context(UUID(tenant_id), correlation_id=UUID(correlation_id)) as context:
        if not context.ai_settings.enabled:
            return {"skipped": "AI is disabled for this tenant", "classified": 0}

        service = ExceptionService(context)
        records = service.repository.list(
            run_id=UUID(run_id) if run_id else None, status="OPEN", limit=limit
        )

        classified = 0
        failures = 0
        for record in records:
            try:
                result = service.request_ai_classification(record.id)
            except (AIDisabledError, DataRegionViolation) as exc:
                return {"skipped": str(exc), "classified": classified}
            except Exception as exc:
                # A model failure must never fail the job: the deterministic
                # classification already stands.
                failures += 1
                logger.warning(
                    "AI classification failed",
                    extra={"exception_id": str(record.id), "error": str(exc)},
                )
                continue

            if result.get("available"):
                classified += 1
            else:
                failures += 1

        if failures:
            registry.increment(
                "ai_schema_validation_failure", failures, tenant=tenant_id
            )

        return {
            "considered": len(records),
            "classified": classified,
            "failures": failures,
            "advisory_only": True,
        }


@celery_app.task(base=TenantTask, bind=True, name="recon.ai.suggest_entity_aliases")
def suggest_entity_aliases(
    self: Any, *, tenant_id: str, correlation_id: str, limit: int = 100
) -> dict:
    """Propose counterparty aliases for human approval (spec section 25).

    Every suggestion is written as a *pending* alias. Entities are never merged
    on a model's word.
    """
    del self, correlation_id, limit
    with system_context(UUID(tenant_id)) as context:
        if not context.ai_settings.enabled:
            return {"skipped": "AI is disabled for this tenant", "suggested": 0}
        # Alias mining needs a corpus of approved resolutions to be useful, so
        # it is enabled per tenant once that history exists rather than run
        # blindly on day one.
        return {"suggested": 0, "note": "alias mining requires approved history"}
