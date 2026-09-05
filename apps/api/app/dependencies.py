"""FastAPI dependencies: session, principal, repositories, services.

The important one is :func:`get_principal`. Every route that touches tenant data
depends on it, so there is no way to reach a repository without a verified
tenant context.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from apps.api.app.config import Settings, get_settings
from apps.api.app.infrastructure.audit_sink import DatabaseAuditLog
from apps.api.app.infrastructure.auth import (
    AuthError,
    TokenVerifier,
    principal_from_claims,
)
from apps.api.app.infrastructure.crypto import CredentialCipher, EnvKeyProvider
from apps.api.app.infrastructure.db import make_session_factory
from apps.api.app.infrastructure.models import TenantRow, UserRoleRow, UserRow
from apps.api.app.infrastructure.storage import ObjectStorage, build_storage
from packages.ai.privacy import TenantAISettings
from packages.ai.provider import AIProvider, build_provider
from packages.audit.logger import AuditContext
from packages.controls.permissions import Permission, PermissionDenied, Principal
from packages.domain.enums import AIPolicy, ActorType, Role

__all__ = [
    "CurrentPrincipal",
    "DbSession",
    "RequestContext",
    "get_ai_provider",
    "get_context",
    "get_principal",
    "get_session",
    "get_storage",
    "require_permission",
]


def get_session() -> Iterator[Session]:
    """One session per request, committed on success."""
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


DbSession = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_principal(
    request: Request,
    session: DbSession,
    settings: SettingsDep,
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    """Verify the bearer token and resolve it to a tenant-scoped principal.

    The user row is created on first sight (just-in-time provisioning) so that
    approvals and audit events can reference a stable internal ID, but roles
    always come from the token, never from the local row: the identity provider
    is the authority on who holds what.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A bearer token is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = authorization.split(" ", 1)[1].strip()
    try:
        claims = TokenVerifier(settings).verify(token)
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    tenant_raw = claims.get("https://recon-platform.example/tenant_id") or claims.get(
        "tenant_id"
    )
    subject = str(claims.get("sub"))

    tenant = session.get(TenantRow, UUID(str(tenant_raw))) if tenant_raw else None
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The token's tenant does not exist on this deployment.",
        )

    user = (
        session.query(UserRow)
        .filter(UserRow.tenant_id == tenant.id, UserRow.external_subject == subject)
        .one_or_none()
    )
    if user is None:
        user = UserRow(
            id=uuid4(),
            tenant_id=tenant.id,
            external_subject=subject,
            email=str(claims.get("email") or f"{subject}@unknown.invalid"),
            display_name=str(claims.get("name") or "") or None,
        )
        session.add(user)
        session.flush()
    elif not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This user has been deactivated.",
        )

    try:
        principal = principal_from_claims(claims, user_id=user.id)
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from exc

    if principal.tenant_id != tenant.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Tenant mismatch."
        )

    # Keep the local role projection in step, so an operator reading the
    # database can see who holds what without decoding tokens.
    _sync_roles(session, user, principal.roles)

    if user.approval_limit is not None and principal.approval_limit is None:
        principal = Principal(
            tenant_id=principal.tenant_id,
            actor_type=principal.actor_type,
            user_id=principal.user_id,
            email=principal.email,
            roles=principal.roles,
            approval_limit=int(user.approval_limit),
        )

    request.state.principal = principal
    return principal


def _sync_roles(session: Session, user: UserRow, roles: frozenset[Role]) -> None:
    current = {row.role for row in user.roles}
    wanted = {role.value for role in roles}
    if current == wanted:
        return
    for row in list(user.roles):
        if row.role not in wanted:
            session.delete(row)
    for role in wanted - current:
        session.add(
            UserRoleRow(
                id=uuid4(), tenant_id=user.tenant_id, user_id=user.id, role=role
            )
        )
    session.flush()


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]


def require_permission(permission: Permission):  # type: ignore[no-untyped-def]
    """Route dependency factory enforcing one permission.

    A denial is logged as an audit event: an attempt to exceed authority is
    itself something an auditor wants to see (spec section 60).
    """

    def dependency(
        principal: CurrentPrincipal, session: DbSession
    ) -> Principal:
        try:
            principal.require(permission)
        except PermissionDenied as exc:
            from packages.domain.enums import AuditAction

            DatabaseAuditLog(session).record(
                AuditContext(
                    tenant_id=principal.tenant_id,
                    actor_type=principal.actor_type,
                    actor_id=principal.user_id,
                    actor_label=principal.email,
                ),
                AuditAction.PERMISSION_DENIED,
                "PERMISSION",
                None,
                reason=str(exc),
                metadata={"permission": permission.value},
            )
            session.commit()
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
            ) from exc
        return principal

    return dependency


@dataclass(slots=True)
class RequestContext:
    """Everything a service needs, assembled once per request."""

    session: Session
    principal: Principal
    settings: Settings
    storage: ObjectStorage
    audit: DatabaseAuditLog
    ai: AIProvider
    ai_settings: TenantAISettings
    correlation_id: UUID

    @property
    def tenant_id(self) -> UUID:
        return self.principal.tenant_id

    def audit_context(self, actor_type: ActorType | None = None) -> AuditContext:
        return AuditContext(
            tenant_id=self.tenant_id,
            actor_type=actor_type or self.principal.actor_type,
            actor_id=self.principal.user_id,
            actor_label=self.principal.email,
            correlation_id=self.correlation_id,
        )

    def cipher(self) -> CredentialCipher:
        return CredentialCipher(
            EnvKeyProvider(self.settings.credential_encryption_key)
        )


def get_storage(settings: SettingsDep) -> ObjectStorage:
    return build_storage(settings)


def get_ai_provider(settings: SettingsDep) -> AIProvider:
    return build_provider(
        enabled=settings.ai_enabled,
        provider=settings.ai_provider,
        model_name=settings.ai_model,
    )


def get_context(
    request: Request,
    session: DbSession,
    principal: CurrentPrincipal,
    settings: SettingsDep,
    storage: Annotated[ObjectStorage, Depends(get_storage)],
    ai: Annotated[AIProvider, Depends(get_ai_provider)],
) -> RequestContext:
    tenant = session.get(TenantRow, principal.tenant_id)
    policy = AIPolicy(tenant.ai_policy) if tenant else AIPolicy.AI_DISABLED
    if not settings.ai_enabled:
        policy = AIPolicy.AI_DISABLED

    ai_settings = TenantAISettings(
        policy=policy,
        allowed_regions=frozenset(tenant.ai_allowed_regions or ["eu"]) if tenant else frozenset({"eu"}),
        provider_region=tenant.data_region if tenant else "eu",
        latency_budget_ms=settings.ai_latency_budget_ms,
    )

    correlation = getattr(request.state, "correlation_id", None) or uuid4()
    return RequestContext(
        session=session,
        principal=principal,
        settings=settings,
        storage=storage,
        audit=DatabaseAuditLog(session),
        ai=ai,
        ai_settings=ai_settings,
        correlation_id=correlation,
    )


Context = Annotated[RequestContext, Depends(get_context)]
