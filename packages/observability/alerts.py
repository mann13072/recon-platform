"""Alert rules (spec section 60).

An alert is only worth waking someone for if it names an action, so every rule
carries the question it answers and the first thing to do about it.

The rules are declarative rather than scattered through the code because the
thresholds are the product's risk appetite, and a controller who does not read
Python should still be able to review them.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

__all__ = ["ALERT_RULES", "AlertRule", "Severity", "evaluate"]


class Severity(StrEnum):
    PAGE = "page"
    """Wake someone now."""

    TICKET = "ticket"
    """Handle during business hours."""

    REVIEW = "review"
    """Surface to a controller, not to engineering."""


@dataclass(frozen=True, slots=True)
class AlertRule:
    name: str
    metric: str
    severity: Severity
    operator: Literal["gt", "lt"]
    threshold: float
    threshold_description: str
    question: str
    first_action: str
    runbook: str = ""

    def predicate(self, value: float) -> bool:
        """Evaluate the declared threshold without hiding it in a lambda."""
        return value > self.threshold if self.operator == "gt" else value < self.threshold


ALERT_RULES: tuple[AlertRule, ...] = (
    AlertRule(
        name="false_automatic_matches",
        metric="false_automatic_match_rate",
        severity=Severity.PAGE,
        operator="gt",
        threshold=0.0,
        threshold_description="any confirmed false automatic match",
        question="Has the platform automatically matched something incorrectly?",
        first_action=(
            "Raise the affected reconciliation's auto-match threshold to stop "
            "further automation, then find which rule produced the false match "
            "and lower its precision estimate."
        ),
        runbook="docs/runbooks/reconciliation-job-failure.md",
    ),
    AlertRule(
        name="audit_chain_broken",
        metric="audit_chain_verified",
        severity=Severity.PAGE,
        operator="lt",
        threshold=1.0,
        threshold_description="audit chain verification failed for any tenant",
        question="Has anyone altered a historical audit event?",
        first_action="Follow the security incident runbook. Do not deploy.",
        runbook="docs/runbooks/security-incident.md",
    ),
    AlertRule(
        name="candidate_explosion",
        metric="candidate_explosion_count",
        severity=Severity.TICKET,
        operator="gt",
        threshold=0.0,
        threshold_description="any run exceeding the expected candidates per row",
        question="Has a blocking key stopped discriminating?",
        first_action=(
            "Check whether a source has started sending the same reference on "
            "every row. Runs get slower and suggestions get noisier."
        ),
        runbook="docs/runbooks/reconciliation-job-failure.md",
    ),
    AlertRule(
        name="connector_unhealthy",
        metric="connector_sync_success_rate",
        severity=Severity.TICKET,
        operator="lt",
        threshold=0.9,
        threshold_description="under 90% of syncs succeeding",
        question="Is a reconciliation about to run on stale data?",
        first_action=(
            "Check the connector health state. A failed connector must not "
            "produce a reconciliation that looks complete."
        ),
    ),
    AlertRule(
        name="ai_schema_failures",
        metric="ai_schema_validation_failure",
        severity=Severity.TICKET,
        operator="gt",
        threshold=10.0,
        threshold_description="more than 10 rejected AI responses in a window",
        question="Has a model or prompt version started returning invalid output?",
        first_action=(
            "Nothing is financially broken: rejected responses change no state. "
            "Check whether the provider changed the model behind the endpoint."
        ),
    ),
    AlertRule(
        name="high_value_unmatched",
        metric="high_value_unmatched_count",
        severity=Severity.REVIEW,
        operator="gt",
        threshold=0.0,
        threshold_description="any unmatched item at or above materiality",
        question="Is material money unaccounted for?",
        first_action="Route to the controller who owns the reconciliation.",
    ),
    AlertRule(
        name="exception_backlog",
        metric="exception_aging",
        severity=Severity.REVIEW,
        operator="gt",
        threshold=30.0,
        threshold_description="an open exception older than 30 days",
        question="Is the exception queue being worked?",
        first_action="Escalate to the controller and check owner assignment.",
    ),
    AlertRule(
        name="reopened_reconciliations",
        metric="reopened_reconciliations",
        severity=Severity.REVIEW,
        operator="gt",
        threshold=0.0,
        threshold_description="any reconciliation reopened after closing",
        question="Was something signed off that should not have been?",
        first_action=(
            "Read the reopen reason. A pattern of reopenings means the close "
            "gate is too weak, not that users are careless."
        ),
    ),
)


def evaluate(values: dict[str, float]) -> list[tuple[AlertRule, float]]:
    """Which rules fire for a set of current metric values."""
    firing: list[tuple[AlertRule, float]] = []
    for rule in ALERT_RULES:
        if rule.metric in values and rule.predicate(values[rule.metric]):
            firing.append((rule, values[rule.metric]))
    return firing
