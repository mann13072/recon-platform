"""The materiality engine (spec section 33).

Materiality is not matching confidence. A EUR 2 ambiguity and a EUR 2,000,000
ambiguity can carry identical evidence and still deserve completely different
approval policies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from packages.domain.enums import ExceptionSeverity

__all__ = [
    "MaterialityDecision",
    "MaterialityPolicy",
    "assess",
    "requires_manual_approval",
    "severity_for",
]


@dataclass(frozen=True, slots=True)
class MaterialityPolicy:
    """Inputs from spec section 33."""

    materiality_threshold: Decimal = Decimal("50000.00")
    percentage_of_balance: Decimal | None = None
    account_balance: Decimal | None = None
    always_review_types: frozenset[str] = frozenset()
    high_risk_accounts: frozenset[str] = frozenset()
    high_risk_entities: frozenset[str] = frozenset()
    manual_approval_above_materiality: bool = True
    period_is_closed: bool = False
    approval_limit: Decimal | None = None

    # Severity bands as multiples of the materiality threshold.
    critical_multiple: Decimal = Decimal("10")
    high_multiple: Decimal = Decimal("1")
    medium_fraction: Decimal = Decimal("0.1")

    def effective_threshold(self) -> Decimal:
        """The tighter of the absolute threshold and the percentage rule."""
        if self.percentage_of_balance is None or self.account_balance is None:
            return self.materiality_threshold
        relative = abs(self.account_balance) * self.percentage_of_balance
        return min(self.materiality_threshold, relative)


@dataclass(slots=True)
class MaterialityDecision:
    requires_approval: bool
    severity: ExceptionSeverity
    threshold: Decimal
    reasons: list[str] = field(default_factory=list)

    def explain(self) -> str:
        if not self.reasons:
            return "Below materiality and not otherwise flagged."
        return "; ".join(self.reasons)


@dataclass(frozen=True, slots=True)
class MatchAssessment:
    """The facts about a match that materiality cares about."""

    total_amount: Decimal
    currency: str = "EUR"
    reconciliation_type: str = ""
    account: str | None = None
    entity: str | None = None
    contains_manual_adjustment: bool = False
    contains_override: bool = False


def requires_manual_approval(
    match: MatchAssessment,
    policy: MaterialityPolicy,
) -> bool:
    """The predicate from spec section 33, kept in its original shape."""
    return assess(match, policy).requires_approval


def assess(match: MatchAssessment, policy: MaterialityPolicy) -> MaterialityDecision:
    """Full assessment, with the reasons attached so the UI can show them."""
    threshold = policy.effective_threshold()
    amount = abs(match.total_amount)
    reasons: list[str] = []
    requires = False

    if policy.manual_approval_above_materiality and amount >= threshold:
        requires = True
        reasons.append(
            f"{match.currency} {amount:,.2f} is at or above the materiality "
            f"threshold of {threshold:,.2f}"
        )

    if match.contains_manual_adjustment:
        requires = True
        reasons.append("the item contains a manual adjustment")

    if match.contains_override:
        requires = True
        reasons.append("an automated decision was overridden")

    if match.reconciliation_type and match.reconciliation_type in policy.always_review_types:
        requires = True
        reasons.append(f"reconciliation type '{match.reconciliation_type}' is always reviewed")

    if match.account and match.account in policy.high_risk_accounts:
        requires = True
        reasons.append(f"account '{match.account}' is flagged high risk")

    if match.entity and match.entity in policy.high_risk_entities:
        requires = True
        reasons.append(f"legal entity '{match.entity}' is flagged high risk")

    if policy.period_is_closed:
        requires = True
        reasons.append("the accounting period is closed")

    if policy.approval_limit is not None and amount > policy.approval_limit:
        requires = True
        reasons.append(f"the amount exceeds the approver's limit of {policy.approval_limit:,.2f}")

    return MaterialityDecision(
        requires_approval=requires,
        severity=severity_for(amount, policy),
        threshold=threshold,
        reasons=reasons,
    )


def severity_for(amount: Decimal, policy: MaterialityPolicy) -> ExceptionSeverity:
    """Map an exposure to a severity band."""
    threshold = policy.effective_threshold()
    magnitude = abs(amount)

    if threshold <= 0:
        return ExceptionSeverity.MEDIUM
    if magnitude >= threshold * policy.critical_multiple:
        return ExceptionSeverity.CRITICAL
    if magnitude >= threshold * policy.high_multiple:
        return ExceptionSeverity.HIGH
    if magnitude >= threshold * policy.medium_fraction:
        return ExceptionSeverity.MEDIUM
    return ExceptionSeverity.LOW
