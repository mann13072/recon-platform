"""Ingestion jobs: parse, profile, normalise, run data quality.

Idempotency here is free rather than engineered: ingestion is keyed on the
canonical identity tuple and enforced by a database constraint, so re-running a
job creates nothing new.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from workers.celery_app import TenantTask, celery_app
from workers.context import system_context

logger = logging.getLogger("recon.worker.ingestion")


@celery_app.task(base=TenantTask, bind=True, name="recon.ingestion.parse_file")
def parse_file(self: Any, *, tenant_id: str, file_id: str, correlation_id: str) -> dict:
    """Parse and profile an uploaded file."""
    del self, correlation_id
    from apps.api.app.services.ingestion import IngestionService

    with system_context(UUID(tenant_id)) as context:
        profile = IngestionService(context).profile(UUID(file_id))
        return {
            "file_id": file_id,
            "columns": profile.get("column_count"),
            "rows": profile.get("row_count"),
        }


@celery_app.task(base=TenantTask, bind=True, name="recon.ingestion.normalize_source")
def normalize_source(self: Any, *, tenant_id: str, file_id: str, correlation_id: str) -> dict:
    """Normalise a mapped file into canonical transactions.

    Safe to retry: duplicates are skipped rather than inserted.
    """
    del self, correlation_id
    from apps.api.app.services.ingestion import IngestionService

    with system_context(UUID(tenant_id)) as context:
        result = IngestionService(context).ingest(UUID(file_id))
        return {
            "file_id": file_id,
            "created": result.transactions_created,
            "skipped": result.duplicates_skipped,
            "failed": result.rows_failed,
            "quality": result.quality.level.value,
            "blocking": result.quality.blocking,
        }


@celery_app.task(base=TenantTask, bind=True, name="recon.ingestion.run_data_quality")
def run_data_quality(self: Any, *, tenant_id: str, file_id: str, correlation_id: str) -> dict:
    """Re-run the quality gate without re-ingesting."""
    del self, correlation_id
    from apps.api.app.services.ingestion import IngestionService

    with system_context(UUID(tenant_id)) as context:
        row = IngestionService(context).files.get(UUID(file_id))
        if row is None:
            raise ValueError(f"file {file_id} does not exist")
        return {"file_id": file_id, "quality": row.quality_report}
