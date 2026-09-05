"""Bounded 1:N / N:1 grouping (spec section 20, Week 9 acceptance).

The acceptance criteria being tested: settlement fixtures work, the search is
time-bounded, and there is no candidate explosion.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from packages.domain.enums import DecisionOutcome, MatchGroupStatus
from packages.domain.models.reconciliation import GroupingConfig
from packages.matching.engine import MatchingContext, MatchingEngine
from packages.matching.grouping import find_subsets
from packages.matching.templates import bank_gl_template
from tests.factories import RECONCILIATION_ID, RUN_ID, TENANT_ID, make_transaction


class TestFindSubsets:
    def test_spec_example_ten_thousand_from_three_ledger_lines(self) -> None:
        """Bank 10,000 = 4,000 + 3,000 + 3,000 (spec section 20)."""
        amounts = [("L1", 400_000), ("L2", 300_000), ("L3", 300_000)]
        result = find_subsets(amounts, 1_000_000, 0, max_group_size=6)
        assert result.exhausted
        assert result.solutions == [("L1", "L2", "L3")]
        assert result.unique

    def test_detects_two_ways_to_make_the_same_total(self) -> None:
        """Ambiguity must be reported, not resolved by taking the first hit."""
        amounts = [("A", 500), ("B", 500), ("C", 1000)]
        result = find_subsets(amounts, 1000, 0, max_group_size=4)
        assert result.exhausted
        assert len(result.solutions) >= 2
        assert not result.unique

    def test_respects_max_group_size(self) -> None:
        amounts = [(f"L{i}", 100) for i in range(10)]
        result = find_subsets(amounts, 500, 0, max_group_size=3)
        assert result.solutions == []

    def test_tolerance_is_in_integer_minor_units(self) -> None:
        amounts = [("A", 40_000), ("B", 30_000), ("C", 30_005)]
        assert find_subsets(amounts, 100_000, 0, max_group_size=3).solutions == []
        assert find_subsets(amounts, 100_000, 5, max_group_size=3).solutions == [
            ("A", "C", "B")
        ]

    def test_node_budget_stops_the_search_and_says_so(self) -> None:
        """A pathological pool degrades to 'unknown', never to a wrong answer."""
        amounts = [(f"L{i}", i + 1) for i in range(60)]
        result = find_subsets(
            amounts, 900, 0, max_group_size=12, node_budget=500, max_solutions=99
        )
        assert not result.exhausted
        assert not result.unique
        assert result.nodes_visited <= 600

    def test_time_budget_is_enforced(self) -> None:
        amounts = [(f"L{i}", i + 1) for i in range(80)]
        result = find_subsets(
            amounts,
            12345,
            0,
            max_group_size=12,
            node_budget=10_000_000,
            time_budget_ms=50,
            max_solutions=999,
        )
        # Generous ceiling: the check runs every 1024 nodes, so a small overrun
        # is expected. The point is that it terminates.
        assert result.elapsed_ms < 2_000

    def test_pruning_keeps_node_count_far_below_two_to_the_n(self) -> None:
        """Branch-and-bound, not brute force: 2**30 nodes would never finish."""
        amounts = [(f"L{i}", 1_000_000 + i) for i in range(30)]
        result = find_subsets(amounts, 3_000_003, 0, max_group_size=3)
        assert result.exhausted
        assert result.nodes_visited < 50_000

    def test_empty_input_is_safe(self) -> None:
        assert find_subsets([], 100, 0).solutions == []


class TestGroupMatcherIntegration:
    def _engine(self, **grouping: object) -> MatchingEngine:
        config, rule_set = bank_gl_template()
        config = config.model_copy(
            update={"grouping": GroupingConfig(enabled=True, **grouping)}  # type: ignore[arg-type]
        )
        return MatchingEngine(config, rule_set)

    def _context(self) -> MatchingContext:
        return MatchingContext(TENANT_ID, RECONCILIATION_ID, RUN_ID, "v1")

    def test_one_to_many_settlement_is_grouped_and_suggested(self) -> None:
        bank = [
            make_transaction(
                "BANK-1", "10000.00", transaction_date=date(2026, 8, 31),
                description="BATCH DEPOSIT", batch_id="B77",
            )
        ]
        ledger = [
            make_transaction("GL-1", "4000.00", source_system="ledger",
                             transaction_date=date(2026, 8, 31), batch_id="B77"),
            make_transaction("GL-2", "3000.00", source_system="ledger",
                             transaction_date=date(2026, 8, 31), batch_id="B77"),
            make_transaction("GL-3", "3000.00", source_system="ledger",
                             transaction_date=date(2026, 8, 31), batch_id="B77"),
        ]
        result = self._engine(max_group_size=5).run(bank, ledger, self._context())

        groups = [m for m in result.matches if m.engine_stage == "grouping"]
        assert len(groups) == 1
        group = groups[0]
        assert group.cardinality.value == "1:N"
        assert len(group.side_b_ids) == 3
        # Arithmetic is not intent: a subset sum is always reviewed by a human.
        assert group.decision is DecisionOutcome.SUGGEST
        assert group.status is MatchGroupStatus.SUGGESTED
        assert sum(
            m.allocated_amount for m in group.members if m.side.value == "B"
        ) == Decimal("10000.00")

    def test_ambiguous_grouping_is_left_for_a_human(self) -> None:
        """Two ways to make 1,000 means no group at all."""
        bank = [make_transaction("BANK-1", "1000.00", transaction_date=date(2026, 8, 31))]
        ledger = [
            make_transaction("GL-1", "500.00", source_system="ledger",
                             transaction_date=date(2026, 8, 31)),
            make_transaction("GL-2", "500.00", source_system="ledger",
                             transaction_date=date(2026, 8, 31)),
            make_transaction("GL-3", "250.00", source_system="ledger",
                             transaction_date=date(2026, 8, 31)),
            make_transaction("GL-4", "750.00", source_system="ledger",
                             transaction_date=date(2026, 8, 31)),
        ]
        result = self._engine(max_group_size=4).run(bank, ledger, self._context())
        assert [m for m in result.matches if m.engine_stage == "grouping"] == []
        assert result.stage_stats["grouping"]["ambiguous"] == 1

    def test_grouping_disabled_leaves_everything_alone(self) -> None:
        config, rule_set = bank_gl_template()
        config = config.model_copy(update={"grouping": GroupingConfig(enabled=False)})
        bank = [make_transaction("BANK-1", "10000.00", transaction_date=date(2026, 8, 31))]
        ledger = [
            make_transaction("GL-1", "4000.00", source_system="ledger",
                             transaction_date=date(2026, 8, 31)),
            make_transaction("GL-2", "6000.00", source_system="ledger",
                             transaction_date=date(2026, 8, 31)),
        ]
        result = MatchingEngine(config, rule_set).run(bank, ledger, self._context())
        assert [m for m in result.matches if m.engine_stage == "grouping"] == []
