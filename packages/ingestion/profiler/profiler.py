"""Column profiling: what does this file actually contain? (spec section 10)

The profile drives two things: the mapping suggestions shown to the user, and
the data-quality report. It never modifies data.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from packages.domain.dates import AmbiguousDateFormatError, detect_date_format
from packages.ingestion.normalization.numbers import (
    AmbiguousNumberFormatError,
    NumberFormat,
    detect_number_format,
    parse_amount,
)
from packages.ingestion.parsers import ParsedTable

__all__ = ["ColumnProfile", "ColumnType", "FileProfile", "profile_table"]


class ColumnType(StrEnum):
    AMOUNT = "amount"
    DATE = "date"
    CURRENCY = "currency"
    IDENTIFIER = "identifier"
    TEXT = "text"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    EMPTY = "empty"


_CURRENCY_RE = re.compile(r"^[A-Za-z]{3}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]{2,}$")
_BOOL_VALUES = {"TRUE", "FALSE", "YES", "NO", "Y", "N", "0", "1"}

# A rough ISO-4217 sanity list. Not exhaustive; used only to raise confidence
# that a 3-letter column really is a currency column.
_KNOWN_CURRENCIES = {
    "AUD",
    "BRL",
    "CAD",
    "CHF",
    "CNY",
    "CZK",
    "DKK",
    "EUR",
    "GBP",
    "HKD",
    "HUF",
    "IDR",
    "ILS",
    "INR",
    "JPY",
    "KRW",
    "MXN",
    "MYR",
    "NOK",
    "NZD",
    "PHP",
    "PLN",
    "RON",
    "SEK",
    "SGD",
    "THB",
    "TRY",
    "USD",
    "ZAR",
    "AED",
}


@dataclass(slots=True)
class ColumnProfile:
    name: str
    inferred_type: ColumnType
    non_empty_count: int
    empty_count: int
    distinct_count: int
    sample_values: list[str] = field(default_factory=list)
    min_value: str | None = None
    max_value: str | None = None
    number_format: NumberFormat | None = None
    date_format: str | None = None
    warnings: list[str] = field(default_factory=list)
    duplicate_ratio: float = 0.0
    negative_count: int = 0

    @property
    def fill_rate(self) -> float:
        total = self.non_empty_count + self.empty_count
        return (self.non_empty_count / total) if total else 0.0


@dataclass(slots=True)
class FileProfile:
    row_count: int
    column_count: int
    columns: list[ColumnProfile]
    encoding: str
    delimiter: str
    sheet_name: str | None = None
    skipped_rows: list[tuple[int, str]] = field(default_factory=list)

    def column(self, name: str) -> ColumnProfile | None:
        for col in self.columns:
            if col.name == name:
                return col
        return None


def _classify(name: str, values: list[str]) -> tuple[ColumnType, ColumnProfile]:
    profile = ColumnProfile(
        name=name,
        inferred_type=ColumnType.TEXT,
        non_empty_count=len(values),
        empty_count=0,
        distinct_count=len(set(values)),
        sample_values=values[:5],
    )

    if not values:
        profile.inferred_type = ColumnType.EMPTY
        return ColumnType.EMPTY, profile

    upper = [v.upper() for v in values]

    if all(v in _BOOL_VALUES for v in upper) and len(set(upper)) <= 2:
        profile.inferred_type = ColumnType.BOOLEAN
        return ColumnType.BOOLEAN, profile

    if all(_CURRENCY_RE.match(v) for v in values):
        known = sum(1 for v in upper if v in _KNOWN_CURRENCIES)
        if known >= max(1, int(len(values) * 0.8)):
            profile.inferred_type = ColumnType.CURRENCY
            if len(set(upper)) > 1:
                profile.warnings.append("column contains more than one currency")
            return ColumnType.CURRENCY, profile

    # Dates before amounts: "20260831" parses as an integer too.
    try:
        profile.date_format = detect_date_format(values)
        profile.inferred_type = ColumnType.DATE
        profile.min_value = min(values)
        profile.max_value = max(values)
        return ColumnType.DATE, profile
    except AmbiguousDateFormatError:
        profile.inferred_type = ColumnType.DATE
        profile.warnings.append(
            "date format is ambiguous between DD/MM/YYYY and MM/DD/YYYY; confirm during mapping"
        )
        return ColumnType.DATE, profile
    except ValueError:
        pass

    try:
        number_format = detect_number_format(values)
        parsed: list[Decimal] = []
        for value in values:
            amount = parse_amount(value, number_format)
            if amount is None:
                raise ValueError(name)
            parsed.append(amount)
    except (ValueError, TypeError, AmbiguousNumberFormatError) as exc:
        if isinstance(exc, AmbiguousNumberFormatError):
            profile.warnings.append(str(exc))
    else:
        profile.number_format = number_format
        profile.negative_count = sum(1 for p in parsed if p < 0)
        profile.min_value = str(min(parsed))
        profile.max_value = str(max(parsed))
        has_fraction = any(p != p.to_integral_value() for p in parsed)
        profile.inferred_type = ColumnType.AMOUNT if has_fraction else ColumnType.INTEGER
        return profile.inferred_type, profile

    if all(_IDENTIFIER_RE.match(v) for v in values) and profile.distinct_count > len(values) * 0.5:
        profile.inferred_type = ColumnType.IDENTIFIER
        return ColumnType.IDENTIFIER, profile

    profile.inferred_type = ColumnType.TEXT
    return ColumnType.TEXT, profile


def profile_table(table: ParsedTable, *, sample_size: int = 500) -> FileProfile:
    """Profile every column of a parsed table."""
    columns: list[ColumnProfile] = []

    for name in table.columns:
        raw_values = [(row.get(name) or "").strip() for row in table.rows]
        non_empty = [v for v in raw_values if v]
        sample = non_empty[:sample_size]

        _, profile = _classify(name, sample)
        profile.non_empty_count = len(non_empty)
        profile.empty_count = len(raw_values) - len(non_empty)
        profile.distinct_count = len(set(non_empty))
        profile.sample_values = non_empty[:5]

        if non_empty:
            counts = Counter(non_empty)
            repeated = sum(c for c in counts.values() if c > 1)
            profile.duplicate_ratio = repeated / len(non_empty)

        if profile.fill_rate < 0.8 and profile.non_empty_count:
            profile.warnings.append(f"only {profile.fill_rate:.0%} of rows have a value")

        columns.append(profile)

    return FileProfile(
        row_count=len(table.rows),
        column_count=len(table.columns),
        columns=columns,
        encoding=table.encoding,
        delimiter=table.delimiter,
        sheet_name=table.sheet_name,
        skipped_rows=list(table.skipped_rows),
    )
