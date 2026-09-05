"""Stage 5 - feature extraction and evidence scoring (spec sections 17 and 18).

Two things are computed here and they are deliberately not the same number:

* **evidence score** - how much evidence supports this specific pairing;
* **confidence**     - a calibrated probability that the pairing is correct.

For the MVP confidence is a monotone transform of the evidence score. When a
trained classifier arrives (spec section 26) it replaces the transform, not the
features - which is why the features are stored on every candidate.

Ambiguity and risk are handled elsewhere: a high score is not permission to
auto-match.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from packages.domain.dates import DateField, date_distance_days
from packages.domain.models.matching import (
    CandidateMatch,
    MatchFeatures,
    MatchReason,
    MatchWarning,
    ScoredCandidate,
)
from packages.domain.models.reconciliation import ReconciliationConfig
from packages.domain.models.transaction import CanonicalTransaction
from packages.ingestion.normalization import description_contains
from packages.matching.explanations import build_reasons, build_warnings
from packages.matching.fuzzy import (
    jaro_winkler_similarity,
    token_set_ratio,
    trigram_similarity,
)
from packages.matching.rules import MatchingRule, evaluate_condition

__all__ = ["FeatureWeights", "Scorer", "extract_features"]


def extract_features(
    a: CanonicalTransaction,
    b: CanonicalTransaction,
    *,
    date_field: DateField = DateField.TRANSACTION,
) -> MatchFeatures:
    """Compute the deterministic feature vector for a pair.

    Every comparison treats a missing value as "no evidence", never as
    agreement.
    """
    amount_difference = abs(a.amount - b.amount)
    amount_difference_pct: Decimal | None = None
    if b.amount != 0:
        amount_difference_pct = amount_difference / abs(b.amount)

    # A processor payout matches a bank deposit on its *net* amount.
    net_exact = False
    for gross_side, net_side in ((a, b), (b, a)):
        if net_side.net_amount is not None and gross_side.amount == net_side.net_amount:
            net_exact = True
            break

    reference_exact = bool(
        a.normalized_reference
        and b.normalized_reference
        and a.normalized_reference == b.normalized_reference
    )
    reference_similarity = (
        jaro_winkler_similarity(a.normalized_reference, b.normalized_reference)
        if a.normalized_reference and b.normalized_reference
        else 0.0
    )

    invoice_exact = bool(
        a.normalized_invoice_number
        and b.normalized_invoice_number
        and a.normalized_invoice_number == b.normalized_invoice_number
    )
    # An invoice number recorded as a plain reference on the other side still counts.
    if not invoice_exact:
        invoice_exact = bool(
            (a.normalized_invoice_number and a.normalized_invoice_number == b.normalized_reference)
            or (b.normalized_invoice_number and b.normalized_invoice_number == a.normalized_reference)
        )

    counterparty_exact = bool(
        a.normalized_counterparty
        and b.normalized_counterparty
        and a.normalized_counterparty == b.normalized_counterparty
    )
    counterparty_similarity = (
        max(
            jaro_winkler_similarity(a.normalized_counterparty, b.normalized_counterparty),
            trigram_similarity(a.normalized_counterparty, b.normalized_counterparty),
        )
        if a.normalized_counterparty and b.normalized_counterparty
        else 0.0
    )

    settlement_exact = _identifier_pair_equal(
        (a.settlement_id, a.payout_id, a.batch_id),
        (b.settlement_id, b.payout_id, b.batch_id),
    )
    external_exact = bool(
        a.external_transaction_id
        and b.external_transaction_id
        and a.external_transaction_id.upper() == b.external_transaction_id.upper()
    )

    description_similarity = token_set_ratio(
        a.normalized_description, b.normalized_description
    )

    contains_reference = any(
        description_contains(source.description, identifier)
        for source, other in ((a, b), (b, a))
        for identifier in (
            other.settlement_id,
            other.payout_id,
            other.external_transaction_id,
            other.invoice_number,
            other.reference,
        )
    )

    return MatchFeatures(
        amount_exact=a.amount == b.amount,
        amount_difference=amount_difference,
        amount_difference_pct=amount_difference_pct,
        currency_exact=a.currency == b.currency,
        reference_exact=reference_exact,
        reference_similarity=reference_similarity,
        invoice_exact=invoice_exact,
        counterparty_exact=counterparty_exact,
        counterparty_similarity=counterparty_similarity,
        date_distance_days=date_distance_days(
            a.date_for(date_field) or a.best_date,
            b.date_for(date_field) or b.best_date,
        ),
        value_date_distance_days=date_distance_days(a.value_date, b.value_date),
        settlement_exact=settlement_exact,
        external_id_exact=external_exact,
        description_similarity=description_similarity,
        description_contains_reference=contains_reference,
        net_amount_exact=net_exact,
    )


def _identifier_pair_equal(
    left: tuple[str | None, ...],
    right: tuple[str | None, ...],
) -> bool:
    left_set = {v.upper() for v in left if v}
    right_set = {v.upper() for v in right if v}
    return bool(left_set & right_set)


@dataclass(frozen=True, slots=True)
class FeatureWeights:
    """Weights for the default scorer, in evidence points.

    Each weight is the value of one comparison *when both records carry the
    field*. Identifier evidence dominates; similarity-only evidence is small on
    purpose, so a fuzzy name plus a close amount cannot reach the auto-match
    band on its own.
    """

    external_id_exact: float = 45.0
    settlement_exact: float = 40.0
    reference_exact: float = 35.0
    invoice_exact: float = 35.0
    amount_exact: float = 25.0
    description_contains_reference: float = 15.0
    counterparty_exact: float = 10.0
    date_proximity: float = 10.0
    currency_exact: float = 5.0
    description_similarity: float = 5.0

    # Similarity below this contributes nothing; weak signals should not add up
    # into a confident-looking score.
    similarity_floor: float = 0.85

    # Added to the denominator when neither record carries any strong
    # identifier. Without it, a pair agreeing only on amount and date would
    # score 1.0 simply because there was nothing else to disagree about.
    # "Nothing contradicted this" is not the same as "this is proven".
    no_identifier_penalty: float = 45.0


# The evidence needed before a pairing may be considered for auto-match at all:
# at least one strong identifier, or an exact amount plus corroboration.
_STRONG_IDENTIFIER_FIELDS = (
    "external_id_exact",
    "settlement_exact",
    "reference_exact",
    "invoice_exact",
)


@dataclass(slots=True)
class Scorer:
    """Scores candidates, either against a rule or with the default weights."""

    config: ReconciliationConfig
    weights: FeatureWeights = FeatureWeights()

    def score_candidate(
        self,
        candidate: CandidateMatch,
        a: CanonicalTransaction,
        b: CanonicalTransaction,
        *,
        rule: MatchingRule | None = None,
    ) -> ScoredCandidate | None:
        """Score one candidate.

        Returns ``None`` when a rule's hard conditions are not satisfied, which
        means the rule simply does not apply to this pair.
        """
        features = extract_features(a, b, date_field=self.config.tolerances.date_field)

        if rule is not None:
            scored = self._score_with_rule(rule, a, b)
            if scored is None:
                return None
            raw_score, satisfied_codes = scored
            normalized = raw_score / rule.max_score if rule.max_score else 0.0
            reasons = build_reasons(features, satisfied_codes, a, b)
            precision = rule.risk.precision_estimate
            rule_id: str | None = rule.id
            rule_version: str | None = rule.version
        else:
            normalized = self._score_with_features(features, a, b)
            reasons = build_reasons(features, None, a, b)
            # The default scorer has no measured history, so it never clears the
            # rule-precision gate on its own; it can only produce suggestions.
            precision = 0.0
            rule_id = None
            rule_version = None

        warnings = build_warnings(features, a, b, self.config)

        return ScoredCandidate(
            candidate=candidate,
            features=features,
            score=round(min(normalized, 1.0), 6),
            reasons=reasons,
            warnings=warnings,
            hard_conflicts=sum(1 for w in warnings if w.code.startswith("CONFLICT_")),
            rule_id=rule_id,
            rule_version=rule_version,
            rule_precision_estimate=precision,
            total_amount=a.amount,
            currency=a.currency,
        )

    # -- rule scoring ------------------------------------------------------
    def _score_with_rule(
        self,
        rule: MatchingRule,
        a: CanonicalTransaction,
        b: CanonicalTransaction,
    ) -> tuple[float, set[str]] | None:
        if not self._passes_filters(rule, a, b):
            return None

        total = 0.0
        satisfied: set[str] = set()

        for condition in rule.conditions:
            ok, strength = evaluate_condition(condition, a, b)
            if not ok:
                if condition.hard:
                    return None
                continue
            satisfied.add(condition.reason_code)
            total += condition.weight * strength

        return total, satisfied

    def _passes_filters(
        self,
        rule: MatchingRule,
        a: CanonicalTransaction,
        b: CanonicalTransaction,
    ) -> bool:
        filters = rule.filters
        if filters.currency_equal and a.currency != b.currency:
            return False
        if filters.direction_opposite and (a.amount > 0) == (b.amount > 0):
            return False
        if filters.max_amount_difference is not None:
            if abs(a.amount - b.amount) > filters.max_amount_difference:
                return False
        if filters.date_window_days is not None:
            distance = date_distance_days(
                a.date_for(filters.date_field) or a.best_date,
                b.date_for(filters.date_field) or b.best_date,
            )
            if distance is None or distance > filters.date_window_days:
                return False
        return True

    # -- default feature scoring -------------------------------------------
    def _score_with_features(
        self,
        features: MatchFeatures,
        a: CanonicalTransaction,
        b: CanonicalTransaction,
    ) -> float:
        """Score as the fraction of *available* evidence that agrees.

        Normalising against the sum of every weight would be wrong: the signals
        are mutually exclusive in practice, so no real pair can earn them all
        and every score would be crushed toward zero. Instead a signal only
        enters the denominator when both records actually carry the field, so
        the score answers "of what could be compared, how much agreed?".

        A field present on one side and absent on the other is neither evidence
        for nor against, and is excluded from both sides of the ratio.
        """
        w = self.weights
        earned = 0.0
        available = 0.0

        def weigh(weight: float, comparable: bool, satisfied: float) -> None:
            nonlocal earned, available
            if not comparable:
                return
            available += weight
            earned += weight * satisfied

        weigh(
            w.external_id_exact,
            bool(a.external_transaction_id and b.external_transaction_id),
            1.0 if features.external_id_exact else 0.0,
        )
        weigh(
            w.settlement_exact,
            bool(
                (a.settlement_id or a.payout_id or a.batch_id)
                and (b.settlement_id or b.payout_id or b.batch_id)
            ),
            1.0 if features.settlement_exact else 0.0,
        )
        weigh(
            w.reference_exact,
            bool(a.normalized_reference and b.normalized_reference),
            1.0
            if features.reference_exact
            else (
                features.reference_similarity
                if features.reference_similarity >= w.similarity_floor
                else 0.0
            ),
        )
        weigh(
            w.invoice_exact,
            bool(a.normalized_invoice_number and b.normalized_invoice_number),
            1.0 if features.invoice_exact else 0.0,
        )

        # Amount and currency are always comparable: every transaction has them.
        amount_satisfied = 1.0 if features.amount_exact or features.net_amount_exact else 0.0
        if amount_satisfied == 0.0:
            limit = self.config.tolerances.max_amount_difference
            if limit > 0 and features.amount_difference <= limit:
                amount_satisfied = 1.0 - float(features.amount_difference / limit)
        weigh(w.amount_exact, True, amount_satisfied)
        weigh(w.currency_exact, True, 1.0 if features.currency_exact else 0.0)

        weigh(
            w.counterparty_exact,
            bool(a.normalized_counterparty and b.normalized_counterparty),
            1.0
            if features.counterparty_exact
            else (
                features.counterparty_similarity
                if features.counterparty_similarity >= w.similarity_floor
                else 0.0
            ),
        )
        weigh(
            w.description_similarity,
            bool(a.normalized_description and b.normalized_description),
            features.description_similarity
            if features.description_similarity >= w.similarity_floor
            else 0.0,
        )

        window = max(self.config.tolerances.date_days, 1)
        weigh(
            w.date_proximity,
            features.date_distance_days is not None,
            max(0.0, 1.0 - ((features.date_distance_days or 0) / (window + 1))),
        )

        # An identifier found inside the other side's narrative is pure upside:
        # it is only ever evidence for, never against.
        if features.description_contains_reference:
            earned += w.description_contains_reference
            available += w.description_contains_reference

        if not has_strong_identifier(features):
            available += w.no_identifier_penalty

        if available <= 0:
            return 0.0
        return earned / available


def has_strong_identifier(features: MatchFeatures) -> bool:
    """Whether at least one identifier matched exactly.

    The decision policy uses this as a precondition for auto-matching, so an
    accumulation of weak similarity can never reach the automatic band.
    """
    return any(getattr(features, name) for name in _STRONG_IDENTIFIER_FIELDS)
