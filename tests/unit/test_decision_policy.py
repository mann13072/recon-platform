"""The decision policy is the platform's central safety claim (spec sections 18, 19).

False matches are more dangerous than unmatched records, so every test here is
written from the direction of "would this wrongly automate?".
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from packages.domain.enums import DecisionOutcome, MatchCardinality
from packages.domain.models.matching import (
    CandidateMatch,
    MatchFeatures,
    MatchWarning,
    ScoredCandidate,
)
from packages.domain.models.reconciliation import (
    ControlsConfig,
    DecisionThresholds,
    ReconciliationConfig,
    SideConfig,
)
from packages.matching.ambiguity import AmbiguityAnalyzer, decide

TENANT = uuid4()
RUN = uuid4()


def config(**overrides: object) -> ReconciliationConfig:
    decision = DecisionThresholds(
        auto_match_threshold=0.995,
        suggested_match_threshold=0.80,
        minimum_candidate_margin=0.08,
        min_rule_precision=0.999,
        require_unique_candidate=bool(overrides.pop("require_unique_candidate", False)),
    )
    return ReconciliationConfig(
        name="test",
        side_a=SideConfig(type="bank"),
        side_b=SideConfig(type="ledger"),
        decision=decision,
        controls=ControlsConfig(**overrides),  # type: ignore[arg-type]
    )


def candidate(a_id: object | None = None, b_id: object | None = None) -> CandidateMatch:
    return CandidateMatch(
        id=uuid4(),
        tenant_id=TENANT,
        run_id=RUN,
        side_a_ids=(a_id or uuid4(),),  # type: ignore[arg-type]
        side_b_ids=(b_id or uuid4(),),  # type: ignore[arg-type]
        cardinality=MatchCardinality.ONE_TO_ONE,
        generated_by="test",
    )


def scored(
    score: float,
    *,
    strong_identifier: bool = True,
    precision: float = 0.9999,
    conflicts: int = 0,
    amount: str = "100.00",
) -> ScoredCandidate:
    features = MatchFeatures(
        amount_exact=True,
        currency_exact=True,
        reference_exact=strong_identifier,
        date_distance_days=0,
    )
    warnings = (
        (MatchWarning(code="CONFLICT_REFERENCE_DISAGREES", description="x"),) if conflicts else ()
    )
    return ScoredCandidate(
        candidate=candidate(),
        features=features,
        score=score,
        warnings=warnings,
        hard_conflicts=conflicts,
        rule_precision_estimate=precision,
        total_amount=Decimal(amount),
        currency="EUR",
    )


class TestTheNinetySixVersusNinetyFiveCase:
    """The spec's own example. This is the test that must never regress."""

    def test_96_does_not_auto_match_when_95_exists(self) -> None:
        result = decide(scored(0.96), scored(0.95), config())
        assert result.outcome is not DecisionOutcome.AUTO_MATCH
        assert result.outcome is DecisionOutcome.SUGGEST
        assert "INSUFFICIENT_MARGIN" in result.policy_codes

    def test_same_top_score_auto_matches_when_alone(self) -> None:
        result = decide(scored(0.9995), None, config())
        assert result.outcome is DecisionOutcome.AUTO_MATCH

    def test_high_score_with_distant_runner_up_auto_matches(self) -> None:
        result = decide(scored(0.9995), scored(0.40), config())
        assert result.outcome is DecisionOutcome.AUTO_MATCH
        assert result.score_margin == pytest.approx(0.5995)


