"""Amount parsing from messy source files.

Real bank and ERP exports contain "1.234,56", "(1,234.56)", "1 234.56 EUR" and
"1234.56-". Each of those has exactly one correct interpretation, but only once
the separator convention is known. This module makes the convention explicit
and refuses to guess when a value is genuinely ambiguous.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from enum import StrEnum

__all__ = [
    "AmbiguousNumberFormatError",
    "NotANumberError",
    "NumberFormat",
    "detect_number_format",
    "parse_amount",
]


class NumberFormat(StrEnum):
    DOT_DECIMAL = "DOT_DECIMAL"  # 1,234.56
    COMMA_DECIMAL = "COMMA_DECIMAL"  # 1.234,56
    PLAIN = "PLAIN"  # 1234.56 or 1234


class AmbiguousNumberFormatError(ValueError):
    """Raised when a column could be either separator convention."""


_CURRENCY_NOISE_RE = re.compile(r"[^0-9,.\-+()]")
_TRAILING_SIGN_RE = re.compile(r"^(?P<body>[0-9.,]+)(?P<sign>[-+])$")

# A cell is only treated as an amount if, after removing an optional currency
# symbol or 3-letter code, nothing alphabetic remains. Without this guard
# "INV-4930" strips down to "-4930" and silently becomes an amount, which is
# exactly the class of silent corruption the spec forbids.
# A currency code is only accepted when whitespace-separated, and a symbol only
# as a prefix. Anything glued directly to the digits ("INV-4930") is an
# identifier, not an amount.
_CURRENCY_AFFIX_RE = re.compile(
    r"^\s*(?:(?:[A-Za-z]{3})\s+|[^\w\s]\s*)?"
    r"(?P<body>[^A-Za-z]*?)"
    r"(?:\s+[A-Za-z]{3})?\s*$"
)
_ALPHA_RE = re.compile(r"[A-Za-z]")


class NotANumberError(ValueError):
    """Raised when a cell contains letters and is therefore not an amount."""


def _strip_noise(text: str) -> tuple[str, bool]:
    """Remove currency symbols/codes; return the body and whether it was negative.

    Raises :class:`NotANumberError` when alphabetic content survives, so callers
    never mistake an identifier for a number.
    """
    text = text.strip()
    negative = False

    if _ALPHA_RE.search(text):
        affix = _CURRENCY_AFFIX_RE.match(text)
        candidate = affix.group("body") if affix else text
        if _ALPHA_RE.search(candidate):
            raise NotANumberError(f"value contains letters and is not an amount: {text!r}")
        text = candidate

    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1]

    text = _CURRENCY_NOISE_RE.sub("", text)

    trailing = _TRAILING_SIGN_RE.match(text)
    if trailing:
        if trailing.group("sign") == "-":
            negative = True
        text = trailing.group("body")
    elif text.startswith("-"):
        negative = True
        text = text[1:]
    elif text.startswith("+"):
        text = text[1:]

    return text, negative


def detect_number_format(samples: list[str]) -> NumberFormat:
    """Infer the separator convention for a column of amounts."""
    cleaned = []
    for raw in samples:
        if raw is None:
            continue
        body, _ = _strip_noise(str(raw))
        if body:
            cleaned.append(body)
    if not cleaned:
        raise ValueError("no non-empty amount samples")

    comma_decimal = 0
    dot_decimal = 0
    for value in cleaned:
        last_comma = value.rfind(",")
        last_dot = value.rfind(".")
        if last_comma == -1 and last_dot == -1:
            continue
        if last_comma > last_dot:
            # The rightmost separator is the decimal separator only when it is
            # followed by 1-2 digits; otherwise it is a thousands separator.
            if len(value) - last_comma - 1 in (1, 2):
                comma_decimal += 1
            else:
                dot_decimal += 1
        elif last_dot > last_comma:
            if len(value) - last_dot - 1 in (1, 2):
                dot_decimal += 1
            else:
                comma_decimal += 1

    if comma_decimal and dot_decimal:
        raise AmbiguousNumberFormatError("amount column mixes 1.234,56 and 1,234.56 conventions")
    if comma_decimal:
        return NumberFormat.COMMA_DECIMAL
    if dot_decimal:
        return NumberFormat.DOT_DECIMAL
    return NumberFormat.PLAIN


def parse_amount(
    value: str | int | Decimal | None,
    number_format: NumberFormat = NumberFormat.DOT_DECIMAL,
    *,
    negate: bool = False,
) -> Decimal | None:
    """Parse one amount cell into ``Decimal``.

    ``float`` is deliberately not accepted: if a parser hands us a float the
    precision has already been lost upstream and we want to hear about it.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return -value if negate else value
    if isinstance(value, bool):
        raise TypeError("bool is not an amount")
    if isinstance(value, int):
        result = Decimal(value)
        return -result if negate else result
    if isinstance(value, float):
        raise TypeError("float amounts are rejected during ingestion")

    text = str(value).strip()
    if not text:
        return None

    body, negative = _strip_noise(text)
    if not body:
        return None

    if number_format is NumberFormat.COMMA_DECIMAL:
        body = body.replace(".", "").replace(",", ".")
    elif number_format is NumberFormat.DOT_DECIMAL:
        body = body.replace(",", "")
    else:  # PLAIN
        body = body.replace(",", "")

    try:
        result = Decimal(body)
    except InvalidOperation as exc:
        raise ValueError(f"cannot parse amount: {value!r}") from exc

    if negative:
        result = -result
    if negate:
        result = -result
    return result
