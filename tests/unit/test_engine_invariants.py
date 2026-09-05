"""Accounting integrity invariants (spec sections 51 and 86).

These are the properties that make a reconciliation result trustworthy at all:
exclusivity, no over-allocation, idempotency and reproducibility.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from packages.domain.enums import DecisionOutcome, MatchGroupStatus, Side
from packages.domain.models.matching import MatchGroup, MatchGroupMember
from packages.domain.models.reconciliation import GroupingConfig
from packages.matching.engine import (
    MatchExclusivityError,
    MatchingContext,
    MatchingEngine,
    assert_invariants,
)
from packages.matching.templates import bank_gl_template
from tests.factories import RECONCILIATION_ID, RUN_ID, TENANT_ID, make_transaction


def context() -> MatchingContext:
    return MatchingContext(TENANT_ID, RECONCILIATION_ID, RUN_ID, "v1")


def engine(**config_overrides: object) -> MatchingEngine:
    config, rule_set = bank_gl_template()
    if config_overrides:
        config = config.model_copy(update=config_overrides)
    return MatchingEngine(config, rule_set)


class TestExclusivity:
    def test_transaction_cannot_be_in_two_active_groups(self) -> None:
        tx = make_transaction("BANK-1", "100.00", transaction_date=date(2026, 8, 1))
        other = make_transaction("GL-1", "100.00", source_system="ledger")

        def group(seed: int) -> MatchGroup:
            return MatchGroup(
                id=make_transaction(f"G{seed}", "1.00").id,
                tenant_id=TENANT_ID,
                run_id=RUN_ID,
                reconciliation_id=RECONCILIATION_ID,
                members=(
                    MatchGroupMember(
                        transaction_id=tx.id, side=Side.A, allocated_amount=tx.amount
                    ),
                    MatchGroupMember(
                        transaction_id=other.id, side=Side.B, allocated_amount=other.amount
                    ),
                ),
                cardinality="1:1",  # type: ignore[arg-type]
                status=MatchGroupStatus.APPROVED,
                decision=DecisionOutcome.AUTO_MATCH,
                confidence=1.0,
                score=1.0,
                currency="EUR",
            )

        with pytest.raises(MatchExclusivityError):
            assert_invariants([group(1), group(2)], {tx.id: tx, other.id: other})

    def test_rejected_group_releases_its_transactions(self) -> None:
        """Only *active* groups hold a claim, so a rejection frees the records."""
        tx = make_transaction("BANK-1", "100.00")
        other = make_transaction("GL-1", "100.00", source_system="ledger")
        members = (
            MatchGroupMember(transaction_id=tx.id, side=Side.A, allocated_amount=tx.amount),
            MatchGroupMember(
                transaction_id=other.id, side=Side.B, allocated_amount=other.amount
            ),
        )
        common = {
            "tenant_id": TENANT_ID,
            "run_id": RUN_ID,
            "reconciliation_id": RECONCILIATION_ID,
            "members": members,
            "cardinality": "1:1",
            "decision": DecisionOutcome.AUTO_MATCH,
            "confidence": 1.0,
            "score": 1.0,
            "currency": "EUR",
        }
        rejected = MatchGroup(
            id=make_transaction("G1", "1.00").id,
            status=MatchGroupStatus.REJECTED,
            **common,  # type: ignore[arg-type]
        )
        approved = MatchGroup(
            id=make_transaction("G2", "1.00").id,
            status=MatchGroupStatus.APPROVED,
            **common,  # type: ignore[arg-type]
        )
        assert_invariants([rejected, approved], {tx.id: tx, other.id: other})

    def test_engine_never_double_consumes_across_stages(self) -> None:
        """A record matched exactly must not reappear in grouping or scoring."""
        bank = [
            make_transaction("BANK-1", "100.00", transaction_date=date(2026, 8, 1),
                             reference="INV-1"),
            make_transaction("BANK-2", "300.00", transaction_date=date(2026, 8, 1)),
        ]
        ledger = [
            make_transaction("GL-1", "100.00", source_system="ledger",
                             transaction_date=date(2026, 8, 1), reference="INV-1"),
            make_transaction("GL-2", "100.00", source_system="ledger",
                             transaction_date=date(2026, 8, 1)),
            make_transaction("GL-3", "200.00", source_system="ledger",
                             transaction_date=date(2026, 8, 1)),
        ]
        result = engine(grouping=GroupingConfig(enabled=True, max_group_size=4)).run(
            bank, ledger, context()
        )
        appearances: dict[str, int] = {}
        for group in result.matches:
            if not group.status.is_active:
                continue
            for member in group.members:
                key = str(member.transaction_id)
                appearances[key] = appearances.get(key, 0) + 1
        assert all(count == 1 for count in appearances.values())


class TestOverAllocation:
    def test_allocation_cannot_exceed_the_transaction_amount(self) -> None:
        tx = make_transaction("BANK-1", "100.00")
        group = MatchGroup(
            id=make_transaction("G1", "1.00").id,
            tenant_id=TENANT_ID,
            run_id=RUN_ID,
            reconciliation_id=RECONCILIATION_ID,
            members=(
                MatchGroupMember(
                    transaction_id=tx.id, side=Side.A, allocated_amount=Decimal("150.00")
                ),
            ),
            cardinality="1:1",  # type: ignore[arg-type]
            status=MatchGroupStatus.APPROVED,
            decision=DecisionOutcome.AUTO_MATCH,
            confidence=1.0,
            score=1.0,
            currency="EUR",
        )
        with pytest.raises(AssertionError, match="allocated"):
            assert_invariants([group], {tx.id: tx})


class TestReproducibility:
    def test_same_snapshot_same_result_hash(self) -> None:
        bank = [
            make_transaction(f"BANK-{i}", f"{100 + i}.00",
                             transaction_date=date(2026, 8, 1), reference=f"INV-{i}")
            for i in range(20)
        ]
        ledger = [
            make_transaction(f"GL-{i}", f"{100 + i}.00", source_system="ledger",
                             transaction_date=date(2026, 8, 1), reference=f"INV-{i}")
            for i in range(20)
        ]
        eng = engine()
        first = eng.run(bank, ledger, context())
        second = eng.run(bank, ledger, context())
        assert first.result_hash == second.result_hash

    def test_input_order_does_not_change_the_outcome(self) -> None:
        """Ranking must not depend on list order, dict order or set order."""
        bank = [
            make_transaction(f"BANK-{i}", f"{100 + i}.00",
                             transaction_date=date(2026, 8, 1), reference=f"INV-{i}")
            for i in range(12)
        ]
        ledger = [
            make_transaction(f"GL-{i}", f"{100 + i}.00", source_system="ledger",
                             transaction_date=date(2026, 8, 1), reference=f"INV-{i}")
            for i in range(12)
        ]
        eng = engine()
        forward = eng.run(bank, ledger, context())
        reversed_ = eng.run(list(reversed(bank)), list(reversed(ledger)), context())
        assert forward.result_hash == reversed_.result_hash


class TestPropertyBased:
    """Hypothesis invariants from spec section 51."""

    @settings(max_examples=40, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(
        amounts=st.lists(
            st.integers(min_value=1, max_value=100_000), min_size=1, max_size=12
        )
    )
    def test_exclusivity_holds_for_arbitrary_amount_sets(
        self, amounts: list[int]
    ) -> None:
        bank = [
            make_transaction(
                f"BANK-{i}", Decimal(value) / 100, transaction_date=date(2026, 8, 1)
            )
            for i, value in enumerate(amounts)
        ]
        ledger = [
            make_transaction(
                f"GL-{i}",
                Decimal(value) / 100,
                source_system="ledger",
                transaction_date=date(2026, 8, 1),
            )
            for i, value in enumerate(amounts)
        ]
        result = engine(
            grouping=GroupingConfig(enabled=True, max_group_size=4, max_candidate_pool=20)
        ).run(bank, ledger, context())

        by_id = {tx.id: tx for tx in (*bank, *ledger)}
        assert_invariants(result.matches, by_id)

    @settings(max_examples=30, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(
        amounts=st.lists(
            st.integers(min_value=1, max_value=50_000), min_size=1, max_size=8
        )
    )
    def test_rerunning_is_idempotent(self, amounts: list[int]) -> None:
        bank = [
            make_transaction(f"BANK-{i}", Decimal(v) / 100,
                             transaction_date=date(2026, 8, 1), reference=f"R{i}")
            for i, v in enumerate(amounts)
        ]
        ledger = [
            make_transaction(f"GL-{i}", Decimal(v) / 100, source_system="ledger",
                             transaction_date=date(2026, 8, 1), reference=f"R{i}")
            for i, v in enumerate(amounts)
        ]
        eng = engine()
        assert eng.run(bank, ledger, context()).result_hash == (
            eng.run(bank, ledger, context()).result_hash
        )

    @settings(max_examples=50, deadline=None)
    @given(
        values=st.lists(st.integers(min_value=1, max_value=5_000), min_size=2, max_size=8),
        target_index=st.integers(min_value=0, max_value=7),
    )
    def test_every_reported_subset_actually_sums_to_the_target(
        self, values: list[int], target_index: int
    ) -> None:
        from packages.matching.grouping import find_subsets

        amounts = [(f"L{i}", v) for i, v in enumerate(values)]
        target = values[target_index % len(values)] + values[(target_index + 1) % len(values)]
        result = find_subsets(amounts, target, 0, max_group_size=6, max_solutions=20)

        lookup = dict(amounts)
        for solution in result.solutions:
            assert sum(lookup[tx_id] for tx_id in solution) == target
            assert len(set(solution)) == len(solution)
