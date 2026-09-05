"""Tolerance predicates (spec section 16).

These functions are generic. They take the tolerance as an argument and never
embed a company's materiality rules, because materiality is a control decision
(spec section 33), not a matching decision.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from packages.domain.dates import date_distance_days

__all__ = [
    "amount_close",
    "amount_difference",
    "date_close",
    "fee_explains_difference",
    "fx_close",
    "relative_difference",
]


def amount_difference(a: Decimal, b: Decimal) -> Decimal:
    return abs(a - b)


def relative_difference(a: Decimal, b: Decimal) -> Decimal | None:
    """Difference as a fraction of ``b``. ``None`` when ``b`` is zero."""
    if b == 0:
        return None
    return abs(a - b) / abs(b)


def amount_close(
    a: Decimal,
    b: Decimal,
    absolute_tolerance: Decimal,
    percentage_tolerance: Decimal | None = None,
) -> bool:
    """Whether two amounts are within tolerance.

    Absolute tolerance is checked first because it is the one a controller can
    reason about directly.
    """
    difference = abs(a - b)

    if difference <= absolute_tolerance:
        return True

    if percentage_tolerance is not None and b != 0:
        return difference / abs(b) <= percentage_tolerance

    return False


def date_close(a: date | None, b: date | None, window_days: int) -> bool:
    """Whether two dates are within ``window_days``.

    A missing date is never "close": absence of evidence is not evidence.
    """
    distance = date_distance_days(a, b)
    if distance is None:
        return False
    return distance <= window_days


def fee_explains_difference(
    gross: Decimal | None,
    net: Decimal | None,
    fee: Decimal | None,
    tolerance: Decimal = Decimal("0"),
) -> bool:
    """Whether ``gross - fee == net`` within tolerance.

    This is the deterministic settlement identity. It is strictly better than
    fuzzy matching for processor payouts (spec section 67).
    """
    if gross is None or net is None or fee is None:
        return False
    return abs((gross - abs(fee)) - net) <= tolerance


def fx_close(
    original_amount: Decimal | None,
    original_currency: str | None,
    functional_amount: Decimal,
    fx_rate: Decimal | None,
    percentage_tolerance: Decimal,
) -> bool:
    """Whether a converted amount is consistent with the stated FX rate.

    FX is deliberately not modelled as a wider amount tolerance (spec section
    66): a rate must be supplied and the conversion must reproduce the booked
    amount, otherwise the difference is unexplained and belongs in an exception.
    """
    if original_amount is None or fx_rate is None or not original_currency:
        return False
    if fx_rate <= 0:
        return False
    expected = original_amount * fx_rate
    if expected == 0:
        return functional_amount == 0
    return abs(expected - functional_amount) / abs(expected) <= percentage_tolerance
