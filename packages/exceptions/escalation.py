"""Escalation rules for exceptions (spec section 31)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from packages.domain.dates import utc_now
from packages.domain.enums import ExceptionSeverity, ExceptionStatus, Role
from packages.domain.models.exceptions import ExceptionRecord
from packages.exceptions.aging import OPEN_STATUSES, age_days, is_overdue

__all__ = ["EscalationPolicy", "EscalationTrigger", "evaluate_escalation"]


@dataclass(frozen=True, slots=True)
class EscalationPolicy:
    overdue_days: int = 3
    critical_age_days: int = 2
    exposure_threshold: Decimal = Decimal("100000.00")
    blocked_days: int = 5
    escalate_to: Role = Role.CONTROLLER


@dataclass(frozen=True, slots=True)
class EscalationTrigger:
    code: str
    reason: str
    escalate_to: Role


def evaluate_escalation(
    exception: ExceptionRecord,
    policy: EscalationPolicy,
    now: datetime | None = None,
) -> list[EscalationTrigger]:
    """Which escalation rules this exception currently trips.

    Returns every reason rather than the first, so the notification tells the
    controller the whole story.
    """
    stamp = now or utc_now()
    if exception.status not in OPEN_STATUSES:
        return []

    triggers: list[EscalationTrigger] = []
    days = age_days(exception, stamp)

    if is_overdue(exception, stamp):
        triggers.append(
            EscalationTrigger(
                "OVERDUE",
                f"Past its due date and still {exception.status.value.lower()}.",
                policy.escalate_to,
            )
        )

    if (
        exception.severity is ExceptionSeverity.CRITICAL
        and days >= policy.critical_age_days
    ):
        triggers.append(
            EscalationTrigger(
                "CRITICAL_AGE",
                f"Critical exception open for {days} days.",
                policy.escalate_to,
            )
        )

    exposure = exception.amount_exposure or Decimal("0")
    if abs(exposure) >= policy.exposure_threshold:
        triggers.append(
            EscalationTrigger(
                "HIGH_EXPOSURE",
                f"Exposure of {exception.currency or ''} {abs(exposure):,.2f} is at "
                f"or above the escalation threshold.",
                policy.escalate_to,
            )
        )

    if exception.status is ExceptionStatus.BLOCKED and days >= policy.blocked_days:
        triggers.append(
            EscalationTrigger(
                "BLOCKED_TOO_LONG",
                f"Blocked for {days} days with no progress.",
                policy.escalate_to,
            )
        )

    if exception.status is ExceptionStatus.OPEN and days >= policy.overdue_days:
        triggers.append(
            EscalationTrigger(
                "UNTRIAGED",
                f"Still untriaged after {days} days.",
                policy.escalate_to,
            )
        )

    return triggers


@dataclass(slots=True)
class EscalationReport:
    escalations: dict[str, list[EscalationTrigger]] = field(default_factory=dict)

    @property
    def count(self) -> int:
        return len(self.escalations)


def evaluate_all(
    exceptions: list[ExceptionRecord],
    policy: EscalationPolicy,
    now: datetime | None = None,
) -> EscalationReport:
    report = EscalationReport()
    for exception in exceptions:
        triggers = evaluate_escalation(exception, policy, now)
        if triggers:
            report.escalations[str(exception.id)] = triggers
    return report
