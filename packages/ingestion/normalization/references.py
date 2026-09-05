"""Reference extraction from noisy bank narratives (spec section 24).

    "STRIPE PAYOUT STRP-48392 / ACME LTD"
        -> provider=STRIPE, reference=STRP-48392, entity=ACME LTD

Deterministic patterns first. AI may later *discover* new patterns, but once a
pattern is stable it is promoted into this file and stops costing an inference
call - which is also what makes it auditable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from packages.ingestion.normalization.text import normalize_name, normalize_reference

__all__ = [
    "EXTRACTOR_VERSION",
    "ExtractedReference",
    "ReferencePattern",
    "extract_references",
]

EXTRACTOR_VERSION = "reference_extractor_v1"


@dataclass(frozen=True, slots=True)
class ExtractedReference:
    kind: str
    value: str
    normalized: str
    provider: str | None = None
    pattern_id: str = ""
    span: tuple[int, int] = (0, 0)


@dataclass(frozen=True, slots=True)
class ReferencePattern:
    pattern_id: str
    kind: str
    regex: re.Pattern[str]
    provider: str | None = None
    group: str = "id"


# An identifier must contain at least one digit and be at least three characters
# long. Without that guard "STRIPE PAYOUT 8F42" yields the word PAYOUT as the
# reference, which then matches nothing and pollutes every downstream feature.
_ID_CORE = r"(?=[A-Z0-9-]{3,}\b)(?P<id>[A-Z0-9]*\d[A-Z0-9]*(?:-[A-Z0-9]+)*)"

# Optional processor keywords that sit between the brand and the identifier.
_PROCESSOR_FILLER = r"(?:\s*(?:PAYOUT|PAYOUTS|SETTLEMENT|BATCH|TRANSFER|DEPOSIT)\b)*[\s:/-]*"

PATTERNS: tuple[ReferencePattern, ...] = (
    ReferencePattern(
        "STRIPE_PAYOUT_V2",
        "settlement_id",
        re.compile(rf"\bSTRIPE\b{_PROCESSOR_FILLER}{_ID_CORE}", re.IGNORECASE),
        provider="STRIPE",
    ),
    ReferencePattern(
        "STRIPE_PREFIXED_ID_V1",
        "settlement_id",
        re.compile(r"\b(?P<id>STRP-?[A-Z0-9]{4,})\b", re.IGNORECASE),
        provider="STRIPE",
    ),
    ReferencePattern(
        "STRIPE_PAYOUT_OBJECT_V1",
        "settlement_id",
        re.compile(r"\b(?P<id>po_[A-Za-z0-9]{8,})\b"),
        provider="STRIPE",
    ),
    ReferencePattern(
        "PAYPAL_BATCH_V1",
        "settlement_id",
        re.compile(rf"\bPAYPAL\b{_PROCESSOR_FILLER}{_ID_CORE}", re.IGNORECASE),
        provider="PAYPAL",
    ),
    ReferencePattern(
        "ADYEN_BATCH_V1",
        "settlement_id",
        re.compile(rf"\bADYEN\b{_PROCESSOR_FILLER}{_ID_CORE}", re.IGNORECASE),
        provider="ADYEN",
    ),
    ReferencePattern(
        "INVOICE_V2",
        "invoice_number",
        # The keyword is part of the identifier: an accounting system storing
        # "INV-4930" must match a bank narrative saying "INV 4930".
        re.compile(
            r"\b(?P<id>(?:INV|INVOICE|RECHNUNG|FACTUUR)[-\s#]?[A-Z]?\d[A-Z0-9-]*)\b",
            re.IGNORECASE,
        ),
    ),
    ReferencePattern(
        "PURCHASE_ORDER_V1",
        "purchase_order",
        re.compile(r"\b(?:PO|P\.O\.|ORDER)[-\s#]?(?P<id>[A-Z0-9][A-Z0-9-]{2,})\b",
                   re.IGNORECASE),
    ),
    ReferencePattern(
        "CHECK_V1",
        "check_number",
        re.compile(r"\b(?:CHK|CHECK|CHEQUE)[-\s#]?(?P<id>\d{3,})\b", re.IGNORECASE),
    ),
    ReferencePattern(
        "IBAN_V1",
        "counterparty_account",
        re.compile(r"\b(?P<id>[A-Z]{2}\d{2}[A-Z0-9]{10,30})\b"),
    ),
    ReferencePattern(
        "END_TO_END_V1",
        "reference",
        re.compile(r"\bEREF[-:\s]?(?P<id>[A-Z0-9-]{4,})\b", re.IGNORECASE),
    ),
)

# An entity often trails the narrative after a separator.
_ENTITY_TAIL_RE = re.compile(r"[/|]\s*(?P<entity>[A-Za-z][A-Za-z0-9 .&'-]{2,})\s*$")


@dataclass(slots=True)
class ExtractionResult:
    references: list[ExtractedReference] = field(default_factory=list)
    provider: str | None = None
    entity: str | None = None

    def first(self, kind: str) -> ExtractedReference | None:
        for ref in self.references:
            if ref.kind == kind:
                return ref
        return None


def extract_references(description: str | None) -> ExtractionResult:
    """Pull structured identifiers out of a free-text description.

    Returns every match; the caller decides which kinds it cares about. Matches
    are returned in pattern order so the result is deterministic.
    """
    result = ExtractionResult()
    if not description:
        return result

    seen: set[tuple[str, str]] = set()
    for pattern in PATTERNS:
        for match in pattern.regex.finditer(description):
            raw = match.group(pattern.group)
            normalized = normalize_reference(raw)
            if not normalized:
                continue
            key = (pattern.kind, normalized)
            if key in seen:
                continue
            seen.add(key)
            result.references.append(
                ExtractedReference(
                    kind=pattern.kind,
                    value=raw,
                    normalized=normalized,
                    provider=pattern.provider,
                    pattern_id=pattern.pattern_id,
                    span=match.span(pattern.group),
                )
            )
            if pattern.provider and result.provider is None:
                result.provider = pattern.provider

    tail = _ENTITY_TAIL_RE.search(description)
    if tail:
        result.entity = normalize_name(tail.group("entity"))

    return result


def description_contains(description: str | None, identifier: str | None) -> bool:
    """Whether a normalised identifier appears inside a description.

    Used by the processor-settlement rule: "settlement/payout ID appears in the
    bank description" (spec section 13, rule C).
    """
    if not description or not identifier:
        return False
    needle = normalize_reference(identifier)
    haystack = normalize_reference(description)
    if not needle or not haystack:
        return False
    # Require a reasonably specific identifier so short tokens do not create
    # accidental hits inside long narratives.
    if len(needle) < 4:
        return False
    return needle in haystack
