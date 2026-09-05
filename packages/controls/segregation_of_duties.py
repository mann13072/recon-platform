"""Segregation of duties and maker-checker (spec sections 34 and 57).

The rules enforced here:

* a preparer cannot approve their own high-risk adjustment;
* a connector administrator cannot alter a closed reconciliation;
* AI cannot approve anything;
* a period lock requires a privileged role;
* reopening a closed reconciliation creates an audit event;
* rule changes require a separate permission.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from packages.controls.permissions import Permission, Principal
from packages.domain.enums import ActorType, ReconciliationStatus, Role

__all__ = [
    "SoDViolation",
    "assert_can_approve_journal",
    "assert_can_approve_match",
    "assert_can_change_rules",
    "assert_can_close_run",
    "assert_can_modify_run",
    "assert_can_reopen_run",
]


class SoDViolation(PermissionError):
    """Raised when an action would breach segregation of duties."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ApprovalSubject:
    """What is being approved, and who created it."""

    subject_type: str
    subject_id: UUID
    created_by: UUID | None
    created_by_actor_type: ActorType
    amount: Decimal
    currency: str
    is_manual: bool = False
    is_override: bool = False


def _assert_human(principal: Principal, action: str) -> None:
    if not principal.is_human:
        raise SoDViolation(
            "NON_HUMAN_APPROVAL",
            f"{principal.describe()} may not {action}. Only an authenticated "
            "user can approve a financial action.",
        )


def assert_can_approve_match(
    principal: Principal,
    subject: ApprovalSubject,
    *,
    materiality_threshold: Decimal,
    maker_checker_required: bool = True,
) -> None:
    """Maker-checker for a match approval.

    A preparer may not approve their own work when the amount is at or above
    materiality, or when the match was manually created or overridden. Below
    materiality a single preparer approving a routine machine-proposed match is
    normal practice and is allowed.
    """
    _assert_human(principal, "approve a match")
    principal.require(Permission.APPROVE_MATCH)

    if not maker_checker_required:
        return

    same_person = principal.user_id is not None and principal.user_id == subject.created_by
    high_risk = (
        abs(subject.amount) >= materiality_threshold or subject.is_manual or subject.is_override
    )

    if same_person and high_risk:
        raise SoDViolation(
            "SELF_APPROVAL",
            f"{principal.describe()} created this {subject.subject_type} and may "
            f"not also approve it: {subject.currency} {abs(subject.amount):,.2f} "
            "is high-risk. A second reviewer is required.",
        )

    if principal.approval_limit is not None and abs(subject.amount) > Decimal(
        principal.approval_limit
    ):
        raise SoDViolation(
            "APPROVAL_LIMIT_EXCEEDED",
            f"{principal.describe()} has an approval limit of "
            f"{principal.approval_limit} and cannot approve "
            f"{subject.currency} {abs(subject.amount):,.2f}.",
        )


def assert_can_approve_journal(
    principal: Principal,
    subject: ApprovalSubject,
) -> None:
    """A journal proposal always needs a second person. No exceptions."""
    _assert_human(principal, "approve a journal entry")
    principal.require(Permission.APPROVE_JOURNAL)

    if principal.user_id is not None and principal.user_id == subject.created_by:
        raise SoDViolation(
            "SELF_APPROVAL",
            "The preparer of a journal proposal may never approve it.",
        )

    if principal.approval_limit is not None and abs(subject.amount) > Decimal(
        principal.approval_limit
    ):
        raise SoDViolation(
            "APPROVAL_LIMIT_EXCEEDED",
            f"{principal.describe()} cannot approve {subject.currency} {abs(subject.amount):,.2f}.",
        )


def assert_can_modify_run(principal: Principal, status: ReconciliationStatus) -> None:
    """Nothing may change inside a closed run until it is formally reopened."""
    if status is ReconciliationStatus.CLOSED:
        raise SoDViolation(
            "PERIOD_CLOSED",
            "This reconciliation is closed. It must be formally reopened, with a "
            "reason and an audit event, before anything in it can change.",
        )
    if (
        Role.INTEGRATION_ADMINISTRATOR in principal.roles
        and len(principal.roles) == 1
        and status in {ReconciliationStatus.READY_TO_CLOSE, ReconciliationStatus.REVIEW_REQUIRED}
    ):
        raise SoDViolation(
            "INTEGRATION_ADMIN_SCOPE",
            "A connector administrator manages integrations and may not alter a "
            "reconciliation that is under review or ready to close.",
        )


def assert_can_close_run(
    principal: Principal,
    *,
    unresolved_required_exceptions: int,
    pending_approvals: int,
    unexplained_difference: Decimal,
    max_unexplained_difference: Decimal,
    missing_evidence: int,
    blocking_quality_errors: int,
) -> None:
    """The close gate (spec section 58).

    Every condition is checked and all failures are reported together, so a
    controller fixes one list rather than discovering blockers one at a time.
    """
    _assert_human(principal, "close a reconciliation")
    principal.require(Permission.CLOSE_RUN)

    blockers: list[str] = []
    if unresolved_required_exceptions:
        blockers.append(f"{unresolved_required_exceptions} exception(s) still require resolution")
    if pending_approvals:
        blockers.append(f"{pending_approvals} approval(s) are still pending")
    if abs(unexplained_difference) > max_unexplained_difference:
        blockers.append(
            f"the unexplained difference of {unexplained_difference} exceeds the "
            f"configured threshold of {max_unexplained_difference}"
        )
    if missing_evidence:
        blockers.append(f"{missing_evidence} item(s) are missing required evidence")
    if blocking_quality_errors:
        blockers.append(f"{blocking_quality_errors} blocking data-quality error(s) are unresolved")

    if blockers:
        raise SoDViolation(
            "CLOSE_BLOCKED",
            "This reconciliation cannot be closed because " + "; ".join(blockers) + ".",
        )


def assert_can_reopen_run(principal: Principal, reason: str | None) -> None:
    """Reopening is privileged and must carry a reason."""
    _assert_human(principal, "reopen a reconciliation")
    principal.require(Permission.REOPEN_RUN)
    if not reason or not reason.strip():
        raise SoDViolation(
            "REASON_REQUIRED",
            "Reopening a closed reconciliation requires a written reason.",
        )


def assert_can_change_rules(principal: Principal, weakens_controls: bool) -> None:
    """Rule changes need their own permission.

    A change that weakens a control is allowed - customers own their risk
    appetite - but it is never allowed silently: the caller must record who
    made it (spec section 19).
    """
    principal.require(Permission.EDIT_RULES)
    if weakens_controls:
        principal.require(Permission.EDIT_THRESHOLDS)
