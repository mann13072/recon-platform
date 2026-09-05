"""The single money implementation for the whole platform (spec section 83).

Rules that are not negotiable anywhere in this codebase:

* money is ``Decimal``, never ``float``;
* arithmetic across currencies raises rather than guessing;
* rounding is explicit and uses ``ROUND_HALF_UP``, which is what finance teams
  expect, rather than Python's default banker's rounding.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

__all__ = [
    "CurrencyMismatchError",
    "Money",
    "currency_exponent",
    "minor_units",
    "quantize",
    "sum_money",
    "to_decimal",
]

# Currencies whose minor unit is not 1/100. Extend as real customers appear;
# never guess an exponent at runtime.
_MINOR_UNIT_EXPONENTS: dict[str, int] = {
    "BHD": 3,
    "BIF": 0,
    "CLP": 0,
    "DJF": 0,
    "GNF": 0,
    "IQD": 3,
    "ISK": 0,
    "JOD": 3,
    "JPY": 0,
    "KMF": 0,
    "KRW": 0,
    "KWD": 3,
    "LYD": 3,
    "OMR": 3,
    "PYG": 0,
    "RWF": 0,
    "TND": 3,
    "UGX": 0,
    "VND": 0,
    "VUV": 0,
    "XAF": 0,
    "XOF": 0,
    "XPF": 0,
}

DEFAULT_MINOR_UNIT_EXPONENT = 2


class CurrencyMismatchError(ValueError):
    """Raised when an operation mixes two different currencies."""


def to_decimal(value: Any) -> Decimal:
    """Coerce a value to ``Decimal`` without ever routing through ``float``.

    ``float`` inputs are rejected outright: accepting them is how rounding
    errors enter an accounting system.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise TypeError("bool is not a valid monetary amount")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        raise TypeError("float is not accepted as a monetary amount; pass Decimal or str")
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("empty string is not a valid monetary amount")
        try:
            return Decimal(text)
        except InvalidOperation as exc:  # pragma: no cover - message clarity only
            raise ValueError(f"cannot parse monetary amount: {value!r}") from exc
    raise TypeError(f"unsupported monetary amount type: {type(value).__name__}")


def currency_exponent(currency: str) -> int:
    """Number of decimal places in the currency's minor unit."""
    return _MINOR_UNIT_EXPONENTS.get(currency.upper(), DEFAULT_MINOR_UNIT_EXPONENT)


def quantize(amount: Decimal, currency: str) -> Decimal:
    """Round ``amount`` to the currency's minor unit using ROUND_HALF_UP."""
    exponent = currency_exponent(currency)
    return amount.quantize(Decimal(1).scaleb(-exponent), rounding=ROUND_HALF_UP)


def minor_units(amount: Decimal, currency: str) -> int:
    """Integerize an amount into minor units.

    Subset-sum and optimisation code works on integers so comparisons stay exact
    and no tolerance leaks in through binary floating point.
    """
    exponent = currency_exponent(currency)
    scaled = quantize(amount, currency).scaleb(exponent)
    return int(scaled.to_integral_value(rounding=ROUND_HALF_UP))


@dataclass(frozen=True, slots=True)
class Money:
    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        if not isinstance(self.currency, str) or len(self.currency) != 3:
            raise ValueError("Currency must be ISO-style 3-letter code")
        if not self.currency.isalpha():
            raise ValueError("Currency must be alphabetic")
        object.__setattr__(self, "currency", self.currency.upper())
        object.__setattr__(self, "amount", to_decimal(self.amount))

    # -- construction ------------------------------------------------------
    @classmethod
    def of(cls, amount: Any, currency: str) -> Money:
        return cls(to_decimal(amount), currency)

    @classmethod
    def zero(cls, currency: str) -> Money:
        return cls(Decimal("0"), currency)

    @classmethod
    def from_minor_units(cls, units: int, currency: str) -> Money:
        exponent = currency_exponent(currency)
        return cls(Decimal(units).scaleb(-exponent), currency)

    # -- arithmetic --------------------------------------------------------
    def _assert_same_currency(self, other: Money) -> None:
        if self.currency != other.currency:
            raise CurrencyMismatchError(f"Currency mismatch: {self.currency} vs {other.currency}")

    def add(self, other: Money) -> Money:
        self._assert_same_currency(other)
        return Money(self.amount + other.amount, self.currency)

    def subtract(self, other: Money) -> Money:
        self._assert_same_currency(other)
        return Money(self.amount - other.amount, self.currency)

    def negate(self) -> Money:
        return Money(-self.amount, self.currency)

    def absolute(self) -> Money:
        return Money(abs(self.amount), self.currency)

    def multiply(self, factor: Decimal | int | str) -> Money:
        return Money(self.amount * to_decimal(factor), self.currency)

    def quantized(self) -> Money:
        return Money(quantize(self.amount, self.currency), self.currency)

    def to_minor_units(self) -> int:
        return minor_units(self.amount, self.currency)

    # -- predicates --------------------------------------------------------
    @property
    def is_zero(self) -> bool:
        return self.amount == 0

    def __add__(self, other: Money) -> Money:
        return self.add(other)

    def __sub__(self, other: Money) -> Money:
        return self.subtract(other)

    def __neg__(self) -> Money:
        return self.negate()

    def __lt__(self, other: Money) -> bool:
        self._assert_same_currency(other)
        return self.amount < other.amount

    def __le__(self, other: Money) -> bool:
        self._assert_same_currency(other)
        return self.amount <= other.amount

    def __str__(self) -> str:
        return f"{quantize(self.amount, self.currency)} {self.currency}"


def sum_money(values: list[Money], currency: str) -> Money:
    """Sum a list of ``Money``; the currency is explicit so the empty case is safe."""
    total = Money.zero(currency)
    for value in values:
        total = total.add(value)
    return total
