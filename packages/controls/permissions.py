"""RBAC (spec section 34).

Permissions are explicit and enumerated. The critical rule, encoded here rather
than described in a document: **AI cannot approve anything.** The AI actor type
holds no approval permission at all, so there is no code path in which a model
response becomes an approved financial action.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from packages.domain.enums import ActorType, Role

__all__ = [
    "Permission",
    "PermissionDenied",
    "Principal",
    "ROLE_PERMISSIONS",
    "permissions_for",
    "require",
]


class Permission(StrEnum):
    # Read
    VIEW_DASHBOARD = "view:dashboard"
    VIEW_TRANSACTIONS = "view:transactions"
    VIEW_MATCHES = "view:matches"
    VIEW_EXCEPTIONS = "view:exceptions"
    VIEW_AUDIT = "view:audit"
    VIEW_SETTINGS = "view:settings"

    # Data
    UPLOAD_FILE = "data:upload"
    INGEST_FILE = "data:ingest"
    MANAGE_CONNECTIONS = "data:connections"
    SYNC_CONNECTION = "data:sync"

    # Reconciliation
    CREATE_RECONCILIATION = "recon:create"
    EDIT_RECONCILIATION = "recon:edit"
    RUN_RECONCILIATION = "recon:run"

    # Matching
    CREATE_MANUAL_MATCH = "match:create"
    APPROVE_MATCH = "match:approve"
    REJECT_MATCH = "match:reject"
    UNMATCH = "match:unmatch"
    OVERRIDE_MATCH = "match:override"

    # Exceptions
    ASSIGN_EXCEPTION = "exception:assign"
    COMMENT_EXCEPTION = "exception:comment"
    ADD_EVIDENCE = "exception:evidence"
    PROPOSE_RESOLUTION = "exception:propose"
    APPROVE_RESOLUTION = "exception:approve"
    CLOSE_EXCEPTION = "exception:close"

    # Accounting actions
    PROPOSE_JOURNAL = "journal:propose"
    APPROVE_JOURNAL = "journal:approve"

    # Close controls
    CLOSE_RUN = "run:close"
    REOPEN_RUN = "run:reopen"
    LOCK_PERIOD = "period:lock"
    UNLOCK_PERIOD = "period:unlock"

    # Configuration
    EDIT_RULES = "rules:edit"
    SIMULATE_RULES = "rules:simulate"
    EDIT_THRESHOLDS = "thresholds:edit"
    MANAGE_USERS = "admin:users"
    EXPORT_AUDIT_PACKAGE = "audit:export"

    # AI
    USE_AI_SUGGESTIONS = "ai:use"
    CONFIGURE_AI = "ai:configure"


_VIEWER = frozenset(
    {
        Permission.VIEW_DASHBOARD,
        Permission.VIEW_TRANSACTIONS,
        Permission.VIEW_MATCHES,
        Permission.VIEW_EXCEPTIONS,
    }
)

_PREPARER = _VIEWER | {
    Permission.UPLOAD_FILE,
    Permission.INGEST_FILE,
    Permission.RUN_RECONCILIATION,
    Permission.CREATE_MANUAL_MATCH,
    Permission.ASSIGN_EXCEPTION,
    Permission.COMMENT_EXCEPTION,
    Permission.ADD_EVIDENCE,
    Permission.PROPOSE_RESOLUTION,
    Permission.PROPOSE_JOURNAL,
    Permission.SIMULATE_RULES,
    Permission.USE_AI_SUGGESTIONS,
}

_REVIEWER = _PREPARER | {
    Permission.APPROVE_MATCH,
    Permission.REJECT_MATCH,
    Permission.UNMATCH,
    Permission.VIEW_AUDIT,
}

_APPROVER = _REVIEWER | {
    Permission.APPROVE_RESOLUTION,
    Permission.CLOSE_EXCEPTION,
    Permission.APPROVE_JOURNAL,
    Permission.OVERRIDE_MATCH,
}

_CONTROLLER = _APPROVER | {
    Permission.CREATE_RECONCILIATION,
    Permission.EDIT_RECONCILIATION,
    Permission.CLOSE_RUN,
    Permission.REOPEN_RUN,
    Permission.LOCK_PERIOD,
    Permission.UNLOCK_PERIOD,
    Permission.EDIT_RULES,
    Permission.EDIT_THRESHOLDS,
    Permission.EXPORT_AUDIT_PACKAGE,
    Permission.VIEW_SETTINGS,
}

# Administrators run the platform. They deliberately do NOT approve matches or
# journals: platform administration and financial approval are different duties
# (spec section 34).
_ADMINISTRATOR = _VIEWER | {
    Permission.VIEW_SETTINGS,
    Permission.VIEW_AUDIT,
    Permission.MANAGE_USERS,
    Permission.MANAGE_CONNECTIONS,
    Permission.CONFIGURE_AI,
    Permission.EDIT_RULES,
    Permission.SIMULATE_RULES,
    Permission.EXPORT_AUDIT_PACKAGE,
}

# Auditors read everything and change nothing.
_AUDITOR = _VIEWER | {
    Permission.VIEW_AUDIT,
    Permission.VIEW_SETTINGS,
    Permission.EXPORT_AUDIT_PACKAGE,
}

# Integration administrators manage connectors, and nothing financial. In
# particular they cannot touch a closed reconciliation.
_INTEGRATION_ADMINISTRATOR = _VIEWER | {
    Permission.MANAGE_CONNECTIONS,
    Permission.SYNC_CONNECTION,
    Permission.UPLOAD_FILE,
    Permission.INGEST_FILE,
}

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.VIEWER: frozenset(_VIEWER),
    Role.PREPARER: frozenset(_PREPARER),
    Role.REVIEWER: frozenset(_REVIEWER),
    Role.APPROVER: frozenset(_APPROVER),
    Role.CONTROLLER: frozenset(_CONTROLLER),
    Role.ADMINISTRATOR: frozenset(_ADMINISTRATOR),
    Role.AUDITOR: frozenset(_AUDITOR),
    Role.INTEGRATION_ADMINISTRATOR: frozenset(_INTEGRATION_ADMINISTRATOR),
}

# Permissions no non-human actor may ever hold, whatever roles are attached.
# This is the hard stop behind "AI cannot approve financial actions"
# (spec sections 34, 74 and 109).
FORBIDDEN_FOR_NON_HUMAN: frozenset[Permission] = frozenset(
    {
        Permission.APPROVE_MATCH,
        Permission.APPROVE_RESOLUTION,
        Permission.APPROVE_JOURNAL,
        Permission.OVERRIDE_MATCH,
        Permission.CLOSE_EXCEPTION,
        Permission.CLOSE_RUN,
        Permission.REOPEN_RUN,
        Permission.LOCK_PERIOD,
        Permission.UNLOCK_PERIOD,
        Permission.EDIT_RULES,
        Permission.EDIT_THRESHOLDS,
        Permission.MANAGE_USERS,
        Permission.UNMATCH,
    }
)


class PermissionDenied(PermissionError):
    """Raised when a principal lacks a required permission."""

    def __init__(self, permission: Permission, principal: Principal) -> None:
        super().__init__(
            f"{principal.describe()} does not hold permission '{permission.value}'"
        )
        self.permission = permission
        self.principal = principal


@dataclass(frozen=True, slots=True)
class Principal:
    """Whoever is making a request: a user, the system, a connector, or AI."""

    tenant_id: UUID
    actor_type: ActorType = ActorType.USER
    user_id: UUID | None = None
    email: str | None = None
    roles: frozenset[Role] = frozenset()
    approval_limit: int | None = None
    """Maximum absolute amount this principal may approve, in major units.
    ``None`` means no configured limit."""

    def describe(self) -> str:
        if self.actor_type is ActorType.USER:
            return f"user {self.email or self.user_id}"
        return f"{self.actor_type.value.lower()} actor"

    @property
    def is_human(self) -> bool:
        return self.actor_type is ActorType.USER

    def permissions(self) -> frozenset[Permission]:
        return permissions_for(self.roles, self.actor_type)

    def has(self, permission: Permission) -> bool:
        return permission in self.permissions()

    def require(self, permission: Permission) -> None:
        if not self.has(permission):
            raise PermissionDenied(permission, self)


def permissions_for(
    roles: frozenset[Role] | set[Role] | list[Role],
    actor_type: ActorType = ActorType.USER,
) -> frozenset[Permission]:
    """Resolve a set of roles to a permission set.

    Non-human actors have the forbidden set subtracted unconditionally, so
    misconfiguring a service account cannot grant approval authority.
    """
    granted: set[Permission] = set()
    for role in roles:
        granted |= ROLE_PERMISSIONS.get(role, frozenset())

    if actor_type is not ActorType.USER:
        granted -= FORBIDDEN_FOR_NON_HUMAN

    return frozenset(granted)


def require(principal: Principal, permission: Permission) -> None:
    """Module-level form, for call sites that read better this way."""
    principal.require(permission)
