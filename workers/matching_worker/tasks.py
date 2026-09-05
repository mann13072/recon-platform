"""Matching jobs: run a reconciliation, replay one, chase exceptions.

A reconciliation run is the long job in this platform, which is exactly why it
belongs on a queue: an HTTP request that takes two minutes will be retried by
something, and retrying a reconciliation over HTTP has no idempotency key
unless the caller supplies one.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any
from uuid import UUID

from workers.celery_app import TenantTask, celery_app
from workers.context import system_context
from packages.observability import registry

logger = logging.getLogger("recon.worker.matching")

# Above this, candidate generation is producing far more work per row than
# blocking should allow, which usually means a key stopped discriminating.
CANDIDATE_EXPLOSION_THRESHOLD = 25.0


@celery_app.task(base=TenantTask, bind=True, name="recon.matching.run_reconciliation")
def run_reconciliation(
    self: Any,
    *,
    tenant_id: str,
    reconciliation_id: str,
    correlation_id: str,
    period_start: str | None = None,
    period_end: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """Execute a reconciliation end to end.

    The idempotency key is passed through, so a retried job returns the original
    run rather than reconciling twice.
    """
    del self
    from apps.api.app.services.reconciliation import ReconciliationService

    with system_context(UUID(tenant_id), correlation_id=UUID(correlation_id)) as context:
        with registry.timer("matching_job_duration", tenant=tenant_id):
            outcome = ReconciliationService(context).start_run(
                UUID(reconciliation_id),
                period_start=date.fromisoformat(period_start) if period_start else None,
                period_end=date.fromisoformat(period_end) if period_end else None,
                idempotency_key=idempotency_key,
            )

        summary = outcome.summary
        registry.set(
            "auto_match_rate", summary.auto_match_rate, reconciliation=reconciliation_id
        )
        registry.set(
            "high_value_unmatched_count",
            float(summary.high_risk_exception_count),
            reconciliation=reconciliation_id,
        )

        return {
            "run_id": str(outcome.run.id),
            "status": outcome.run.status,
            "matches": outcome.matches_created,
            "exceptions": outcome.exceptions_created,
            "result_hash": outcome.result_hash,
            "replayed": outcome.replayed,
        }


@celery_app.task(base=TenantTask, bind=True, name="recon.matching.replay_run")
def replay_run(self: Any, *, tenant_id: str, run_id: str, correlation_id: str) -> dict:
    """Re-execute a run against its snapshot and compare result hashes.

    Scheduled periodically in production: a divergence means the engine has
    stopped being deterministic, and that must be found before an auditor finds
    it (spec sections 36 and 51).
    """
    del self, correlation_id
    from apps.api.app.services.reconciliation import ReconciliationService

    with system_context(UUID(tenant_id)) as context:
        service = ReconciliationService(context)
        outcome = service.rerun(UUID(run_id))
        original = service.runs.get(UUID(run_id))
        stored = original.result_hash if original else None
        reproducible = stored == outcome.result_hash

        if not reproducible:
            logger.error(
                "run did not reproduce",
                extra={
                    "run_id": run_id,
                    "stored": stored,
                    "replayed": outcome.result_hash,
                },
            )

        return {
            "run_id": run_id,
            "stored_result_hash": stored,
            "replayed_result_hash": outcome.result_hash,
            "reproducible": reproducible,
        }


@celery_app.task(base=TenantTask, bind=True, name="recon.matching.send_exception_reminder")
def send_exception_reminder(
    self: Any, *, tenant_id: str, correlation_id: str, run_id: str | None = None
) -> dict:
    """Find exceptions that have aged past their escalation policy."""
    del self, correlation_id
    from apps.api.app.services.exceptions import ExceptionService

    with system_context(UUID(tenant_id)) as context:
        service = ExceptionService(context)
        escalations = service.escalations(UUID(run_id) if run_id else None)
        aging = service.aging(UUID(run_id) if run_id else None)

        if aging["oldest_age_days"] is not None:
            registry.observe(
                "exception_aging", float(aging["oldest_age_days"]), tenant=tenant_id
            )

        return {
            "escalations": len(escalations),
            "overdue": aging["overdue_count"],
            "oldest_age_days": aging["oldest_age_days"],
        }
