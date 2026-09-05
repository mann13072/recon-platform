"""The exception state machine (spec section 31).

    OPEN -> TRIAGED -> ASSIGNED -> INVESTIGATING -> PROPOSED_RESOLUTION
         -> AWAITING_APPROVAL -> RESOLVED -> CLOSED

    side states: BLOCKED, ESCALATED, REOPENED

Two rules are enforced structurally rather than by convention:

* **AI may never transition an exception to CLOSED** (or to RESOLVED). The
  transition table is consulted together with the actor type, and every
  closing transition requires a human.
* every transition is legal or it raises. There is no "just set the status"
  path anywhere in the codebase.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from packages.controls.permissions import Permission, Principal
from packages.domain.dates import utc_now
from packages.domain.enums import ActorType, ExceptionStatus
from packages.domain.models.exceptions import ExceptionRecord

__all__ = [
    "ALLOWED_TRANSITIONS",
    "HUMAN_ONLY_STATES",
    "IllegalTransition",
    "ExceptionWorkflow",
    "can_transition",
]

S = ExceptionStatus

ALLOWED_TRANSITIONS: dict[ExceptionStatus, frozenset[ExceptionStatus]] = {
    S.OPEN: frozenset({S.TRIAGED, S.ASSIGNED, S.BLOCKED, S.ESCALATED}),
    S.TRIAGED: frozenset({S.ASSIGNED, S.INVESTIGATING, S.BLOCKED, S.ESCALATED}),
    S.ASSIGNED: frozenset(
        {S.INVESTIGATING, S.PROPOSED_RESOLUTION, S.BLOCKED, S.ESCALATED, S.TRIAGED}
    ),
    S.INVESTIGATING: frozenset({S.PROPOSED_RESOLUTION, S.BLOCKED, S.ESCALATED, S.ASSIGNED}),
    S.PROPOSED_RESOLUTION: frozenset(
        {S.AWAITING_APPROVAL, S.RESOLVED, S.INVESTIGATING, S.BLOCKED, S.ESCALATED}
    ),
    S.AWAITING_APPROVAL: frozenset({S.RESOLVED, S.PROPOSED_RESOLUTION, S.BLOCKED, S.ESCALATED}),
    S.RESOLVED: frozenset({S.CLOSED, S.REOPENED}),
    S.CLOSED: frozenset({S.REOPENED}),
    S.BLOCKED: frozenset({S.ASSIGNED, S.INVESTIGATING, S.ESCALATED, S.TRIAGED}),
    S.ESCALATED: frozenset({S.ASSIGNED, S.INVESTIGATING, S.PROPOSED_RESOLUTION, S.BLOCKED}),
    S.REOPENED: frozenset({S.ASSIGNED, S.INVESTIGATING, S.TRIAGED, S.ESCALATED}),
}

# States only an authenticated human may move an exception into.
HUMAN_ONLY_STATES: frozenset[ExceptionStatus] = frozenset(
    {S.RESOLVED, S.CLOSED, S.REOPENED, S.AWAITING_APPROVAL}
)

# Permission required to enter each state.
_REQUIRED_PERMISSION: dict[ExceptionStatus, Permission] = {
    S.TRIAGED: Permission.ASSIGN_EXCEPTION,
    S.ASSIGNED: Permission.ASSIGN_EXCEPTION,
    S.INVESTIGATING: Permission.COMMENT_EXCEPTION,
    S.PROPOSED_RESOLUTION: Permission.PROPOSE_RESOLUTION,
    S.AWAITING_APPROVAL: Permission.PROPOSE_RESOLUTION,
    S.RESOLVED: Permission.APPROVE_RESOLUTION,
    S.CLOSED: Permission.CLOSE_EXCEPTION,
    S.REOPENED: Permission.ASSIGN_EXCEPTION,
    S.BLOCKED: Permission.COMMENT_EXCEPTION,
    S.ESCALATED: Permission.COMMENT_EXCEPTION,
}


class IllegalTransition(ValueError):
    """Raised when a status change is not permitted."""

    def __init__(self, message: str, code: str = "ILLEGAL_TRANSITION") -> None:
        super().__init__(message)
        self.code = code


def can_transition(
    current: ExceptionStatus,
    target: ExceptionStatus,
    actor_type: ActorType = ActorType.USER,
) -> bool:
    """Whether a transition is structurally allowed for this actor type."""
    if target not in ALLOWED_TRANSITIONS.get(current, frozenset()):
        return False
    return not (target in HUMAN_ONLY_STATES and actor_type is not ActorType.USER)


@dataclass(slots=True)
class ExceptionWorkflow:
    """Applies transitions to exception records, with the controls attached."""

    def transition(
        self,
        exception: ExceptionRecord,
        target: ExceptionStatus,
        principal: Principal,
        *,
        reason: str | None = None,
        owner_user_id: UUID | None = None,
        resolution_code: str | None = None,
        proposed_resolution: str | None = None,
        expected_version: int | None = None,
    ) -> ExceptionRecord:
        """Move an exception to ``target``, or raise.

        ``expected_version`` gives callers optimistic locking (spec section 64):
        pass the version you read, and a concurrent change is reported rather
        than silently overwritten.
        """
        if expected_version is not None and expected_version != exception.version:
            raise IllegalTransition(
                f"This exception changed since you loaded it (version "
                f"{exception.version}, you have {expected_version}). Reload and "
                "review the change before retrying.",
                code="VERSION_CONFLICT",
            )

        if target is exception.status:
            raise IllegalTransition(f"The exception is already {target.value}.", code="NO_OP")

        if target not in ALLOWED_TRANSITIONS.get(exception.status, frozenset()):
            legal = ", ".join(
                sorted(s.value for s in ALLOWED_TRANSITIONS.get(exception.status, ()))
            )
            raise IllegalTransition(
                f"Cannot move an exception from {exception.status.value} to "
                f"{target.value}. Legal next states: {legal or 'none'}."
            )

        if target in HUMAN_ONLY_STATES and not principal.is_human:
            raise IllegalTransition(
                f"{principal.describe()} may not move an exception to "
                f"{target.value}. This transition requires an authenticated user.",
                code="HUMAN_REQUIRED",
            )

        required = _REQUIRED_PERMISSION.get(target)
        if required is not None:
            principal.require(required)

        if target is S.REOPENED and not (reason or "").strip():
            raise IllegalTransition(
                "Reopening an exception requires a written reason.",
                code="REASON_REQUIRED",
            )

        if target is S.RESOLVED and not (resolution_code or exception.resolution_code):
            raise IllegalTransition(
                "Resolving an exception requires a resolution code.",
                code="RESOLUTION_CODE_REQUIRED",
            )

        if target is S.ASSIGNED and owner_user_id is None and exception.owner_user_id is None:
            raise IllegalTransition(
                "Assigning an exception requires an owner.", code="OWNER_REQUIRED"
            )

        updates: dict[str, object] = {
            "status": target,
            "version": exception.version + 1,
        }
        if owner_user_id is not None:
            updates["owner_user_id"] = owner_user_id
        if resolution_code is not None:
            updates["resolution_code"] = resolution_code
        if proposed_resolution is not None:
            updates["proposed_resolution"] = proposed_resolution
            updates["proposed_by_actor_type"] = principal.actor_type.value
        if target is S.CLOSED:
            updates["closed_by"] = principal.user_id
            updates["closed_at"] = utc_now()
        if target is S.REOPENED:
            updates["closed_by"] = None
            updates["closed_at"] = None

        return exception.model_copy(update=updates)

    def apply_ai_suggestion(
        self,
        exception: ExceptionRecord,
        *,
        category: str,
        proposed_resolution: str,
        suggestion_id: UUID,
    ) -> ExceptionRecord:
        """Attach an AI suggestion without changing the exception's status.

        A suggestion is metadata. It never advances the workflow, which is the
        structural reason an AI response cannot resolve or close anything.
        """
        del category  # the classifier's category stays authoritative
        return exception.model_copy(
            update={
                "proposed_resolution": proposed_resolution,
                "proposed_by_actor_type": ActorType.AI_ASSISTANT.value,
                "ai_suggestion_id": suggestion_id,
                "version": exception.version + 1,
            }
        )
