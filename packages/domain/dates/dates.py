"""Date handling for reconciliation (spec section 84).

A financial transaction has several dates and they are not interchangeable:

* ``transaction_date`` - when the economic event happened;
* ``posting_date``     - when the source system booked it;
* ``value_date``       - when value moved / interest starts;
* ``settlement_date``  - when a processor settled the batch.

Matching rules pick which date concept matters. Nothing here collapses them
into a single generic ``date``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum

__all__ = [
    "AmbiguousDateFormatError",
    "DateField",
    "DateWindow",
    "date_distance_days",
    "detect_date_format",
    "ensure_utc",
    "parse_date",
    "utc_now",
]


class DateField(StrEnum):
    """Which date concept a rule compares."""

    TRANSACTION = "transaction_date"
    POSTING = "posting_date"
    VALUE = "value_date"
    SETTLEMENT = "settlement_date"


class AmbiguousDateFormatError(ValueError):
    """Raised when a column could be either DD/MM/YYYY or MM/DD/YYYY."""


def utc_now() -> datetime:
    """Timezone-aware current time. Never use ``datetime.utcnow()``."""
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    """Attach UTC to a naive datetime, or convert an aware one."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


# Ordered by preference. ISO first so unambiguous data never hits the
# day/month guessing path at all.
_EXPLICIT_FORMATS: tuple[tuple[str, str], ...] = (
    ("%Y-%m-%d", "ISO_YMD"),
    ("%Y/%m/%d", "YMD_SLASH"),
    ("%Y%m%d", "YMD_COMPACT"),
    ("%d-%b-%Y", "DMY_MONTHNAME"),
    ("%d %b %Y", "DMY_MONTHNAME_SPACE"),
    ("%b %d, %Y", "MDY_MONTHNAME"),
    ("%d.%m.%Y", "DMY_DOT"),
)

_NUMERIC_SLASH_RE = re.compile(r"^\s*(\d{1,2})[/-](\d{1,2})[/-](\d{4})\s*$")


def detect_date_format(samples: list[str]) -> str:
    """Infer a date format from a column of samples.

    Returns a format label. Raises :class:`AmbiguousDateFormatError` when the
    samples are numeric day/month values that are all <= 12, because guessing
    silently is exactly the sort of "helpful fix" the spec forbids (section 10).
    """
    cleaned = [s for s in (v.strip() for v in samples if v) if s]
    if not cleaned:
        raise ValueError("no non-empty date samples")

    for fmt, label in _EXPLICIT_FORMATS:
        if all(_matches(value, fmt) for value in cleaned):
            return label

    slash_parts = [_NUMERIC_SLASH_RE.match(value) for value in cleaned]
    if all(slash_parts):
        firsts = [int(m.group(1)) for m in slash_parts if m]
        seconds = [int(m.group(2)) for m in slash_parts if m]
        first_can_be_day = any(v > 12 for v in firsts)
        second_can_be_day = any(v > 12 for v in seconds)
        if first_can_be_day and second_can_be_day:
            raise ValueError("date column contains contradictory day/month values")
        if first_can_be_day:
            return "DMY_NUMERIC"
        if second_can_be_day:
            return "MDY_NUMERIC"
        raise AmbiguousDateFormatError(
            "date column is ambiguous between DD/MM/YYYY and MM/DD/YYYY; "
            "the user must confirm the format during mapping"
        )

    raise ValueError("unrecognised date format")


def _matches(value: str, fmt: str) -> bool:
    try:
        datetime.strptime(value.strip(), fmt)
    except ValueError:
        return False
    return True


_FORMAT_STRINGS: dict[str, str] = {
    "ISO_YMD": "%Y-%m-%d",
    "YMD_SLASH": "%Y/%m/%d",
    "YMD_COMPACT": "%Y%m%d",
    "DMY_MONTHNAME": "%d-%b-%Y",
    "DMY_MONTHNAME_SPACE": "%d %b %Y",
    "MDY_MONTHNAME": "%b %d, %Y",
    "DMY_DOT": "%d.%m.%Y",
    "DMY_NUMERIC": "%d/%m/%Y",
    "MDY_NUMERIC": "%m/%d/%Y",
}


def parse_date(value: str | date | datetime | None, fmt_label: str | None = None) -> date | None:
    """Parse a date using an explicitly chosen format label.

    When ``fmt_label`` is omitted only unambiguous formats are attempted.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = value.strip()
    if not text:
        return None

    if fmt_label:
        fmt = _FORMAT_STRINGS.get(fmt_label)
        if fmt is None:
            raise ValueError(f"unknown date format label: {fmt_label}")
        # Normalise separators for the numeric formats so 01-02-2026 and
        # 01/02/2026 both parse under the label the user confirmed.
        if fmt_label in {"DMY_NUMERIC", "MDY_NUMERIC"}:
            text = text.replace("-", "/")
        return datetime.strptime(text, fmt).date()

    for label in ("ISO_YMD", "YMD_SLASH", "YMD_COMPACT"):
        try:
            return datetime.strptime(text, _FORMAT_STRINGS[label]).date()
        except ValueError:
            continue
    raise AmbiguousDateFormatError(
        f"refusing to guess the format of {value!r}; supply an explicit format label"
    )


def date_distance_days(a: date | None, b: date | None) -> int | None:
    """Absolute distance in days, or ``None`` when either side is missing."""
    if a is None or b is None:
        return None
    return abs((a - b).days)


@dataclass(frozen=True, slots=True)
class DateWindow:
    """An inclusive +/- day window around a reference date."""

    days: int

    def __post_init__(self) -> None:
        if self.days < 0:
            raise ValueError("date window must not be negative")

    def contains(self, a: date | None, b: date | None) -> bool:
        distance = date_distance_days(a, b)
        if distance is None:
            # A missing date cannot prove closeness. Callers decide whether a
            # missing date is disqualifying; this function never invents one.
            return False
        return distance <= self.days

    def bounds(self, anchor: date) -> tuple[date, date]:
        from datetime import timedelta

        return anchor - timedelta(days=self.days), anchor + timedelta(days=self.days)
