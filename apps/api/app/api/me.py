"""Who am I, and what am I allowed to do?

The frontend uses this to decide which actions to render. Rendering an action a
user cannot perform and then failing at the API is a poor experience, and
hiding one they can perform is worse.
"""

from __future__ import annotations

from apps.api.app.dependencies import Context
from fastapi import APIRouter

router = APIRouter(prefix="/me", tags=["me"])


@router.get("")
def whoami(context: Context) -> dict:
    principal = context.principal
    return {
        "user_id": str(principal.user_id) if principal.user_id else None,
        "email": principal.email,
        "tenant_id": str(principal.tenant_id),
        "actor_type": principal.actor_type.value,
        "roles": sorted(role.value for role in principal.roles),
        "permissions": sorted(p.value for p in principal.permissions()),
        "approval_limit": principal.approval_limit,
        "ai": {
            "enabled": context.settings.ai_enabled,
            "policy": context.ai_settings.policy.value,
            "provider": context.ai.name,
        },
    }
