"""Exception aging and escalation (spec sections 31 and 60)."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from packages.domain.dates import utc_now
from packages.domain.enums import ExceptionSeverity, ExceptionStatus
from packages.domain.models.exceptions import ExceptionRecord

__all__ = ["AgingBucket", "AgingReport", "build_aging_report", "is_overdue"]

# Buckets a controller recognises from any AR/AP aging report.
BUCKET_BOUNDS: tuple[tuple[str, int, int | None], ...] = (
    ("0-7", 0, 7),
    ("8-30", 8, 30),
    ("31-60", 31, 60),
    ("61-90", 61, 90),
    ("90+", 91, None),
)

OPEN_STATUSES = frozenset(
    {
        ExceptionStatus.OPEN,
        ExceptionStatus.TRIAGED,
        ExceptionStatus.ASSIGNED,
        ExceptionStatus.INVESTIGATING,
        ExceptionStatus.PROPOSED_RESOLUTION,
        ExceptionStatus.AWAITING_APPROVAL,
        ExceptionStatus.BLOCKED,
        ExceptionStatus.ESCALATED,
        ExceptionStatus.REOPENED,
    }
)


def age_days(exception: ExceptionRecord, now: datetime | None = None) -> int:
    return ((now or utc_now()) - exception.first_detected_at).days


def is_overdue(exception: ExceptionRecord, now: datetime | None = None) -> bool:
    if exception.due_at is None or exception.status not in OPEN_STATUSES:
        return False
    return (now or utc_now()) > exception.due_at


def bucket_for(days: int) -> str:
    for label, lower, upper in BUCKET_BOUNDS:
        if days >= lower and (upper is None or days <= upper):
            return label
    return "90+"


@dataclass(slots=True)
class AgingBucket:
    label: str
    count: int = 0
    exposure: Decimal = Decimal("0")
    high_risk_count: int = 0


@dataclass(slots=True)
class AgingReport:
    buckets: list[AgingBucket] = field(default_factory=list)
    overdue_count: int = 0
    overdue_exposure: Decimal = Decimal("0")
    oldest_age_days: int | None = None
    by_category: dict[str, int] = field(default_factory=dict)
    by_severity: dict[str, int] = field(default_factory=dict)
    total_open: int = 0
    total_exposure: Decimal = Decimal("0")

    def bucket(self, label: str) -> AgingBucket | None:
        for bucket in self.buckets:
            if bucket.label == label:
                return bucket
        return None


def build_aging_report(
    exceptions: list[ExceptionRecord],
    now: datetime | None = None,
) -> AgingReport:
    """Aggregate open exceptions into aging buckets."""
    stamp = now or utc_now()
    buckets = {label: AgingBucket(label=label) for label, _, _ in BUCKET_BOUNDS}
    report = AgingReport(buckets=[buckets[label] for label, _, _ in BUCKET_BOUNDS])

    categories: Counter[str] = Counter()
    severities: Counter[str] = Counter()

    for exception in exceptions:
        if exception.status not in OPEN_STATUSES:
            continue

        report.total_open += 1
        exposure = exception.amount_exposure or Decimal("0")
        report.total_exposure += exposure

        days = age_days(exception, stamp)
        if report.oldest_age_days is None or days > report.oldest_age_days:
            report.oldest_age_days = days

        bucket = buckets[bucket_for(days)]
        bucket.count += 1
        bucket.exposure += exposure
        if exception.severity in {ExceptionSeverity.HIGH, ExceptionSeverity.CRITICAL}:
            bucket.high_risk_count += 1

        if is_overdue(exception, stamp):
            report.overdue_count += 1
            report.overdue_exposure += exposure

        categories[exception.category.value] += 1
        severities[exception.severity.value] += 1

    report.by_category = dict(categories.most_common())
    report.by_severity = dict(severities.most_common())
    return report


def next_reminder_at(
    exception: ExceptionRecord, now: datetime | None = None
) -> datetime | None:
    """When to remind the owner next.

    Cadence follows severity: a critical item is chased daily, a low one weekly.
    """
    if exception.status not in OPEN_STATUSES:
        return None
    interval = {
        ExceptionSeverity.CRITICAL: timedelta(days=1),
        ExceptionSeverity.HIGH: timedelta(days=2),
        ExceptionSeverity.MEDIUM: timedelta(days=4),
        ExceptionSeverity.LOW: timedelta(days=7),
    }[exception.severity]
    return (now or utc_now()) + interval
