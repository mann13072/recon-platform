"""Natural-language investigation (spec section 71).

The pattern is:

    Question -> governed query functions -> structured evidence -> LLM explanation

and explicitly **not**:

    Question -> LLM invents SQL -> direct financial database access

:func:`gather_evidence` is the governed layer. It takes typed arguments, returns
a typed bundle, and that bundle is the only thing the model ever sees.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from packages.ai.privacy import AIDisabledError, TenantAISettings
from packages.ai.provider import AIProvider
from packages.ai.schemas import EvidenceSummary, InvestigationEvidence
from packages.domain.models.exceptions import ExceptionRecord
from packages.domain.models.matching import MatchGroup
from packages.domain.models.transaction import CanonicalTransaction

__all__ = [
    "InvestigationAnswer",
    "deterministic_narrative",
    "gather_evidence",
    "investigate",
]

MAX_ITEMS_PER_SECTION = 10


@dataclass(slots=True)
class InvestigationAnswer:
    evidence: InvestigationEvidence
    narrative: str
    ai_generated: bool = False
    ai_call_id: UUID | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "question": self.evidence.question,
            "difference": str(self.evidence.difference),
            "currency": self.evidence.currency,
            "narrative": self.narrative,
            "ai_generated": self.ai_generated,
            "evidence": self.evidence.model_dump(mode="json"),
        }


def gather_evidence(
    question: str,
    *,
    difference: Decimal,
    currency: str,
    exceptions: list[ExceptionRecord],
    unmatched: list[CanonicalTransaction],
    match_groups: list[MatchGroup],
    aging_threshold_days: int = 30,
) -> InvestigationEvidence:
    """Assemble the deterministic evidence bundle for a question.

    Every number here is computed from stored state, never from a model.
    """
    ranked = sorted(
        exceptions,
        key=lambda e: (-(abs(e.amount_exposure or Decimal("0"))), str(e.id)),
    )

    largest = tuple(
        {
            "id": str(item.id),
            "category": item.category.value,
            "severity": item.severity.value,
            "amount": str(item.amount_exposure or Decimal("0")),
            "currency": item.currency or currency,
            "status": item.status.value,
            "detail": item.detail,
        }
        for item in ranked[:MAX_ITEMS_PER_SECTION]
    )

    aged = tuple(
        {
            "id": str(item.id),
            "category": item.category.value,
            "age_days": item.age_days,
            "amount": str(item.amount_exposure or Decimal("0")),
        }
        for item in ranked
        if item.age_days >= aging_threshold_days
    )[:MAX_ITEMS_PER_SECTION]

    fee_categories = {"PROCESSOR_FEE", "BANK_FEE", "ROUNDING_DIFFERENCE"}
    fees = tuple(
        {
            "id": str(item.id),
            "category": item.category.value,
            "amount": str(item.amount_exposure or Decimal("0")),
        }
        for item in ranked
        if item.category.value in fee_categories
    )[:MAX_ITEMS_PER_SECTION]

    groups = tuple(
        {
            "id": str(group.id),
            "cardinality": group.cardinality.value,
            "status": group.status.value,
            "amount": str(group.total_amount),
        }
        for group in sorted(match_groups, key=lambda g: str(g.id))
        if group.status.value in {"SUGGESTED", "PROPOSED"}
    )[:MAX_ITEMS_PER_SECTION]

    # Unmatched records reach the model only through the exceptions raised for
    # them, which carry the category and exposure a reviewer needs.
    del unmatched
    return InvestigationEvidence(
        question=question,
        difference=difference,
        currency=currency,
        largest_open_exceptions=largest,
        aged_items=aged,
        possible_fee_items=fees,
        unmatched_groups=groups,
    )


def deterministic_narrative(evidence: InvestigationEvidence) -> str:
    """A useful answer with no model involved at all.

    This is what the user sees when AI is disabled, and it is also the fallback
    when a model call fails or its response does not validate.
    """
    lines = [f"The unexplained difference is {evidence.currency} {evidence.difference}."]

    if evidence.largest_open_exceptions:
        top = evidence.largest_open_exceptions[0]
        lines.append(
            f"The largest open exception is {top['category']} for "
            f"{top['currency']} {top['amount']} ({top['status']})."
        )
        lines.append(
            f"There are {len(evidence.largest_open_exceptions)} open exception(s) in the shortlist."
        )
    if evidence.possible_fee_items:
        lines.append(
            f"{len(evidence.possible_fee_items)} item(s) look like fee or rounding differences."
        )
    if evidence.aged_items:
        lines.append(f"{len(evidence.aged_items)} item(s) are aged beyond the threshold.")
    if evidence.unmatched_groups:
        lines.append(
            f"{len(evidence.unmatched_groups)} suggested match group(s) are still awaiting review."
        )
    return " ".join(lines)


def investigate(
    evidence: InvestigationEvidence,
    *,
    tenant_id: UUID,
    provider: AIProvider | None = None,
    settings: TenantAISettings | None = None,
) -> InvestigationAnswer:
    """Answer a question from the evidence bundle, using AI only to phrase it."""
    fallback = deterministic_narrative(evidence)

    if provider is None or settings is None:
        return InvestigationAnswer(evidence=evidence, narrative=fallback)

    try:
        envelope = provider.summarize_evidence(
            tenant_id,
            {
                "question": evidence.question,
                "difference": str(evidence.difference),
                "currency": evidence.currency,
                "largest_open_exceptions": list(evidence.largest_open_exceptions),
                "aged_items": list(evidence.aged_items),
                "possible_fee_items": list(evidence.possible_fee_items),
                "unmatched_groups": list(evidence.unmatched_groups),
            },
            settings,
        )
    except AIDisabledError:
        return InvestigationAnswer(evidence=evidence, narrative=fallback)

    if envelope.usable and isinstance(envelope.suggestion, EvidenceSummary):
        return InvestigationAnswer(
            evidence=evidence,
            narrative=envelope.suggestion.summary,
            ai_generated=True,
            ai_call_id=envelope.call.id,
        )

    return InvestigationAnswer(evidence=evidence, narrative=fallback)
