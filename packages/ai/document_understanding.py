"""Document extraction (spec section 93).

    upload -> OCR/parser -> structured fields -> per-field confidence
           -> human verification if material -> attach to exception

OCR output is never accounting truth. Every extracted field carries its own
confidence, and anything material is queued for human verification before it can
be used.

The OCR engine itself is a bought component (spec section 96), so it sits behind
:class:`DocumentParser` rather than being implemented here.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

__all__ = [
    "DocumentParser",
    "ExtractedDocument",
    "ExtractedField",
    "NullDocumentParser",
    "to_evidence_metadata",
    "verification_required",
]

# Below this, a field is never used without a human confirming it.
FIELD_CONFIDENCE_FLOOR = 0.95


@dataclass(frozen=True, slots=True)
class ExtractedField:
    name: str
    value: str
    confidence: float
    page: int | None = None
    bounding_box: tuple[float, float, float, float] | None = None

    @property
    def is_confident(self) -> bool:
        return self.confidence >= FIELD_CONFIDENCE_FLOOR


@dataclass(slots=True)
class ExtractedDocument:
    document_type: str
    fields: list[ExtractedField] = field(default_factory=list)
    raw_text: str = ""
    parser: str = "none"
    parser_version: str = "none"

    def get(self, name: str) -> ExtractedField | None:
        for item in self.fields:
            if item.name == name:
                return item
        return None

    def low_confidence_fields(self) -> list[ExtractedField]:
        return [f for f in self.fields if not f.is_confident]


class DocumentParser(ABC):
    """Contract for an OCR / document-understanding backend."""

    name: str = "abstract"
    version: str = "none"

    @abstractmethod
    def parse(self, data: bytes, mime_type: str) -> ExtractedDocument:
        ...


class NullDocumentParser(DocumentParser):
    """The default: extracts nothing, and says so.

    Document AI is a Phase 2 capability. Shipping a parser that quietly returned
    empty fields would be worse than one that states plainly that no backend is
    configured.
    """

    name = "null"

    def parse(self, data: bytes, mime_type: str) -> ExtractedDocument:
        del data, mime_type
        return ExtractedDocument(
            document_type="unknown",
            fields=[],
            parser=self.name,
            parser_version=self.version,
        )


def verification_required(
    document: ExtractedDocument,
    *,
    materiality_threshold: Decimal,
) -> tuple[bool, list[str]]:
    """Whether a human must verify this extraction before it is used.

    Returns the decision and the reasons, so the UI can tell the reviewer what
    to look at rather than just demanding a click.
    """
    reasons: list[str] = []

    low = document.low_confidence_fields()
    if low:
        reasons.append("low extraction confidence on: " + ", ".join(f.name for f in low))

    amount_field = document.get("total_amount") or document.get("amount")
    if amount_field is not None:
        try:
            amount = Decimal(amount_field.value.replace(",", ""))
        except (ValueError, ArithmeticError):
            reasons.append(
                f"the extracted amount '{amount_field.value}' is not a number"
            )
        else:
            if abs(amount) >= materiality_threshold:
                reasons.append(
                    f"the extracted amount {amount} is at or above the materiality "
                    f"threshold of {materiality_threshold}"
                )

    if not document.fields:
        reasons.append("no fields were extracted")

    return bool(reasons), reasons


def to_evidence_metadata(document: ExtractedDocument) -> dict[str, Any]:
    """Extraction metadata to store alongside the evidence file."""
    return {
        "document_type": document.document_type,
        "parser": document.parser,
        "parser_version": document.parser_version,
        "field_count": len(document.fields),
        "low_confidence_field_count": len(document.low_confidence_fields()),
        "fields": {
            f.name: {"value": f.value, "confidence": f.confidence}
            for f in document.fields
        },
    }
