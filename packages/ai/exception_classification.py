"""AI-assisted exception classification (spec sections 28 and 30).

The deterministic classifier runs first and always wins on the categories it can
decide. AI is consulted only when the deterministic answer is weak, and its
answer is attached as a *suggestion* - it never overwrites the category and
never advances the workflow.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from packages.ai.privacy import AIDisabledError, DataRegionViolation, TenantAISettings
from packages.ai.provider import AIProvider
from packages.ai.schemas import AIResponseEnvelope, ExceptionClassificationSuggestion
from packages.domain.models.exceptions import ExceptionRecord
from packages.domain.models.transaction import CanonicalTransaction
from packages.exceptions.classifier import Classification

__all__ = ["ClassificationOutcome", "attach_suggestion", "classify_with_ai"]

# Below this the deterministic classifier is admitting it does not know, so a
# semantic opinion is worth asking for.
DETERMINISTIC_CONFIDENCE_FLOOR = 0.75


@dataclass(slots=True)
class ClassificationOutcome:
    """What the platform will act on, plus what AI offered."""

    classification: Classification
    ai: AIResponseEnvelope | None = None
    ai_consulted: bool = False
    ai_skipped_reason: str | None = None

    @property
    def ai_suggestion(self) -> ExceptionClassificationSuggestion | None:
        if self.ai is None or not self.ai.usable:
            return None
        suggestion = self.ai.suggestion
        return (
            suggestion
            if isinstance(suggestion, ExceptionClassificationSuggestion)
            else None
        )


def classify_with_ai(
    deterministic: Classification,
    *,
    tenant_id: UUID,
    provider: AIProvider,
    settings: TenantAISettings,
    transaction: CanonicalTransaction | None = None,
    candidate: CanonicalTransaction | None = None,
) -> ClassificationOutcome:
    """Attach an AI opinion to a deterministic classification, where useful.

    The deterministic ``Classification`` is returned unchanged in every case.
    Nothing downstream reads the AI suggestion as authoritative.
    """
    if deterministic.confidence >= DETERMINISTIC_CONFIDENCE_FLOOR:
        return ClassificationOutcome(
            classification=deterministic,
            ai_skipped_reason=(
                "the deterministic classifier is confident, so no model call is needed"
            ),
        )

    payload: dict[str, Any] = {}
    if transaction is not None:
        payload.update(
            {
                "amount": str(transaction.amount),
                "currency": transaction.currency,
                "description": transaction.description,
                "transaction_date": (
                    transaction.transaction_date.isoformat()
                    if transaction.transaction_date
                    else None
                ),
                "status": transaction.status,
                "transaction_type": transaction.transaction_type,
                "reference": transaction.reference,
            }
        )
    if candidate is not None:
        payload.update(
            {
                "gross_amount": (
                    str(candidate.gross_amount) if candidate.gross_amount else None
                ),
                "fee_amount": (
                    str(candidate.fee_amount) if candidate.fee_amount else None
                ),
                "tax_amount": (
                    str(candidate.tax_amount) if candidate.tax_amount else None
                ),
                "net_amount": (
                    str(candidate.net_amount) if candidate.net_amount else None
                ),
                "settlement_id": candidate.settlement_id,
                "payout_id": candidate.payout_id,
            }
        )

    try:
        envelope = provider.classify_exception(tenant_id, payload, settings)
    except (AIDisabledError, DataRegionViolation) as exc:
        return ClassificationOutcome(
            classification=deterministic, ai_skipped_reason=str(exc)
        )

    return ClassificationOutcome(
        classification=deterministic, ai=envelope, ai_consulted=True
    )


def attach_suggestion(
    exception: ExceptionRecord,
    outcome: ClassificationOutcome,
) -> ExceptionRecord:
    """Record an AI suggestion on an exception without changing its state."""
    suggestion = outcome.ai_suggestion
    if suggestion is None or outcome.ai is None:
        return exception
    return exception.model_copy(
        update={
            "proposed_resolution": suggestion.suggested_resolution or None,
            "proposed_by_actor_type": "AI_ASSISTANT",
            "ai_suggestion_id": outcome.ai.call.id,
        }
    )
