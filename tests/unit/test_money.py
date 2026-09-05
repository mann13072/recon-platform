"""Money must never touch float, and must never guess a currency."""

from __future__ import annotations

from decimal import Decimal

import pytest

from packages.domain.money import (
    CurrencyMismatchError,
    Money,
    currency_exponent,
    minor_units,
    quantize,
    sum_money,
    to_decimal,
)


class TestFloatRejection:
    def test_to_decimal_rejects_float(self) -> None:
        with pytest.raises(TypeError, match="float is not accepted"):
            to_decimal(982.45)

    def test_money_of_rejects_float(self) -> None:
        with pytest.raises(TypeError):
            Money.of(982.45, "EUR")

    def test_to_decimal_rejects_bool(self) -> None:
        with pytest.raises(TypeError):
            to_decimal(True)

    def test_string_keeps_full_precision(self) -> None:
        assert Money.of("0.1", "EUR").add(Money.of("0.2", "EUR")).amount == Decimal("0.3")


class TestCurrency:
    def test_rejects_non_iso_length(self) -> None:
        with pytest.raises(ValueError, match="3-letter"):
            Money(Decimal("1"), "EURO")

    def test_rejects_digits(self) -> None:
        with pytest.raises(ValueError, match="alphabetic"):
            Money(Decimal("1"), "E1R")

    def test_upper_cases(self) -> None:
        assert Money(Decimal("1"), "eur").currency == "EUR"

    def test_arithmetic_across_currencies_raises(self) -> None:
        with pytest.raises(CurrencyMismatchError):
            Money.of("1", "EUR").add(Money.of("1", "USD"))

    def test_comparison_across_currencies_raises(self) -> None:
        with pytest.raises(CurrencyMismatchError):
            _ = Money.of("1", "EUR") < Money.of("1", "USD")


class TestMinorUnits:
    def test_default_exponent_is_two(self) -> None:
        assert currency_exponent("EUR") == 2
        assert minor_units(Decimal("982.45"), "EUR") == 98245

    def test_zero_decimal_currency(self) -> None:
        assert currency_exponent("JPY") == 0
        assert minor_units(Decimal("1050"), "JPY") == 1050

    def test_three_decimal_currency(self) -> None:
        assert currency_exponent("KWD") == 3
        assert minor_units(Decimal("1.234"), "KWD") == 1234

    def test_round_trip(self) -> None:
        original = Money.of("982.45", "EUR")
        assert Money.from_minor_units(original.to_minor_units(), "EUR") == original

    def test_half_up_not_bankers_rounding(self) -> None:
        # Python's default would give 0.12 for both; finance expects 0.13/0.12.
        assert quantize(Decimal("0.125"), "EUR") == Decimal("0.13")
        assert quantize(Decimal("0.135"), "EUR") == Decimal("0.14")


class TestArithmetic:
    def test_add_subtract_negate(self) -> None:
        a, b = Money.of("1050.00", "EUR"), Money.of("67.55", "EUR")
        assert (a - b).amount == Decimal("982.45")
        assert (-b).amount == Decimal("-67.55")
        assert b.absolute().amount == Decimal("67.55")

    def test_sum_money_requires_explicit_currency_for_empty(self) -> None:
        assert sum_money([], "EUR") == Money.zero("EUR")

    def test_settlement_identity_is_exact(self) -> None:
        # 1050.00 - 50.00 - 17.55 = 982.45, exactly, with no float drift.
        sales = Money.of("1050.00", "EUR")
        refunds = Money.of("50.00", "EUR")
        fees = Money.of("17.55", "EUR")
        assert sales.subtract(refunds).subtract(fees) == Money.of("982.45", "EUR")
