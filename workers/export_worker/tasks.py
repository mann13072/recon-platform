"""Export jobs: build the audit package (spec section 91).

The package is assembled entirely from stored state and written to object
storage, so a large export does not hold an HTTP connection open and the
resulting file has a stable, signed manifest.
"""

from __future__ import annotations

import io
import logging
import zipfile
from typing import Any
from uuid import UUID

from workers.celery_app import TenantTask, celery_app
from workers.context import system_context

logger = logging.getLogger("recon.worker.export")


@celery_app.task(base=TenantTask, bind=True, name="recon.export.build_audit_export")
def build_audit_export(self: Any, *, tenant_id: str, run_id: str, correlation_id: str) -> dict:
    """Assemble a run's audit package and store it.

    Idempotent by content: the key is derived from the archive's own hash, so
    re-running produces the same object rather than a second copy.
    """
    del self
    from apps.api.app.services.close import CloseService

    with system_context(UUID(tenant_id), correlation_id=UUID(correlation_id)) as context:
        package = CloseService(context).audit_package(UUID(run_id))

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            # Sorted, with a fixed timestamp, so the same run always produces a
            # byte-identical archive and therefore the same storage key.
            for name in sorted(package.files):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, package.files[name])

        data = buffer.getvalue()
        key, digest = context.storage.put_content(
            context.tenant_id,
            "exports",
            data,
            extension="zip",
            content_type="application/zip",
        )

        return {
            "run_id": run_id,
            "storage_key": key,
            "sha256": digest,
            "byte_size": len(data),
            "files": len(package.files),
            "download_url": context.storage.presigned_url(key),
        }
