"""Human-readable evidence codes for every suggestion (spec section 40).

The UI renders these directly and the audit export contains them verbatim. A
match the platform cannot explain in these terms is a match it should not have
made.

Two sources of evidence are merged:

* what the *features* observe about the pair;
* what the *rule* asserted in order to fire.

Both are reported. A rule condition that fired on evidence the generic feature
set does not model - "the payout ID appears inside the bank narrative", say -
would otherwise be invisible to the reviewer, which defeats the point.
"""

from __future__ import annotations

from decimal import Decimal

from packages.domain.models.matching import MatchFeatures, MatchReason, MatchWarning
from packages.domain.models.reconciliation import ReconciliationConfig
from packages.domain.models.transaction import CanonicalTransaction

__all__ = ["REASON_CATALOGUE", "build_reasons", "build_warnings"]


# code -> (contribution, template). ``contribution`` is the share of the
# evidence a reader should attribute to this signal. It is presentational; the
# score itself comes from the scorer.
REASON_CATALOGUE: dict[str, tuple[float, str]] = {
    "EXTERNAL_ID_EXACT": (0.45, "External transaction ID {external_id} is identical."),
    "SETTLEMENT_REFERENCE_EXACT": (
        0.40,
        "Settlement or payout ID {settlement} is identical.",
    ),
    "REFERENCE_EXACT": (0.30, "Payment reference {reference} is identical."),
    "INVOICE_EXACT": (0.30, "Invoice number {invoice} matches."),
    "AMOUNT_EXACT": (0.35, "Amounts are exactly equal: {currency} {amount}."),
    "NET_AMOUNT_EXACT": (
        0.30,
        "The {currency} {amount} deposit equals the counterparty record's net amount after fees.",
    ),
    "AMOUNT_WITHIN_TOLERANCE": (
        0.10,
        "Amounts differ by {currency} {difference}, within the configured tolerance.",
    ),
    "CURRENCY_EXACT": (0.05, "Both records are in {currency}."),
    "DATE_SAME_DAY": (0.15, "Both records fall on {date_a}."),
    "DATE_WITHIN_1_DAY": (0.15, "Dates differ by one day ({date_a} and {date_b})."),
    "DATE_WITHIN_WINDOW": (0.10, "Dates differ by {days} days, within the window."),
    "REFERENCE_SIMILAR": (0.10, "References are {similarity} similar."),
    "COUNTERPARTY_EXACT": (
        0.10,
        "The counterparty names both normalise to {counterparty}.",
    ),
    "COUNTERPARTY_ALIAS_MATCH": (0.10, "The names map to the same approved counterparty."),
    "COUNTERPARTY_SIMILAR": (0.06, "Counterparty names are {similarity} similar."),
    "DESCRIPTION_CONTAINS_REFERENCE": (
        0.15,
        'The description "{description}" contains the counterparty '
        "record's identifier {settlement}.",
    ),
    "DESCRIPTION_SIMILAR": (0.05, "Descriptions share {similarity} of their tokens."),
    "GROUP_SUM_EXACT": (
        0.50,
        "The grouped records sum exactly to the anchor transaction's amount.",
    ),
    "GROUP_SEARCH_EXHAUSTIVE": (
        0.30,
        "The bounded search found exactly one combination that works.",
    ),
    "GROUP_SHARED_BATCH": (
        0.20,
        "Every grouped record carries the same batch as the anchor.",
    ),
    "FEATURE_SCORE": (0.0, "Scored on deterministic features."),
}


