"""Conservative text normalisation (spec section 15).

Deliberately *not* a single "clean the string" function. Identifiers depend on
punctuation, so a reference and a counterparty name get different treatment.
Every normaliser is versioned because lineage records which one produced a
canonical value (spec section 59).
"""

from __future__ import annotations

import re
import unicodedata

__all__ = [
    "NORMALIZER_VERSIONS",
    "normalize_bank_description",
    "normalize_invoice_number",
    "normalize_name",
    "normalize_reference",
    "normalize_text",
    "tokenize",
]

NORMALIZER_VERSIONS: dict[str, str] = {
    "normalize_text": "normalize_text_v1",
    "normalize_name": "normalize_name_v1",
    "normalize_reference": "normalize_reference_v2",
    "normalize_bank_description": "normalize_bank_description_v1",
    "normalize_invoice_number": "normalize_invoice_number_v1",
}

_WHITESPACE_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^A-Z0-9]+")

# Legal-form suffixes stripped from counterparty names before comparison.
# Kept short and explicit; a longer list belongs in tenant configuration.
_LEGAL_SUFFIXES = (
    "LIMITED",
    "LTD",
    "LLC",
    "INC",
    "CORP",
    "CORPORATION",
    "GMBH",
    "AG",
    "SARL",
    "SAS",
    "SA",
    "BV",
    "NV",
    "PLC",
    "PTY",
    "OY",
    "AB",
    "AS",
    "SRL",
    "SPA",
    "KG",
    "UG",
    "CO",
)

# Noise words that appear in nearly every bank narrative and carry no signal.
_BANK_NOISE = {
    "PAYMENT",
    "PAYMENTS",
    "TRANSFER",
    "TRF",
    "REF",
    "REFERENCE",
    "SEPA",
    "CT",
    "DD",
    "ACH",
    "WIRE",
    "CREDIT",
    "DEBIT",
    "TXN",
    "TRANSACTION",
    "FROM",
    "TO",
    "THE",
    "OF",
    "FOR",
    "AND",
}


def normalize_text(value: str | None) -> str | None:
    """Unicode-normalise, upper-case and collapse whitespace. Nothing else.

    Punctuation is preserved because identifiers may depend on it.
    """
    if not value:
        return None

    value = unicodedata.normalize("NFKC", value)
    value = value.upper().strip()
    value = _WHITESPACE_RE.sub(" ", value)
    return value or None


def normalize_name(value: str | None) -> str | None:
    """Normalise a counterparty/entity name for comparison.

    Strips accents and trailing legal forms so "Acme Ltd." and "ACME LIMITED"
    compare equal, but never merges distinct names.
    """
    text = normalize_text(value)
    if text is None:
        return None

    decomposed = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    text = re.sub(r"[.,()/\\-]+", " ", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()

    tokens = text.split(" ")
    while len(tokens) > 1 and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens) or None


def normalize_reference(value: str | None) -> str | None:
    """Normalise a payment reference to its comparable core.

    Separators inside identifiers are noise across systems ("INV-4930" vs
    "INV 4930" vs "inv4930"), so they are removed here - but only here, and the
    original is always kept on the transaction.
    """
    text = normalize_text(value)
    if text is None:
        return None
    text = _NON_ALNUM_RE.sub("", text)
    return text or None


def normalize_invoice_number(value: str | None) -> str | None:
    """Normalise an invoice number.

    Leading zeros in the numeric tail are dropped, because "INV-0042" and
    "INV-42" are the same invoice in every accounting system encountered so far.
    """
    text = normalize_reference(value)
    if text is None:
        return None
    match = re.match(r"^([A-Z]*)0*(\d+)$", text)
    if match:
        prefix, digits = match.groups()
        return f"{prefix}{digits}"
    return text


def normalize_bank_description(value: str | None) -> str | None:
    """Normalise a bank narrative for token-overlap comparison.

    Drops high-frequency banking noise words. Identifiers survive because they
    are not in the noise list.
    """
    text = normalize_text(value)
    if text is None:
        return None
    text = re.sub(r"[^A-Z0-9 ]+", " ", text)
    tokens = [t for t in text.split() if t and t not in _BANK_NOISE]
    return " ".join(tokens) or None


def tokenize(value: str | None) -> list[str]:
    """Split a normalised string into comparison tokens."""
    if not value:
        return []
    return [t for t in re.split(r"[^A-Z0-9]+", value.upper()) if t]
