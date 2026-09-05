"""Celery application and the job contract (spec section 62).

Every job must be idempotent, retryable, observable, tenant-scoped and carry a
correlation ID. :class:`TenantTask` supplies the last three so an individual job
cannot forget them, and idempotency is the job's own responsibility because only
it knows what "already done" means.

Redis and Celery are the MVP choice. Temporal for durable workflows and Kafka
for event volume are deliberately not here (spec section 4): neither is
justified before the throughput demands it.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import suppress
from typing import Any

from celery import Celery, Task
from celery.signals import task_failure, task_prerun

from apps.api.app.config import get_settings
from packages.observability import registry

logger = logging.getLogger("recon.worker")

settings = get_settings()

celery_app = Celery(
    "recon",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # A financial job that silently vanishes is worse than one that runs twice,
    # and every job here is idempotent, so acknowledge only after completion.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    task_time_limit=3600,
    task_soft_time_limit=3300,
    result_expires=7 * 24 * 3600,
    task_routes={
        "recon.ingestion.*": {"queue": "ingestion"},
        "recon.matching.*": {"queue": "matching"},
        "recon.connector.*": {"queue": "connectors"},
        "recon.ai.*": {"queue": "ai"},
        "recon.export.*": {"queue": "exports"},
        "recon.notify.*": {"queue": "notifications"},
    },
)


class TenantTask(Task):
    """Base task: tenant-scoped, correlated, retried with backoff.

    ``tenant_id`` is a required keyword on every task. A job that could run
    without one could read across tenants, so the contract is enforced here
    rather than trusted to each task.
    """

    autoretry_for = (ConnectionError, TimeoutError)
    retry_backoff = True
    retry_backoff_max = 600
    retry_jitter = True
    max_retries = 5

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        tenant_id = kwargs.get("tenant_id")
        if tenant_id is None:
            raise ValueError(
                f"task {self.name} was called without a tenant_id; every job "
                "must be scoped to a tenant"
            )

        correlation = kwargs.get("correlation_id") or str(uuid.uuid4())
        kwargs["correlation_id"] = correlation

        from packages.observability.tracing import correlation_id, span

        with correlation_id(uuid.UUID(str(correlation))), span(self.name, tenant=str(tenant_id)):
            return super().__call__(*args, **kwargs)


@task_prerun.connect
def _log_start(task_id: str, task: Task, **_: Any) -> None:
    logger.info("job started", extra={"task": task.name, "task_id": task_id})


@task_failure.connect
def _log_failure(task_id: str, exception: BaseException, sender: Task, **_: Any) -> None:
    logger.error(
        "job failed",
        extra={
            "task": getattr(sender, "name", "unknown"),
            "task_id": task_id,
            "error": f"{type(exception).__name__}: {exception}",
        },
    )
    with suppress(KeyError):  # pragma: no cover - metric name guard
        registry.increment("ingestion_failures", reason=type(exception).__name__)


# Importing the task modules registers them with the app.
celery_app.autodiscover_tasks(
    [
        "workers.ingestion_worker",
        "workers.matching_worker",
        "workers.connector_sync_worker",
        "workers.ai_worker",
        "workers.export_worker",
    ],
    related_name="tasks",
    force=True,
)

__all__ = ["TenantTask", "celery_app"]
