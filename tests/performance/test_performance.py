"""Bounded performance and algorithmic-complexity regression tests."""

from __future__ import annotations

import time
from decimal import Decimal
from uuid import uuid4

import pytest

from packages.domain.enums import DecisionOutcome, MatchCardinality, MatchGroupStatus, Side
from packages.domain.models.matching import MatchGroup, MatchGroupMember
from packages.ingestion.parsers import parse_csv
from packages.matching.engine import assert_invariants
from packages.matching.grouping import find_subsets
from scripts.benchmark_matching import run_size
from tests.factories import RECONCILIATION_ID, RUN_ID, TENANT_ID, make_transaction

pytestmark = pytest.mark.performance


def test_candidate_generation_stays_flat_as_dataset_grows() -> None:
    small = run_size(500, match_rate=0.8, grouping=False)
    large = run_size(2_000, match_rate=0.8, grouping=False)

    assert small.candidates_per_row > 0
    assert large.candidates_per_row <= small.candidates_per_row * 1.5


def test_bounded_subset_search_honors_its_time_budget() -> None:
    # Equal values create a deliberately huge number of possible subsets.
    amounts = [(f"item-{index}", 100) for index in range(80)]
    result = find_subsets(
        amounts,
        target=4_000,
        tolerance=0,
        max_group_size=40,
        max_solutions=1_000_000,
        node_budget=10_000_000,
        time_budget_ms=50,
    )

    assert result.elapsed_ms < 2_000
    assert result.exhausted is False


def test_ingesting_five_thousand_csv_rows_completes_under_thirty_seconds() -> None:
    rows = ["Date,Reference,Amount,Currency"]
    rows.extend(f"2026-08-31,INV-{index:05d},{index}.01,EUR" for index in range(5_000))
    payload = ("\n".join(rows) + "\n").encode("utf-8")

    started = time.perf_counter()
    parsed = parse_csv(payload)
    elapsed = time.perf_counter() - started

    assert len(parsed.rows) == 5_000
    assert not parsed.skipped_rows
    assert elapsed < 30


def test_five_thousand_match_groups_validate_under_five_seconds() -> None:
    transactions = {}
    groups = []
    for index in range(5_000):
        transaction = make_transaction(f"INVARIANT-{index}", "1.00")
        transactions[transaction.id] = transaction
        groups.append(
            MatchGroup(
                id=uuid4(),
                tenant_id=TENANT_ID,
                run_id=RUN_ID,
                reconciliation_id=RECONCILIATION_ID,
                members=(
                    MatchGroupMember(
                        transaction_id=transaction.id,
                        side=Side.A,
                        allocated_amount=Decimal("1.00"),
                    ),
                ),
                cardinality=MatchCardinality.ONE_TO_ONE,
                status=MatchGroupStatus.APPROVED,
                decision=DecisionOutcome.SUGGEST,
                confidence=1.0,
                score=1.0,
                currency="EUR",
            )
        )

    started = time.perf_counter()
    assert_invariants(groups, transactions)
    elapsed = time.perf_counter() - started

    assert elapsed < 5
