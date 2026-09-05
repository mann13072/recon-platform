"""Structured schemas for every AI response (spec sections 28 and 29).

Nothing a model returns is trusted until it validates against one of these. A
schema validation failure produces **no state change at all** - the platform
falls back to the deterministic answer and records the failure.

Every response carries the metadata the spec requires: provider, model, model
version, prompt version, input hash, latency and cost.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from packages.domain.dates import utc_now
from packages.domain.enums import ExceptionCategory

__all__ = [
    "AICallRecord",
    "AIResponseEnvelope",
    "EntityResolutionSuggestion",
    "EvidenceSummary",
    "ExceptionClassificationSuggestion",
    "ExtractedReferenceSuggestion",
    "InvestigationEvidence",
    "ResolutionProposal",
    "SchemaValidationFailure",
]


class SchemaValidationFailure(ValueError):
    """Raised when a model response does not validate.

    Carries the raw text so the failure can be logged and inspected, but the
    caller must not use it: an unvalidated response is not a result.
    """

    def __init__(self, message: str, raw: str | None = None) -> None:
        super().__init__(message)
        self.raw = raw


class _Cited(BaseModel):
    """Base for every suggestion: cite the fields that support the claim."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    confidence: float = Field(ge=0.0, le=1.0)
    reason_codes: tuple[str, ...] = ()
    human_explanation: str = ""
    cited_fields: tuple[str, ...] = ()

    @field_validator("human_explanation")
    @classmethod
    def _bounded_text(cls, value: str) -> str:
        # A long free-text field is where prompt-injected instructions would
        # travel. It is bounded and only ever rendered as text, never executed.
        if len(value) > 1200:
            raise ValueError("human_explanation exceeds 1200 characters")
        return value


class ExceptionClassificationSuggestion(_Cited):
    """Output of ``classify_exception`` (spec section 28)."""

    category: ExceptionCategory
    suggested_resolution: str = ""

    @field_validator("category", mode="before")
    @classmethod
    def _known_category(cls, value: Any) -> Any:
        # An unknown category becomes INSUFFICIENT_EVIDENCE rather than an
        # error: a model inventing a taxonomy entry must not break the pipeline,
        # but it must not extend the taxonomy either.
        if isinstance(value, str):
            try:
                return ExceptionCategory(value.upper())
            except ValueError:
                return ExceptionCategory.INSUFFICIENT_EVIDENCE
        return value


class EntityResolutionSuggestion(_Cited):
    """A proposed alias between an observed name and a canonical entity.

    Never applied automatically: entities are merged only after human approval
    (spec section 25).
    """

    observed_name: str
    canonical_entity: str
    is_same_entity: bool


class ExtractedReferenceSuggestion(_Cited):
    """A pattern the model believes it found in a description.

    Used to *propose* new deterministic regexes for review, not to extract
    references in production (spec section 24).
    """

    kind: str
    value: str
    proposed_pattern: str | None = None


class EvidenceSummary(_Cited):
    """A narrative summary of evidence the deterministic layer assembled."""

    summary: str
    key_points: tuple[str, ...] = ()


class ResolutionProposal(_Cited):
    """A suggested resolution for an exception. Advisory, never applied."""

    resolution_code: str
    steps: tuple[str, ...] = ()
    requires_journal_entry: bool = False
    estimated_amount: Decimal | None = None


class InvestigationEvidence(BaseModel):
    """The deterministic evidence bundle an investigation question is answered from.

    The LLM summarises *this*; it never queries the database itself
    (spec section 71).
    """

    model_config = ConfigDict(frozen=True)

    question: str
    difference: Decimal
    currency: str
    largest_open_exceptions: tuple[dict[str, Any], ...] = ()
    aged_items: tuple[dict[str, Any], ...] = ()
    possible_fee_items: tuple[dict[str, Any], ...] = ()
    unmatched_groups: tuple[dict[str, Any], ...] = ()


class AICallRecord(BaseModel):
    """Audit metadata for one AI call (spec section 29)."""

    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    task: str
    provider: str
    model_name: str
    model_version: str
    prompt_version: str
    input_hash: str
    policy: str
    redacted_field_count: int = 0
    latency_ms: float = 0.0
    cost_usd: Decimal = Decimal("0")
    schema_valid: bool = True
    failure_reason: str | None = None
    occurred_at: datetime = Field(default_factory=utc_now)
    user_decision: Literal["accepted", "rejected", "pending", "not_shown"] = "pending"
    output_summary: str = ""


class AIResponseEnvelope(BaseModel):
    """A validated suggestion plus its audit record.

    Callers receive this, never a bare model response, so there is no way to use
    an AI answer without also having its provenance.
    """

    model_config = ConfigDict(frozen=True)

    suggestion: (
        ExceptionClassificationSuggestion
        | EntityResolutionSuggestion
        | ExtractedReferenceSuggestion
        | EvidenceSummary
        | ResolutionProposal
        | None
    )
    call: AICallRecord

    @property
    def usable(self) -> bool:
        return self.suggestion is not None and self.call.schema_valid
