"""Approval workflow and journal validation (spec sections 45 and 57)."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from uuid import UUID, uuid4

from packages.controls.permissions import Principal
from packages.controls.segregation_of_duties import (
    ApprovalSubject,
    SoDViolation,
    assert_can_approve_journal,
    assert_can_approve_match,
)
from packages.domain.dates import utc_now
from packages.domain.models.exceptions import (
    AccountingActionProposal,
    Approval,
    JournalLine,
)
from packages.domain.money import quantize

__all__ = [
    "UnbalancedJournalError",
    "approve_journal",
    "approve_match",
    "request_approval",
    "validate_journal",
]


class UnbalancedJournalError(ValueError):
    """Raised when a journal entry's debits do not equal its credits."""


def validate_journal(lines: Sequence[JournalLine]) -> None:
    """Debits must equal credits. This is arithmetic, never a model's opinion.

    AI may suggest the narrative or the account classification; the balance
    check is deterministic and non-negotiable (spec section 45).
    """
    if not lines:
        raise UnbalancedJournalError("Journal entry has no lines.")

    currencies = {line.currency for line in lines}
    if len(currencies) != 1:
        raise UnbalancedJournalError(
            "Journal entry mixes currencies: " + ", ".join(sorted(currencies))
        )
    currency = currencies.pop()

    debits = sum((line.amount for line in lines if line.side == "debit"), Decimal("0"))
    credits = sum((line.amount for line in lines if line.side == "credit"), Decimal("0"))

    if quantize(debits, currency) != quantize(credits, currency):
        raise UnbalancedJournalError(
            f"Journal entry is not balanced: debits {debits} != credits {credits}."
        )

    if debits == 0:
        raise UnbalancedJournalError("Journal entry has a zero total.")


def request_approval(
    *,
    tenant_id: UUID,
    subject_type: str,
    subject_id: UUID,
    requested_by: UUID,
    required_role: str,
    reason: str | None = None,
) -> Approval:
    """Create a pending approval request."""
    return Approval(
        id=uuid4(),
        tenant_id=tenant_id,
        subject_type=subject_type,
        subject_id=subject_id,
        requested_by=requested_by,
        requested_at=utc_now(),
        required_role=required_role,
        reason=reason,
    )


def approve_match(
    principal: Principal,
    approval: Approval,
    subject: ApprovalSubject,
    *,
    materiality_threshold: Decimal,
    maker_checker_required: bool = True,
    reason: str | None = None,
) -> Approval:
    """Record an approval decision on a match, after the SoD checks pass."""
    assert_can_approve_match(
        principal,
        subject,
        materiality_threshold=materiality_threshold,
        maker_checker_required=maker_checker_required,
    )
    if approval.decision is not None:
        raise SoDViolation(
            "ALREADY_DECIDED",
            f"This approval was already {approval.decision.lower()}.",
        )
    return approval.model_copy(
        update={
            "approver_id": principal.user_id,
            "decided_at": utc_now(),
            "decision": "APPROVED",
            "reason": reason or approval.reason,
        }
    )


def approve_journal(
    principal: Principal,
    proposal: AccountingActionProposal,
    *,
    reason: str | None = None,
) -> AccountingActionProposal:
    """Approve a journal proposal.

    The balance check runs again here even though it ran at proposal time: the
    approval is the point at which the entry becomes real, and re-validating
    costs nothing.
    """
    validate_journal(proposal.lines)

    total = sum(
        (line.amount for line in proposal.lines if line.side == "debit"),
        Decimal("0"),
    )
    assert_can_approve_journal(
        principal,
        ApprovalSubject(
            subject_type="accounting_action_proposal",
            subject_id=proposal.id,
            created_by=proposal.preparer_id,
            created_by_actor_type=principal.actor_type,
            amount=total,
            currency=proposal.currency,
            is_manual=True,
        ),
    )

    if proposal.status != "PROPOSED":
        raise SoDViolation("ALREADY_DECIDED", f"This proposal is already {proposal.status}.")

    return proposal.model_copy(
        update={
            "approver_id": principal.user_id,
            "status": "APPROVED",
            "metadata": {**proposal.metadata, "approval_reason": reason or ""},
        }
    )
