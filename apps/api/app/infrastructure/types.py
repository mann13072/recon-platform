"""Portable column types.

The application targets PostgreSQL. The test suite runs on SQLite so that the
API can be exercised without a server. That is only safe if the types behave
identically on both, and the one that does *not* by default is money: SQLite
returns ``NUMERIC`` columns as Python floats, which would silently reintroduce
binary floating point into the ledger.

:class:`Money` fixes that by coercing on the way out. It is used for every
monetary column in the schema.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import CHAR, JSON, Numeric, String, TypeDecorator
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID

__all__ = ["GUID", "JSONColumn", "Money", "MONEY_PRECISION", "MONEY_SCALE"]

MONEY_PRECISION = 24
MONEY_SCALE = 8


# SQLite has no exact decimal type: it stores NUMERIC as a 64-bit float, which
# silently rounds anything past ~15 significant digits. Rather than accept a
# test database that is weaker than production, money is stored on SQLite as a
# fixed-width offset-scaled integer string. That is exact, and because the
# width is fixed and the offset makes every value non-negative, lexicographic
# ordering equals numeric ordering - so ORDER BY still behaves.
#
# Range: NUMERIC(24, 8) holds |value| < 10^16. Scaling by 10^8 and offsetting by
# 10^24 lands every legal value in [0, 2 * 10^24), which fits in 25 digits.
_SQLITE_OFFSET = 10**24
_SQLITE_WIDTH = 25
_SQLITE_SCALE = Decimal(1).scaleb(MONEY_SCALE)


class Money(TypeDecorator[Decimal]):
    """A monetary column that always reads back as an exact ``Decimal``.

    NUMERIC(24, 8) on PostgreSQL; an order-preserving fixed-width encoding on
    SQLite. Floats are rejected on write: if a float reaches this layer the
    precision was already lost upstream, and failing loudly is the only useful
    response.

    One caveat worth knowing: SQL-side aggregation (``func.sum``) over a money
    column is only meaningful on PostgreSQL. Application code sums money in
    Python, through ``packages.domain.money``, which is exact on both.
    """

    impl = Numeric(MONEY_PRECISION, MONEY_SCALE, asdecimal=True)
    cache_ok = True

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect.name == "sqlite":
            return dialect.type_descriptor(String(_SQLITE_WIDTH))
        return dialect.type_descriptor(Numeric(MONEY_PRECISION, MONEY_SCALE, asdecimal=True))

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        amount = _coerce(value)
        if amount is None:
            return None
        if dialect.name != "sqlite":
            return amount

        scaled = (amount * _SQLITE_SCALE).to_integral_value()
        shifted = int(scaled) + _SQLITE_OFFSET
        if not 0 <= shifted < 2 * _SQLITE_OFFSET:
            raise ValueError(
                f"monetary amount {amount} is outside the supported "
                f"NUMERIC({MONEY_PRECISION}, {MONEY_SCALE}) range"
            )
        return str(shifted).zfill(_SQLITE_WIDTH)

    def process_result_value(self, value: Any, dialect: Any) -> Decimal | None:
        if value is None:
            return None
        if dialect.name == "sqlite":
            shifted = int(value) - _SQLITE_OFFSET
            return (Decimal(shifted) / _SQLITE_SCALE).normalize() + Decimal("0")
        if isinstance(value, Decimal):
            return value
        # Defensive: another dialect returning a float would reintroduce binary
        # floating point. Route through repr so the shortest round-trippable
        # form is used rather than the full binary expansion.
        return Decimal(repr(value) if isinstance(value, float) else str(value))


def _coerce(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise TypeError("bool is not a monetary amount")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        raise TypeError("float amounts are rejected at the database boundary; use Decimal")
    return Decimal(str(value))


class GUID(TypeDecorator[UUID]):
    """UUID column: native ``uuid`` on PostgreSQL, 36-char string elsewhere."""

    impl = CHAR(36)
    cache_ok = True

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PGUUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        if dialect.name == "postgresql":
            return value if isinstance(value, UUID) else UUID(str(value))
        return str(value)

    def process_result_value(self, value: Any, dialect: Any) -> UUID | None:
        if value is None:
            return None
        return value if isinstance(value, UUID) else UUID(str(value))


# JSONB on PostgreSQL for indexing and containment queries; plain JSON on SQLite.
JSONColumn = JSON().with_variant(JSONB(), "postgresql")

# Short, indexed identifier columns.
ShortText = String(64)
MediumText = String(255)