class TestAutoMatchPreconditions:
    def test_below_auto_threshold_only_suggests(self) -> None:
        result = decide(scored(0.90), None, config())
        assert result.outcome is DecisionOutcome.SUGGEST
        assert "BELOW_AUTO_THRESHOLD" in result.policy_codes

    def test_hard_conflict_blocks_auto_match(self) -> None:
        result = decide(scored(1.0, conflicts=1), None, config())
        assert result.outcome is DecisionOutcome.SUGGEST
        assert "HARD_CONFLICT" in result.policy_codes

    def test_low_rule_precision_blocks_auto_match(self) -> None:
        result = decide(scored(1.0, precision=0.5), None, config())
        assert result.outcome is DecisionOutcome.SUGGEST
        assert "RULE_PRECISION_BELOW_FLOOR" in result.policy_codes

    def test_no_strong_identifier_blocks_auto_match(self) -> None:
        """Weak similarity must never accumulate into an automatic match."""
        result = decide(scored(1.0, strong_identifier=False), None, config())
        assert result.outcome is DecisionOutcome.SUGGEST
        assert "NO_STRONG_IDENTIFIER" in result.policy_codes

    def test_require_unique_candidate_blocks_any_runner_up(self) -> None:
        result = decide(scored(1.0), scored(0.05), config(require_unique_candidate=True))
        assert result.outcome is DecisionOutcome.SUGGEST
        assert "CANDIDATE_NOT_UNIQUE" in result.policy_codes

    def test_contested_counterparty_record_blocks_auto_match(self) -> None:
        result = decide(scored(1.0), None, config(), contested=True)
        assert result.outcome is DecisionOutcome.SUGGEST
        assert "COUNTERPARTY_RECORD_CONTESTED" in result.policy_codes

    def test_materiality_forces_manual_approval(self) -> None:
        result = decide(scored(1.0), None, config(), materiality_override=True)
        assert result.outcome is DecisionOutcome.SUGGEST
        assert "ABOVE_MATERIALITY_REQUIRES_APPROVAL" in result.policy_codes


class TestExceptionBand:
    def test_below_suggest_threshold_is_an_exception(self) -> None:
        result = decide(scored(0.4), None, config())
        assert result.outcome is DecisionOutcome.EXCEPTION
        assert "BELOW_SUGGEST_THRESHOLD" in result.policy_codes

    def test_no_candidate_is_an_exception(self) -> None:
        result = decide(None, None, config())
        assert result.outcome is DecisionOutcome.EXCEPTION
        assert result.policy_codes == ("NO_CANDIDATE",)


class TestAmbiguityAnalyzer:
    def test_ranking_is_deterministic_across_equal_scores(self) -> None:
        anchor = uuid4()
        a = ScoredCandidate(
            candidate=candidate(anchor, uuid4()),
            features=MatchFeatures(),
            score=0.9,
        )
        b = ScoredCandidate(
            candidate=candidate(anchor, uuid4()),
            features=MatchFeatures(),
            score=0.9,
        )
        analyzer = AmbiguityAnalyzer()
        first = analyzer.analyze([a, b])[0]
        second = analyzer.analyze([b, a])[0]
        assert [c.candidate.id for c in first.ranked] == [c.candidate.id for c in second.ranked]

    def test_margin_and_competing_count(self) -> None:
        anchor = uuid4()
        candidates = [
            ScoredCandidate(
                candidate=candidate(anchor, uuid4()),
                features=MatchFeatures(),
                score=score,
            )
            for score in (0.96, 0.95, 0.10)
        ]
        analysis = AmbiguityAnalyzer().analyze(candidates)[0]
        assert analysis.margin == pytest.approx(0.01)
        # 0.95 is a genuine competitor; 0.10 is a long-tail near-miss.
        assert analysis.competing_count == 1

    def test_reverse_contention_detects_two_anchors_wanting_one_record(self) -> None:
        target = uuid4()
        anchors = [uuid4(), uuid4()]
        candidates = [
            ScoredCandidate(
                candidate=candidate(anchor, target),
                features=MatchFeatures(),
                score=0.99,
            )
            for anchor in anchors
        ]
        analyzer = AmbiguityAnalyzer()
        analyses = analyzer.analyze(candidates)
        contention = analyzer.reverse_contention(analyses)
        assert contention[target] == 2
