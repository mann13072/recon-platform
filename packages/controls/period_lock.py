"""Closed-period protection (spec sections 57 and 58).

A locked period is immutable. Nothing - not a connector sync, not a rerun, not
an AI suggestion - may alter a transaction, match or exception dated inside it
until a privileged user formally unlocks it, which is itself an audit event.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from uuid import UUID

from packages.controls.permissions import Permission, Principal
from packages.controls.segregation_of_duties import SoDViolation
from packages.domain.dates import utc_now

__all__ = ["PeriodLock", "PeriodLockRegistry", "PeriodLocked"]


class PeriodLocked(SoDViolation):
    """Raised when an operation targets a locked accounting period."""

    def __init__(self, period: str, locked_by: UUID | None) -> None:
        super().__init__(
            "PERIOD_LOCKED",
            f"Accounting period {period} is locked"
            + (f" (locked by {locked_by})" if locked_by else "")
            + ". It must be formally unlocked before anything dated inside it "
            "can change.",
        )
        self.period = period


@dataclass(frozen=True, slots=True)
class PeriodLock:
    tenant_id: UUID
    entity: str
    period_start: date
    period_end: date
    locked_by: UUID
    locked_at: datetime
    reason: str | None = None

    @property
    def label(self) -> str:
        return f"{self.period_start.isoformat()}..{self.period_end.isoformat()}"

    def covers(self, when: date | None) -> bool:
        if when is None:
            # A record with no date cannot be proven to fall outside the lock,
            # so it is treated as inside it. Failing closed is the safe default
            # for a control.
            return True
        return self.period_start <= when <= self.period_end


@dataclass(slots=True)
class PeriodLockRegistry:
    """The set of locks in force for a tenant."""

    locks: list[PeriodLock] = field(default_factory=list)

    def lock(
        self,
        principal: Principal,
        *,
        tenant_id: UUID,
        entity: str,
        period_start: date,
        period_end: date,
        reason: str | None = None,
    ) -> PeriodLock:
        principal.require(Permission.LOCK_PERIOD)
        if principal.user_id is None:
            raise SoDViolation("ACTOR_REQUIRED", "Locking a period requires an authenticated user.")
        if period_end < period_start:
            raise ValueError("period_end must not precede period_start")

        lock = PeriodLock(
            tenant_id=tenant_id,
            entity=entity,
            period_start=period_start,
            period_end=period_end,
            locked_by=principal.user_id,
            locked_at=utc_now(),
            reason=reason,
        )
        self.locks.append(lock)
        return lock

    def unlock(self, principal: Principal, lock: PeriodLock, reason: str) -> None:
        principal.require(Permission.UNLOCK_PERIOD)
        if not reason.strip():
            raise SoDViolation("REASON_REQUIRED", "Unlocking a period requires a written reason.")
        self.locks = [item for item in self.locks if item != lock]

    def find(self, entity: str, when: date | None) -> PeriodLock | None:
        for lock in self.locks:
            if lock.entity == entity and lock.covers(when):
                return lock
        return None

    def assert_open(self, entity: str, when: date | None) -> None:
        """Raise if the given date falls inside a locked period."""
        lock = self.find(entity, when)
        if lock is not None:
            raise PeriodLocked(lock.label, lock.locked_by)

    def assert_range_open(self, entity: str, start: date | None, end: date | None) -> None:
        """Raise if any part of a range overlaps a locked period."""
        for lock in self.locks:
            if lock.entity != entity:
                continue
            if start is None or end is None:
                raise PeriodLocked(lock.label, lock.locked_by)
            if not (end < lock.period_start or start > lock.period_end):
                raise PeriodLocked(lock.label, lock.locked_by)
