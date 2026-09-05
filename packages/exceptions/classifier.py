"""Deterministic exception classification (spec section 30).

This runs before any AI is consulted. Most exception categories are decidable
from arithmetic - a fee difference, a rounding difference, a timing difference -
and where they are, deciding them deterministically is faster, cheaper, exactly
reproducible and auditable.

AI is asked only about what remains genuinely semantic, and even then its answer
is a suggestion (see ``packages.ai.exception_classification``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from packages.controls.materiality import MaterialityPolicy, severity_for
from packages.domain.dates import utc_now
from packages.domain.enums import ExceptionCategory, ExceptionSeverity
from packages.domain.models.exceptions import ExceptionRecord
from packages.domain.models.reconciliation import ReconciliationConfig
from packages.domain.models.transaction import CanonicalTransaction
from packages.matching.tolerances import fee_explains_difference

__all__ = [
    "Classification",
    "ExceptionClassifier",
    "classify_pair",
    "classify_unmatched",
]


@dataclass(frozen=True, slots=True)
class Classification:
    category: ExceptionCategory
    confidence: float
    reason_codes: tuple[str, ...]
    detail: str
    requires_approval: bool = False

    @property
    def is_confident(self) -> bool:
        return self.confidence >= 0.9


def classify_pair(
    a: CanonicalTransaction,
    b: CanonicalTransaction,
    config: ReconciliationConfig,
) -> Classification:
    """Classify why two records that look related did not match cleanly."""
    difference = a.amount - b.amount
    magnitude = abs(difference)
    tolerances = config.tolerances

    if a.currency != b.currency:
        return Classification(
            ExceptionCategory.FX_DIFFERENCE,
            0.95,
            ("CURRENCY_MISMATCH",),
            f"The records are in different currencies ({a.currency} and "
            f"{b.currency}). An FX rate and rate source are required before "
            "they can be compared.",
        )

    # Processor fee: the difference is exactly the reported fee.
    for gross_side, other in ((a, b), (b, a)):
        if fee_explains_difference(
            gross_side.gross_amount, other.amount, gross_side.fee_amount
        ):
            return Classification(
                ExceptionCategory.PROCESSOR_FEE,
                0.97,
                ("NET_AMOUNT_MATCH", "FEE_EXPLAINS_DIFFERENCE"),
                f"The difference of {magnitude} equals the reported processor fee "
                f"of {gross_side.fee_amount}.",
            )

    if magnitude > 0 and (a.fee_amount is not None or b.fee_amount is not None):
        reported_fee = a.fee_amount or b.fee_amount or Decimal("0")
        fee_gap = abs(abs(reported_fee) - magnitude)
        if fee_gap <= tolerances.max_amount_difference and magnitude > 0:
            return Classification(
                ExceptionCategory.PROCESSOR_FEE,
                0.9,
                ("FEE_EXPLAINS_DIFFERENCE",),
                f"The {magnitude} difference is consistent with the reported fee "
                f"of {reported_fee}.",
            )
        if magnitude < abs(reported_fee):
            return Classification(
                ExceptionCategory.PROCESSOR_FEE,
                0.75,
                ("FEE_PARTIALLY_EXPLAINS_DIFFERENCE",),
                f"The difference of {magnitude} is smaller than the reported fee "
                f"of {reported_fee}; the fee postings may disagree.",
            )

    # Rounding: sub-minor-unit noise.
    if 0 < magnitude <= Decimal("0.05"):
        return Classification(
            ExceptionCategory.ROUNDING_DIFFERENCE,
            0.9,
            ("SMALL_DIFFERENCE",),
            f"The records differ by {a.currency} {magnitude}, consistent with a "
            "rounding or fee-posting difference.",
        )

    # Timing: same amount, dates outside the window.
    if magnitude == 0:
        distance = None
        if a.best_date and b.best_date:
            distance = abs((a.best_date - b.best_date).days)
        if distance is not None and distance > tolerances.date_days:
            return Classification(
                ExceptionCategory.TIMING_DIFFERENCE,
                0.92,
                ("AMOUNT_EXACT", "DATE_OUTSIDE_WINDOW"),
                f"The amounts are identical but the records are {distance} days "
                f"apart, beyond the {tolerances.date_days}-day window.",
            )
        if (
            a.normalized_reference
            and b.normalized_reference
            and a.normalized_reference != b.normalized_reference
        ):
            return Classification(
                ExceptionCategory.WRONG_REFERENCE,
                0.85,
                ("AMOUNT_EXACT", "REFERENCE_DISAGREES"),
                f"The amounts are identical but the references differ "
                f"({a.reference} vs {b.reference}).",
            )

    # Partial settlement.
    if magnitude > 0 and abs(b.amount) > 0:
        ratio = magnitude / abs(b.amount)
        if difference * (1 if b.amount > 0 else -1) < 0 and ratio < Decimal("0.5"):
            return Classification(
                ExceptionCategory.PARTIAL_PAYMENT,
                0.7,
                ("AMOUNT_SHORT",),
                f"The received amount is {magnitude} short of the expected "
                f"{b.amount}, which is consistent with a partial payment.",
            )
        if difference * (1 if b.amount > 0 else -1) > 0:
            return Classification(
                ExceptionCategory.OVERPAYMENT,
                0.7,
                ("AMOUNT_OVER",),
                f"The received amount exceeds the expected {b.amount} by "
                f"{magnitude}.",
            )

    return Classification(
        ExceptionCategory.WRONG_AMOUNT,
        0.5,
        ("AMOUNT_DISAGREES",),
        f"The records differ by {a.currency} {magnitude} for no deterministic "
        "reason found so far.",
    )


def classify_unmatched(
    transaction: CanonicalTransaction,
    side: str,
    config: ReconciliationConfig,
    *,
    duplicate_of: UUID | None = None,
) -> Classification:
    """Classify a transaction that found no partner at all."""
    if duplicate_of is not None:
        return Classification(
            ExceptionCategory.DUPLICATE_RECORD,
            0.95,
            ("DUPLICATE_CHECKSUM",),
            "An identical record already exists in this reconciliation.",
        )

    description = (transaction.normalized_description or "").upper()

    if any(token in description for token in ("REFUND", "RETURN", "GUTSCHRIFT")):
        return Classification(
            ExceptionCategory.REFUND,
            0.75,
            ("DESCRIPTION_INDICATES_REFUND",),
            "The description indicates a refund with no counterpart recorded.",
        )
    if any(token in description for token in ("CHARGEBACK", "DISPUTE")):
        return Classification(
            ExceptionCategory.CHARGEBACK,
            0.8,
            ("DESCRIPTION_INDICATES_CHARGEBACK",),
            "The description indicates a chargeback with no counterpart recorded.",
        )
    if any(token in description for token in ("REVERSAL", "REVERSED", "STORNO")):
        return Classification(
            ExceptionCategory.REVERSAL,
            0.8,
            ("DESCRIPTION_INDICATES_REVERSAL",),
            "The description indicates a reversal with no counterpart recorded.",
        )
    if any(token in description for token in ("FEE", "CHARGE", "COMMISSION", "GEBUEHR")):
        category = (
            ExceptionCategory.BANK_FEE if side == "A" else ExceptionCategory.PROCESSOR_FEE
        )
        return Classification(
            category,
            0.75,
            ("DESCRIPTION_INDICATES_FEE",),
            "The description indicates a fee that has not been posted on the "
            "other side.",
        )
    if any(token in description for token in ("WITHHOLDING", "WHT", "TAX")):
        return Classification(
            ExceptionCategory.WITHHOLDING_TAX,
            0.7,
            ("DESCRIPTION_INDICATES_TAX",),
            "The description indicates withholding tax with no counterpart.",
        )

    if transaction.best_date is None:
        return Classification(
            ExceptionCategory.DATA_QUALITY,
            0.9,
            ("NO_USABLE_DATE",),
            "The record has no usable date, so date-based rules could not apply "
            "to it.",
        )

    if not (
        transaction.reference
        or transaction.invoice_number
        or transaction.external_transaction_id
        or transaction.settlement_id
    ):
        return Classification(
            ExceptionCategory.DATA_QUALITY,
            0.6,
            ("NO_IDENTIFIER",),
            "The record carries no reference, invoice number or external "
            "identifier, so no identifier-based rule could apply to it.",
        )

    category = (
        ExceptionCategory.MISSING_LEDGER_RECORD
        if side == "A"
        else ExceptionCategory.MISSING_BANK_RECORD
    )
    return Classification(
        category,
        0.6,
        ("NO_ELIGIBLE_CANDIDATE",),
        "No eligible counterpart was found within the configured tolerances.",
    )


@dataclass(slots=True)
class ExceptionClassifier:
    """Turns engine output into exception records."""

    config: ReconciliationConfig
    materiality: MaterialityPolicy
    default_due_days: int = 7

    def build(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        transactions: list[CanonicalTransaction],
        classification: Classification,
    ) -> ExceptionRecord:
        exposure = sum((abs(tx.amount) for tx in transactions), Decimal("0"))
        currency = transactions[0].currency if transactions else None
        severity = severity_for(exposure, self.materiality)

        due_days = {
            ExceptionSeverity.CRITICAL: 1,
            ExceptionSeverity.HIGH: 3,
            ExceptionSeverity.MEDIUM: self.default_due_days,
            ExceptionSeverity.LOW: self.default_due_days * 2,
        }[severity]

        detected = utc_now()
        return ExceptionRecord(
            id=uuid4(),
            tenant_id=tenant_id,
            reconciliation_run_id=run_id,
            transaction_ids=tuple(tx.id for tx in transactions),
            category=classification.category,
            severity=severity,
            amount_exposure=exposure,
            currency=currency,
            first_detected_at=detected,
            due_at=detected + timedelta(days=due_days),
            title=_title_for(classification.category, exposure, currency),
            detail=classification.detail,
            reason_codes=classification.reason_codes,
            requires_approval=(
                classification.requires_approval
                or severity in {ExceptionSeverity.HIGH, ExceptionSeverity.CRITICAL}
            ),
        )


def _title_for(
    category: ExceptionCategory, exposure: Decimal, currency: str | None
) -> str:
    label = category.value.replace("_", " ").title()
    if currency:
        return f"{label} - {currency} {exposure:,.2f}"
    return label
