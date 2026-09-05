"""Connector sync jobs (spec sections 9 and 89).

A sync that fails must leave the connection visibly unhealthy. The failure mode
this guards against is subtle: a connector that quietly returns nothing lets a
reconciliation run against half its data and report itself complete.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from packages.domain.dates import utc_now
from packages.domain.enums import AuditAction, ConnectorState
from packages.observability import registry
from workers.celery_app import TenantTask, celery_app
from workers.context import system_context

logger = logging.getLogger("recon.worker.connector")


@celery_app.task(base=TenantTask, bind=True, name="recon.connector.sync_connection")
def sync_connection(
    self: Any,
    *,
    tenant_id: str,
    connection_id: str,
    correlation_id: str,
    days: int = 7,
) -> dict:
    """Pull a window of transactions from one connection.

    The connector's own cursor makes this resumable, and the canonical identity
    constraint makes it idempotent, so an overlapping window is safe and is in
    fact preferred: a small overlap catches records the provider backdated.
    """
    del self
    from apps.api.app.infrastructure.models import ConnectionRow

    with system_context(UUID(tenant_id), correlation_id=UUID(correlation_id)) as context:
        connection = context.session.get(ConnectionRow, UUID(connection_id))
        if connection is None or connection.tenant_id != context.tenant_id:
            raise ValueError(f"connection {connection_id} does not exist")

        if connection.state == ConnectorState.DISABLED.value:
            return {"connection_id": connection_id, "skipped": "connection is disabled"}

        context.audit.record(
            context.audit_context(),
            AuditAction.SOURCE_SYNC_STARTED,
            "CONNECTION",
            connection.id,
            metadata={"window_days": days},
        )

        end = utc_now()
        start = end - timedelta(days=days)

        try:
            result = asyncio.run(_run_sync(context, connection, start, end))
        except Exception as exc:
            connection.state = ConnectorState.FAILED.value
            connection.state_detail = str(exc)[:1000]
            connection.last_attempt_at = utc_now()
            context.session.flush()
            context.audit.record(
                context.audit_context(),
                AuditAction.SOURCE_SYNC_FAILED,
                "CONNECTION",
                connection.id,
                reason=str(exc),
            )
            registry.set("connector_sync_success_rate", 0.0, connection=connection_id)
            raise

        connection.state = result["state"]
        connection.state_detail = result.get("detail", "")
        connection.last_attempt_at = utc_now()
        if result["state"] == ConnectorState.HEALTHY.value:
            connection.last_success_at = utc_now()
        context.session.flush()

        context.audit.record(
            context.audit_context(),
            AuditAction.SOURCE_SYNC_COMPLETED,
            "CONNECTION",
            connection.id,
            after={"state": connection.state, "fetched": result["fetched"]},
        )
        registry.set(
            "connector_sync_success_rate",
            1.0 if result["state"] == ConnectorState.HEALTHY.value else 0.0,
            connection=connection_id,
        )
        return {"connection_id": connection_id, **result}


async def _run_sync(context: Any, connection: Any, start: datetime, end: datetime) -> dict:
    """Build the connector for this connection and run one window.

    Only connectors that are actually implemented can be built. An unimplemented
    one raises here rather than returning an empty result, because an empty
    result would look like a healthy sync of a quiet account.
    """
    from packages.connectors.base import ConnectorContext, SyncCursor

    connector_type = connection.connector_type
    if connector_type != "stripe":
        raise NotImplementedError(
            f"the '{connector_type}' connector is not implemented yet. "
            "Returning no data would make this look like a healthy sync of a "
            "quiet account, so the job fails instead. See docs/connector-sdk.md."
        )

    from packages.connectors.stripe import StripeConnector

    credentials = _decrypt_credentials(context, connection)
    connector_context = ConnectorContext(
        tenant_id=context.tenant_id,
        connection_id=connection.id,
        source_system=connection.source_system,
        config=dict(connection.config or {}),
        cursor=SyncCursor.from_dict({"value": connection.cursor} if connection.cursor else None),
        credentials=credentials,
    )

    transport = _build_stripe_transport(credentials)
    connector = StripeConnector(connector_context, transport)
    account = (connection.config or {}).get("account_id", "default")
    result = await connector.sync(account, start, end)

    connection.cursor = result.cursor.value
    return {
        "fetched": result.fetched,
        "normalized": result.normalized,
        "skipped": result.skipped,
        "state": result.health.state.value,
        "detail": result.health.detail,
        "errors": result.errors[:5],
    }


def _decrypt_credentials(context: Any, connection: Any) -> dict[str, str]:
    from apps.api.app.infrastructure.models import ConnectorCredentialRow

    rows = (
        context.session.query(ConnectorCredentialRow)
        .filter(
            ConnectorCredentialRow.tenant_id == context.tenant_id,
            ConnectorCredentialRow.connection_id == connection.id,
        )
        .all()
    )
    cipher = context.cipher()
    return {row.kind: cipher.decrypt(row.ciphertext, context=str(connection.id)) for row in rows}


def _build_stripe_transport(credentials: dict[str, str]) -> Any:
    """A minimal httpx-backed transport.

    Injected rather than constructed inside the connector so the connector can
    be tested against recorded payloads.
    """
    import httpx

    from packages.connectors.base import ConnectorAuthError, RateLimitError

    token = credentials.get("access_token", "")

    class HttpxTransport:
        async def get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
            async with httpx.AsyncClient(base_url="https://api.stripe.com", timeout=30.0) as client:
                response = await client.get(
                    path, params=params, headers={"Authorization": f"Bearer {token}"}
                )
                if response.status_code == 401:
                    raise ConnectorAuthError("Stripe rejected the access token")
                if response.status_code == 429:
                    raise RateLimitError(
                        "Stripe rate limited the request",
                        retry_after_seconds=float(response.headers.get("retry-after", 60)),
                    )
                response.raise_for_status()
                return dict(response.json())

    return HttpxTransport()
