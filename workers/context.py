"""Building a service context inside a worker.

An HTTP request gets its context from FastAPI dependencies. A background job has
no request, so it assembles the same context here - with one important
difference: the principal is a *system* actor, not a user.

That matters because the permission layer subtracts every privileged permission
from non-human actors. A job therefore cannot approve a match or close a
reconciliation, whatever its code tries to do.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from apps.api.app.config import Settings, get_settings
from apps.api.app.dependencies import RequestContext
from apps.api.app.infrastructure.audit_sink import DatabaseAuditLog
from apps.api.app.infrastructure.db import make_session_factory
from apps.api.app.infrastructure.models import TenantRow
from apps.api.app.infrastructure.storage import build_storage
from packages.ai.privacy import TenantAISettings
from packages.ai.provider import build_provider
from packages.controls.permissions import Principal
from packages.domain.enums import ActorType, AIPolicy

__all__ = ["system_context", "worker_session"]


@contextmanager
def worker_session() -> Iterator[Session]:
    session = make_session_factory()()
    try:
        yield session
        if session.is_active:
            session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def system_context(
    tenant_id: UUID,
    *,
    correlation_id: UUID | None = None,
    actor_type: ActorType = ActorType.SYSTEM,
    settings: Settings | None = None,
) -> Iterator[RequestContext]:
    """A context for a background job, acting as a non-human principal."""
    resolved = settings or get_settings()
    with worker_session() as session:
        tenant = session.get(TenantRow, tenant_id)
        if tenant is None:
            raise ValueError(f"tenant {tenant_id} does not exist")

        policy = AIPolicy(tenant.ai_policy)
        if not resolved.ai_enabled:
            policy = AIPolicy.AI_DISABLED

        yield RequestContext(
            session=session,
            # No roles: a system actor holds nothing beyond what a job needs,
            # and the permission layer strips privileged permissions from
            # non-human actors regardless.
            principal=Principal(
                tenant_id=tenant_id,
                actor_type=actor_type,
                user_id=None,
                email=None,
                roles=frozenset(),
            ),
            settings=resolved,
            storage=build_storage(resolved),
            audit=DatabaseAuditLog(session),
            ai=build_provider(
                enabled=resolved.ai_enabled,
                provider=resolved.ai_provider,
                model_name=resolved.ai_model,
            ),
            ai_settings=TenantAISettings(
                policy=policy,
                allowed_regions=frozenset(tenant.ai_allowed_regions or ["eu"]),
                provider_region=tenant.data_region,
                latency_budget_ms=resolved.ai_latency_budget_ms,
            ),
            correlation_id=correlation_id or uuid4(),
        )