def build_reasons(
    features: MatchFeatures,
    satisfied_codes: set[str] | None,
    a: CanonicalTransaction | None = None,
    b: CanonicalTransaction | None = None,
) -> tuple[MatchReason, ...]:
    """Turn a feature vector and a rule's satisfied conditions into evidence codes.

    Codes are emitted in catalogue-independent, deterministic order: identifier
    evidence, then amount, then currency, then date, then similarity.
    """
    params = _params(a, b, features)
    emitted: dict[str, MatchReason] = {}

    def emit(code: str) -> None:
        if code in emitted:
            return
        contribution, template = REASON_CATALOGUE[code]
        try:
            description = template.format(**params)
        except (KeyError, IndexError, ValueError):  # pragma: no cover - defensive
            description = template
        emitted[code] = MatchReason(code=code, contribution=contribution, description=description)

    # -- evidence the features observed ------------------------------------
    if features.external_id_exact:
        emit("EXTERNAL_ID_EXACT")
    if features.settlement_exact:
        emit("SETTLEMENT_REFERENCE_EXACT")
    if features.reference_exact:
        emit("REFERENCE_EXACT")
    if features.invoice_exact:
        emit("INVOICE_EXACT")

    if features.amount_exact:
        emit("AMOUNT_EXACT")
    elif features.net_amount_exact:
        emit("NET_AMOUNT_EXACT")
    elif features.amount_difference > 0:
        emit("AMOUNT_WITHIN_TOLERANCE")

    if features.currency_exact:
        emit("CURRENCY_EXACT")

    if features.date_distance_days == 0:
        emit("DATE_SAME_DAY")
    elif features.date_distance_days == 1:
        emit("DATE_WITHIN_1_DAY")
    elif features.date_distance_days is not None:
        emit("DATE_WITHIN_WINDOW")

    if features.description_contains_reference:
        emit("DESCRIPTION_CONTAINS_REFERENCE")
    if not features.reference_exact and features.reference_similarity >= 0.85:
        emit("REFERENCE_SIMILAR")
    if features.counterparty_exact:
        emit("COUNTERPARTY_EXACT")
    elif features.counterparty_similarity >= 0.85:
        emit("COUNTERPARTY_SIMILAR")
    if features.description_similarity >= 0.85:
        emit("DESCRIPTION_SIMILAR")

    # -- evidence the rule asserted ----------------------------------------
    # A rule may fire on something the generic feature set does not model. Those
    # conditions are reported too, so the reviewer sees the actual basis of the
    # decision rather than a subset of it.
    for code in sorted(satisfied_codes or ()):
        if code in REASON_CATALOGUE:
            emit(code)

    return tuple(emitted.values())


def _params(
    a: CanonicalTransaction | None,
    b: CanonicalTransaction | None,
    features: MatchFeatures,
) -> dict[str, object]:
    """Values available to every reason template.

    Missing values render as a dash rather than an empty string, so a
    half-filled sentence never reaches a reviewer.
    """
    dash = "-"

    def pick(*values: object) -> object:
        for value in values:
            if value:
                return value
        return dash

    similarity = max(
        features.reference_similarity,
        features.counterparty_similarity,
        features.description_similarity,
    )

    currency = a.currency if a else (b.currency if b else dash)
    amount = f"{abs(a.amount):,.2f}" if a else dash

    return {
        "external_id": pick(
            a.external_transaction_id if a else None,
            b.external_transaction_id if b else None,
        ),
        "settlement": pick(
            b.settlement_id if b else None,
            b.payout_id if b else None,
            a.settlement_id if a else None,
            a.payout_id if a else None,
            b.external_transaction_id if b else None,
        ),
        "reference": pick(a.reference if a else None, b.reference if b else None),
        "invoice": pick(a.invoice_number if a else None, b.invoice_number if b else None),
        "counterparty": pick(
            a.normalized_counterparty if a else None,
            b.normalized_counterparty if b else None,
        ),
        "description": _truncate(pick(a.description if a else None, dash)),
        "currency": currency,
        "amount": amount,
        "difference": f"{features.amount_difference:,.2f}",
        "days": features.date_distance_days if features.date_distance_days is not None else dash,
        "date_a": (a.best_date.isoformat() if a and a.best_date else dash),
        "date_b": (b.best_date.isoformat() if b and b.best_date else dash),
        "similarity": f"{similarity:.0%}",
    }


