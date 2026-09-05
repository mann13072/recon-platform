"""Feature extraction and evidence scoring (spec sections 17, 18, 23).

The scale matters as much as the ordering: if a perfect pair cannot clear the
suggest threshold, the whole suggestion stage is dead code. These tests pin both.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

from packages.domain.enums import MatchCardinality
from packages.domain.models.matching import CandidateMatch
from packages.matching.fuzzy import (
    jaro_winkler_similarity,
    levenshtein_similarity,
    token_set_ratio,
    trigram_similarity,
)
from packages.matching.scoring import Scorer, extract_features
from packages.matching.templates import bank_gl_template
from tests.factories import RUN_ID, TENANT_ID, make_transaction

D = date(2026, 8, 1)


def score_pair(a: object, b: object) -> float:
    config, _ = bank_gl_template()
    scorer = Scorer(config=config)
    candidate = CandidateMatch(
        id=uuid4(),
        tenant_id=TENANT_ID,
        run_id=RUN_ID,
        side_a_ids=(a.id,),  # type: ignore[attr-defined]
        side_b_ids=(b.id,),  # type: ignore[attr-defined]
        cardinality=MatchCardinality.ONE_TO_ONE,
        generated_by="test",
    )
    result = scorer.score_candidate(candidate, a, b)  # type: ignore[arg-type]
    assert result is not None
    return result.score


class TestScoreScale:
    def test_a_perfect_pair_reaches_the_top_of_the_scale(self) -> None:
        """It must clear the 0.995 auto threshold, or nothing ever can."""
        a = make_transaction("B1", "500.00", transaction_date=D, reference="INV-7")
        b = make_transaction("G1", "500.00", source_system="ledger",
                             transaction_date=D, reference="INV-7")
        assert score_pair(a, b) >= 0.995

    def test_amount_and_date_alone_stay_in_the_exception_band(self) -> None:
        """No identifier means no suggestion, however neatly the numbers line up."""
        a = make_transaction("B2", "500.00", transaction_date=D)
        b = make_transaction("G2", "500.00", source_system="ledger", transaction_date=D)
        assert score_pair(a, b) < 0.80

    def test_a_similar_counterparty_does_not_rescue_a_weak_pair(self) -> None:
        a = make_transaction("B3", "500.00", transaction_date=D,
                             counterparty_name="Acme Ltd")
        b = make_transaction("G3", "500.00", source_system="ledger",
                             transaction_date=D, counterparty_name="ACME LIMITED")
        assert score_pair(a, b) < 0.80

    def test_an_exact_reference_a_few_days_apart_still_suggests(self) -> None:
        a = make_transaction("B5", "500.00", transaction_date=D, reference="INV-8")
        b = make_transaction("G5", "500.00", source_system="ledger",
                             transaction_date=date(2026, 8, 4), reference="INV-8")
        assert 0.80 <= score_pair(a, b) < 0.995

    def test_scores_are_ordered_by_evidence_strength(self) -> None:
        exact = score_pair(
            make_transaction("B6", "500.00", transaction_date=D, reference="INV-7"),
            make_transaction("G6", "500.00", source_system="ledger",
                            transaction_date=D, reference="INV-7"),
        )
        dated = score_pair(
            make_transaction("B7", "500.00", transaction_date=D, reference="INV-8"),
            make_transaction("G7", "500.00", source_system="ledger",
                            transaction_date=date(2026, 8, 3), reference="INV-8"),
        )
        bare = score_pair(
            make_transaction("B8", "500.00", transaction_date=D),
            make_transaction("G8", "500.00", source_system="ledger", transaction_date=D),
        )
        assert exact > dated > bare

    def test_a_missing_field_neither_helps_nor_hurts(self) -> None:
        """A reference on one side only is silence, not disagreement."""
        both = score_pair(
            make_transaction("B9", "500.00", transaction_date=D, reference="INV-7"),
            make_transaction("G9", "500.00", source_system="ledger",
                            transaction_date=D, reference="INV-7"),
        )
        one_side = score_pair(
            make_transaction("B10", "500.00", transaction_date=D, reference="INV-7"),
            make_transaction("G10", "500.00", source_system="ledger",
                            transaction_date=D),
        )
        assert both > one_side


class TestFeatureExtraction:
    def test_missing_values_never_count_as_agreement(self) -> None:
        a = make_transaction("B1", "10.00", transaction_date=D)
        b = make_transaction("G1", "10.00", source_system="ledger", transaction_date=D)
        features = extract_features(a, b)
        assert not features.reference_exact
        assert not features.invoice_exact
        assert not features.counterparty_exact
        assert not features.settlement_exact
        assert not features.external_id_exact

    def test_net_amount_matching_for_processor_payouts(self) -> None:
        bank = make_transaction("B1", "982.45", transaction_date=D)
        payout = make_transaction(
            "P1", "1000.00", source_system="processor", transaction_date=D,
            gross_amount="1000.00", fee_amount="17.55", net_amount="982.45",
        )
        features = extract_features(bank, payout)
        assert features.net_amount_exact
        assert not features.amount_exact

    def test_invoice_recorded_as_a_plain_reference_still_matches(self) -> None:
        a = make_transaction("B1", "10.00", transaction_date=D, reference="INV-4930")
        b = make_transaction("G1", "10.00", source_system="ledger",
                             transaction_date=D, invoice_number="INV-4930")
        assert extract_features(a, b).invoice_exact

    def test_identifier_inside_a_narrative_is_detected(self) -> None:
        bank = make_transaction("B1", "982.45", transaction_date=D,
                                description="STRIPE PAYOUT 8F42")
        payout = make_transaction("P1", "982.45", source_system="processor",
                                  transaction_date=D, settlement_id="8F42")
        assert extract_features(bank, payout).description_contains_reference

    def test_amount_difference_is_exact_decimal(self) -> None:
        a = make_transaction("B1", "982.45", transaction_date=D)
        b = make_transaction("G1", "982.40", source_system="ledger", transaction_date=D)
        assert extract_features(a, b).amount_difference == Decimal("0.05")


class TestSimilarity:
    def test_identical_strings_score_one(self) -> None:
        for fn in (jaro_winkler_similarity, trigram_similarity, token_set_ratio,
                   levenshtein_similarity):
            assert fn("ACME GMBH", "ACME GMBH") == 1.0

    def test_empty_input_scores_zero(self) -> None:
        for fn in (jaro_winkler_similarity, trigram_similarity, token_set_ratio,
                   levenshtein_similarity):
            assert fn(None, "ACME") == 0.0
            assert fn("ACME", None) == 0.0

    def test_all_similarities_stay_in_range(self) -> None:
        pairs = [
            ("ACME GMBH", "ACME AG"),
            ("INV-4930", "INV-4931"),
            ("STRIPE PAYOUT", "PAYOUT STRIPE"),
            ("A", "ZZZZZZZZZZ"),
        ]
        for left, right in pairs:
            for fn in (jaro_winkler_similarity, trigram_similarity, token_set_ratio,
                       levenshtein_similarity):
                value = fn(left, right)
                assert 0.0 <= value <= 1.0, f"{fn.__name__}({left!r}, {right!r}) = {value}"

    def test_token_set_ratio_ignores_word_order(self) -> None:
        assert token_set_ratio("STRIPE PAYOUT ACME", "ACME STRIPE PAYOUT") == 1.0

    def test_jaro_winkler_rewards_a_shared_prefix(self) -> None:
        assert jaro_winkler_similarity("INV4930", "INV4931") > jaro_winkler_similarity(
            "INV4930", "XYZ4930"
        )
