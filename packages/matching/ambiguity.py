"""Stage 6 - ambiguity analysis, and Stage 7 - the decision policy.

Spec sections 18 and 19. The claim this module exists to enforce:

    Candidate A score: 96
    Candidate B score: 95

    A score of 96 should NOT auto-match if another candidate scores 95.

Confidence and ambiguity are different quantities. A pairing can be individually
convincing and still be unsafe to automate, because a nearly identical pairing
exists. Precision is optimised ahead of automation rate.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from uuid import UUID

from packages.domain.enums import DecisionOutcome
from packages.domain.models.matching import DecisionResult, ScoredCandidate
from packages.domain.models.reconciliation import ReconciliationConfig
from packages.matching.scoring import has_strong_identifier

__all__ = ["AmbiguityAnalysis", "AmbiguityAnalyzer", "PolicyEngine", "decide"]


@dataclass(slots=True)
class AmbiguityAnalysis:
    """The candidate set for one anchor transaction, ranked and analysed."""

    anchor_id: UUID
    ranked: list[ScoredCandidate] = field(default_factory=list)

    @property
    def top(self) -> ScoredCandidate | None:
        return self.ranked[0] if self.ranked else None

    @property
    def second(self) -> ScoredCandidate | None:
        return self.ranked[1] if len(self.ranked) > 1 else None

    @property
    def margin(self) -> float:
        if not self.ranked:
            return 0.0
        top = self.ranked[0].score
        second = self.ranked[1].score if len(self.ranked) > 1 else 0.0
        return round(top - second, 6)

    @property
    def candidate_count(self) -> int:
        return len(self.ranked)

    @property
    def competing_count(self) -> int:
        """Candidates other than the top one that are themselves plausible."""
        if len(self.ranked) < 2:
            return 0
        top = self.ranked[0].score
        # Anything within 20% of the leader's score is a genuine competitor
        # rather than a long-tail near-miss.
        return sum(1 for c in self.ranked[1:] if c.score >= top * 0.8)


@dataclass(slots=True)
class AmbiguityAnalyzer:
    """Groups scored candidates by anchor and ranks them deterministically."""

    def analyze(
        self,
        scored: list[ScoredCandidate],
    ) -> list[AmbiguityAnalysis]:
        """Group by side-A anchor, rank by score with a stable tiebreak.

        Ranking never depends on dict or set iteration order, so re-running the
        same snapshot produces the same ranking (spec sections 36 and 51).
        """
        by_anchor: dict[UUID, list[ScoredCandidate]] = defaultdict(list)
        for candidate in scored:
            for anchor in candidate.candidate.side_a_ids:
                by_anchor[anchor].append(candidate)

        analyses: list[AmbiguityAnalysis] = []
        for anchor in sorted(by_anchor, key=str):
            ranked = sorted(by_anchor[anchor], key=lambda c: c.sort_key)
            analyses.append(AmbiguityAnalysis(anchor_id=anchor, ranked=ranked))
        return analyses

    def reverse_contention(
        self,
        analyses: list[AmbiguityAnalysis],
    ) -> dict[UUID, int]:
        """How many side-A anchors each side-B transaction is the top pick for.

        A side-B record wanted by two different anchors is contested even when
        each anchor sees an unambiguous winner. Without this check the engine
        would happily auto-match both.
        """
        counts: dict[UUID, int] = defaultdict(int)
        for analysis in analyses:
            top = analysis.top
            if top is None:
                continue
            for target in top.candidate.side_b_ids:
                counts[target] += 1
        return dict(counts)


def confidence_from_score(score: float) -> float:
    """Map an evidence score in [0,1] to a calibrated-shaped confidence.

    This is an explicitly uncalibrated placeholder for the MVP. It is monotone
    and conservative: it never returns more confidence than evidence, so the
    auto-match band cannot be reached by a low-evidence pairing. When a trained,
    calibrated classifier ships (spec sections 26 and 47) it replaces this
    function and the thresholds keep their meaning.
    """
    return round(min(score, 1.0), 6)


def decide(
    top: ScoredCandidate | None,
    second: ScoredCandidate | None,
    config: ReconciliationConfig,
    *,
    contested: bool = False,
    materiality_override: bool = False,
) -> DecisionResult:
    """The decision policy (spec section 18).

    Auto-match requires *all* of:

    * the evidence score clears the auto threshold, or a deterministic rule
      asserted the match outright;
    * the margin to the runner-up clears the minimum;
    * no hard conflicts;
    * the rule's measured precision clears the configured floor;
    * the candidate is unique (when uniqueness is required);
    * at least one strong identifier matched;
    * the amount is below the materiality threshold, or manual approval above
      materiality is switched off.
    """
    thresholds = config.decision
    codes: list[str] = []

    if top is None:
        return DecisionResult(
            outcome=DecisionOutcome.EXCEPTION,
            policy_codes=("NO_CANDIDATE",),
            explanation="No eligible candidate was found for this transaction.",
        )

    margin = round(top.score - (second.score if second else 0.0), 6)
    confidence = confidence_from_score(top.score)
    competing = 1 if second else 0

    blockers: list[str] = []

    if top.score < thresholds.auto_match_threshold:
        blockers.append("BELOW_AUTO_THRESHOLD")
    if second is not None and margin < thresholds.minimum_candidate_margin:
        blockers.append("INSUFFICIENT_MARGIN")
    if top.hard_conflicts:
        blockers.append("HARD_CONFLICT")
    if thresholds.require_unique_candidate and second is not None:
        blockers.append("CANDIDATE_NOT_UNIQUE")
    if contested:
        blockers.append("COUNTERPARTY_RECORD_CONTESTED")
    if top.rule_precision_estimate < thresholds.min_rule_precision:
        blockers.append("RULE_PRECISION_BELOW_FLOOR")
    if not has_strong_identifier(top.features):
        blockers.append("NO_STRONG_IDENTIFIER")
    if materiality_override:
        blockers.append("ABOVE_MATERIALITY_REQUIRES_APPROVAL")

    if not blockers:
        codes.append("AUTO_MATCH_ALL_CONDITIONS_MET")
        return DecisionResult(
            outcome=DecisionOutcome.AUTO_MATCH,
            top=top,
            second=second,
            score_margin=margin,
            competing_candidate_count=competing,
            confidence=confidence,
            policy_codes=tuple(codes),
            explanation=_explain_auto(top, margin),
        )

    if top.score >= thresholds.suggested_match_threshold:
        return DecisionResult(
            outcome=DecisionOutcome.SUGGEST,
            top=top,
            second=second,
            score_margin=margin,
            competing_candidate_count=competing,
            confidence=confidence,
            policy_codes=tuple(blockers),
            explanation=_explain_suggest(blockers, margin),
        )

    return DecisionResult(
        outcome=DecisionOutcome.EXCEPTION,
        top=top,
        second=second,
        score_margin=margin,
        competing_candidate_count=competing,
        confidence=confidence,
        policy_codes=tuple([*blockers, "BELOW_SUGGEST_THRESHOLD"]),
        explanation=("The best candidate does not carry enough evidence to suggest a match."),
    )


def _explain_auto(top: ScoredCandidate, margin: float) -> str:
    lead = "Matched automatically: "
    reasons = "; ".join(r.description for r in top.reasons[:3])
    if margin:
        return f"{lead}{reasons} The nearest alternative scored {margin:.1%} lower."
    return f"{lead}{reasons} No competing candidate was found."


_BLOCKER_TEXT: dict[str, str] = {
    "BELOW_AUTO_THRESHOLD": "the evidence score is below the automatic threshold",
    "INSUFFICIENT_MARGIN": "a competing candidate scores almost as highly",
    "HARD_CONFLICT": "the records disagree on an identifier",
    "CANDIDATE_NOT_UNIQUE": "more than one candidate is eligible",
    "COUNTERPARTY_RECORD_CONTESTED": "another transaction also selects this counterparty record",
    "RULE_PRECISION_BELOW_FLOOR": "this rule does not yet have enough measured precision",
    "NO_STRONG_IDENTIFIER": "no identifier matched exactly",
    "ABOVE_MATERIALITY_REQUIRES_APPROVAL": "the amount is at or above the materiality threshold",
}


def _explain_suggest(blockers: list[str], margin: float) -> str:
    del margin
    causes = [_BLOCKER_TEXT.get(code, code.lower().replace("_", " ")) for code in blockers]
    joined = "; ".join(causes)
    return f"Suggested for review because {joined}."


@dataclass(slots=True)
class PolicyEngine:
    """Applies :func:`decide` across a whole run, with contention awareness."""

    config: ReconciliationConfig

    def decide_all(
        self,
        analyses: list[AmbiguityAnalysis],
        *,
        contention: dict[UUID, int] | None = None,
        amounts: dict[UUID, Decimal] | None = None,
    ) -> dict[UUID, DecisionResult]:
        contention = contention or {}
        amounts = amounts or {}
        controls = self.config.controls
        results: dict[UUID, DecisionResult] = {}

        for analysis in analyses:
            top = analysis.top
            contested = False
            if top is not None:
                contested = any(
                    contention.get(target, 0) > 1 for target in top.candidate.side_b_ids
                )

            above_materiality = False
            if controls.manual_approval_above_materiality and top is not None:
                amount = amounts.get(analysis.anchor_id, top.total_amount)
                above_materiality = abs(amount) >= controls.materiality_threshold

            results[analysis.anchor_id] = decide(
                analysis.top,
                analysis.second,
                self.config,
                contested=contested,
                materiality_override=above_materiality,
            )

        return results