def _truncate(value: object, limit: int = 60) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def build_warnings(
    features: MatchFeatures,
    a: CanonicalTransaction,
    b: CanonicalTransaction,
    config: ReconciliationConfig,
) -> tuple[MatchWarning, ...]:
    """Signals that argue against the match.

    ``CONFLICT_`` codes are hard conflicts: the decision policy refuses to
    auto-match when any is present, regardless of score.
    """
    warnings: list[MatchWarning] = []

    if a.currency != b.currency:
        warnings.append(
            MatchWarning(
                code="CONFLICT_CURRENCY_MISMATCH",
                description=f"Currencies differ: {a.currency} vs {b.currency}.",
            )
        )

    if _identifiers_disagree(
        a.normalized_reference, b.normalized_reference, features.reference_exact
    ):
        # A reference disagreement is only a *hard* conflict when nothing
        # stronger has already agreed. Systems routinely put different things in
        # a "reference" field - a bank puts the payment reference, a ledger puts
        # a memo - so when the invoice or external ID matches exactly, the
        # differing references are two conventions, not a contradiction.
        corroborated = (
            features.invoice_exact or features.external_id_exact or features.settlement_exact
        )
        warnings.append(
            MatchWarning(
                code=("REFERENCE_DIFFERS" if corroborated else "CONFLICT_REFERENCE_DISAGREES"),
                description=(
                    f"The records carry different references: "
                    f"{a.reference} vs {b.reference}."
                    + (
                        " A stronger identifier matched exactly, so this is "
                        "reported rather than treated as a contradiction."
                        if corroborated
                        else ""
                    )
                ),
            )
        )

    if _identifiers_disagree(
        a.normalized_invoice_number, b.normalized_invoice_number, features.invoice_exact
    ):
        warnings.append(
            MatchWarning(
                code="CONFLICT_INVOICE_DISAGREES",
                description=(
                    f"Both records carry an invoice number and they differ: "
                    f"{a.invoice_number} vs {b.invoice_number}."
                ),
            )
        )

    if a.external_transaction_id and b.external_transaction_id and not features.external_id_exact:
        warnings.append(
            MatchWarning(
                code="CONFLICT_EXTERNAL_ID_DISAGREES",
                description="Both records carry an external transaction ID and they differ.",
            )
        )

    if features.date_distance_days is None:
        warnings.append(
            MatchWarning(
                code="NO_COMPARABLE_DATE",
                description=(
                    "At least one record has no usable date, so the date "
                    "tolerance could not be applied."
                ),
            )
        )
    elif features.date_distance_days > config.tolerances.date_days:
        warnings.append(
            MatchWarning(
                code="DATE_OUTSIDE_WINDOW",
                description=(
                    f"Dates differ by {features.date_distance_days} days, beyond the "
                    f"{config.tolerances.date_days}-day window."
                ),
            )
        )

    if not features.amount_exact and not features.net_amount_exact:
        difference: Decimal = features.amount_difference
        if difference > config.tolerances.max_amount_difference:
            warnings.append(
                MatchWarning(
                    code="AMOUNT_OUTSIDE_TOLERANCE",
                    description=(
                        f"Amounts differ by {difference}, beyond the configured "
                        f"tolerance of {config.tolerances.max_amount_difference}."
                    ),
                )
            )

    if abs(a.amount) >= config.controls.materiality_threshold:
        warnings.append(
            MatchWarning(
                code="ABOVE_MATERIALITY",
                description=(
                    f"Amount {a.currency} {abs(a.amount):,.2f} is at or above the "
                    f"materiality threshold of {config.controls.materiality_threshold}."
                ),
            )
        )

    return tuple(warnings)


def _identifiers_disagree(left: str | None, right: str | None, matched: bool) -> bool:
    """Both sides carry an identifier-shaped value, and the two differ.

    Two missing values are silence, not disagreement. Free text is also not
    disagreement: a ledger memo reading "Customer payment Acme" does not
    contradict a bank reference of "INV-4930", it simply is not an identifier.
    Requiring a digit is a crude test but a reliable one - every reference,
    invoice number and transaction ID encountered in practice contains one.
    """
    if not (left and right) or matched or left == right:
        return False
    return _looks_like_identifier(left) and _looks_like_identifier(right)


def _looks_like_identifier(value: str) -> bool:
    return any(character.isdigit() for character in value)
