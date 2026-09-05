"""Stage 3 - bounded 1:N and N:1 group matching (spec section 20).

The spec's illustrative ``find_subsets`` is exponential and returns on the first
tolerance hit without exploring alternatives, which hides ambiguity - the one
thing a reconciliation engine must never hide. This implementation:

* works in **integer minor units**, so sums are exact and no tolerance leaks in
  through binary floating point;
* sorts candidates descending and carries a **suffix-sum bound**, so a branch
  that cannot possibly reach the target is abandoned immediately;
* enforces a **node budget** and a **wall-clock budget**, so a pathological pool
  can slow a run down but can never hang it;
* keeps searching after the first solution and reports **how many** solutions
  exist, because two ways to make the same total is an exception, not a match.

Bounding is what makes "no candidate explosion" (Week 9 acceptance) a property
of the code rather than a hope.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from uuid import UUID

from packages.domain.enums import DecisionOutcome, MatchGroupStatus
from packages.domain.models.matching import MatchGroup
from packages.domain.models.reconciliation import GroupingConfig, ReconciliationConfig
from packages.domain.models.transaction import CanonicalTransaction
from packages.domain.money import minor_units
from packages.matching.exact import StageContext, StageResult, make_group
from packages.matching.rules import MatchingRule, RuleDecision, RuleRisk

__all__ = ["GroupMatcher", "SubsetSearchResult", "find_subsets"]


@dataclass(slots=True)
class SubsetSearchResult:
    """Outcome of one bounded subset search."""

    solutions: list[tuple[str, ...]] = field(default_factory=list)
    nodes_visited: int = 0
    elapsed_ms: float = 0.0
    exhausted: bool = True
    """``True`` when the whole (bounded) space was explored. ``False`` means a
    budget stopped the search, so 'unique solution' cannot be asserted."""

    @property
    def unique(self) -> bool:
        return self.exhausted and len(self.solutions) == 1


def find_subsets(
    amounts: list[tuple[str, int]],
    target: int,
    tolerance: int,
    *,
    max_group_size: int = 6,
    max_solutions: int = 8,
    node_budget: int = 200_000,
    time_budget_ms: int = 2_000,
) -> SubsetSearchResult:
    """Find subsets of ``amounts`` summing to ``target`` within ``tolerance``.

    All values are integer minor units. Returns every solution found, up to
    ``max_solutions``, so the caller can detect ambiguity.
    """
    result = SubsetSearchResult()
    if not amounts or max_group_size < 1:
        return result

    # Descending by absolute value: large items are decided first, which prunes
    # far more of the tree than an arbitrary order.
    ordered = sorted(amounts, key=lambda item: (-abs(item[1]), item[0]))
    values = [value for _, value in ordered]
    ids = [tx_id for tx_id, _ in ordered]
    n = len(values)

    # suffix_positive[i] is the largest total still reachable from index i, and
    # suffix_negative[i] the smallest. A branch is dead when the target cannot
    # be reached even by taking every remaining item of the helpful sign.
    suffix_positive = [0] * (n + 1)
    suffix_negative = [0] * (n + 1)
    for i in range(n - 1, -1, -1):
        value = values[i]
        suffix_positive[i] = suffix_positive[i + 1] + (value if value > 0 else 0)
        suffix_negative[i] = suffix_negative[i + 1] + (value if value < 0 else 0)

    started = time.perf_counter()
    deadline = started + (time_budget_ms / 1000.0)
    state = {"nodes": 0, "stopped": False}

    def budget_exceeded() -> bool:
        if state["nodes"] >= node_budget:
            state["stopped"] = True
            return True
        # Checking the clock on every node is wasteful; every 1024 is enough to
        # bound the overrun to well under a millisecond of extra work.
        if state["nodes"] % 1024 == 0 and time.perf_counter() > deadline:
            state["stopped"] = True
            return True
        return False

    def backtrack(index: int, chosen: list[str], total: int) -> None:
        if state["stopped"] or len(result.solutions) >= max_solutions:
            return

        state["nodes"] += 1
        if budget_exceeded():
            return

        if chosen and abs(total - target) <= tolerance:
            result.solutions.append(tuple(chosen))
            # Do not return: a superset containing a zero-value item would be a
            # different, equally valid solution, and the caller needs to know.
            if len(result.solutions) >= max_solutions:
                return

        if index >= n or len(chosen) >= max_group_size:
            return

        remaining_max = total + suffix_positive[index]
        remaining_min = total + suffix_negative[index]
        if remaining_max < target - tolerance or remaining_min > target + tolerance:
            return

        # Take the item at ``index``.
        backtrack(index + 1, [*chosen, ids[index]], total + values[index])
        # Skip it.
        backtrack(index + 1, chosen, total)

    backtrack(0, [], 0)

    result.nodes_visited = state["nodes"]
    result.elapsed_ms = (time.perf_counter() - started) * 1000.0
    result.exhausted = not state["stopped"]
    return result


@dataclass(slots=True)
class GroupMatcher:
    """Bounded 1:N and N:1 matching over the transactions earlier stages left."""

    config: ReconciliationConfig

    @property
    def grouping(self) -> GroupingConfig:
        return self.config.grouping

    def match(
        self,
        side_a: list[CanonicalTransaction],
        side_b: list[CanonicalTransaction],
        context: StageContext,
    ) -> StageResult:
        """Try to explain each side-A transaction as a sum of side-B records, then
        the reverse.

        A group is only created when the search was exhaustive *and* found
        exactly one solution. An ambiguous or budget-truncated search leaves the
        transactions unmatched so a human sees them.
        """
        if not self.grouping.enabled:
            return StageResult(matches=[], remaining_a=list(side_a), remaining_b=list(side_b))

        matches: list[MatchGroup] = []
        stats: dict[str, int] = {
            "one_to_many": 0,
            "many_to_one": 0,
            "ambiguous": 0,
            "budget_exhausted": 0,
        }
        consumed_a: set[UUID] = set()
        consumed_b: set[UUID] = set()

        matches.extend(
            self._match_direction(
                anchors=side_a,
                pool=side_b,
                consumed_anchors=consumed_a,
                consumed_pool=consumed_b,
                context=context,
                anchor_is_a=True,
                stats=stats,
            )
        )
        matches.extend(
            self._match_direction(
                anchors=[tx for tx in side_b if tx.id not in consumed_b],
                pool=[tx for tx in side_a if tx.id not in consumed_a],
                consumed_anchors=consumed_b,
                consumed_pool=consumed_a,
                context=context,
                anchor_is_a=False,
                stats=stats,
            )
        )

        return StageResult(
            matches=matches,
            remaining_a=[tx for tx in side_a if tx.id not in consumed_a],
            remaining_b=[tx for tx in side_b if tx.id not in consumed_b],
            stats=stats,
        )

    def _match_direction(
        self,
        *,
        anchors: list[CanonicalTransaction],
        pool: list[CanonicalTransaction],
        consumed_anchors: set[UUID],
        consumed_pool: set[UUID],
        context: StageContext,
        anchor_is_a: bool,
        stats: dict[str, int],
    ) -> list[MatchGroup]:
        matches: list[MatchGroup] = []
        by_id = {str(tx.id): tx for tx in pool}
        grouping = self.grouping
        tolerance_units_cache: dict[str, int] = {}

        for anchor in anchors:
            if anchor.id in consumed_anchors:
                continue

            currency = anchor.currency
            if currency not in tolerance_units_cache:
                tolerance_units_cache[currency] = minor_units(
                    self.config.tolerances.max_amount_difference, currency
                )
            tolerance_units = tolerance_units_cache[currency]

            pool_candidates = self._candidate_pool(anchor, pool, consumed_pool)
            if len(pool_candidates) < 2:
                # A single-member "group" is a 1:1 match and belongs to the
                # earlier stages, not here.
                continue

            amounts = [
                (str(tx.id), minor_units(tx.amount, currency)) for tx in pool_candidates
            ]
            search = find_subsets(
                amounts,
                minor_units(anchor.amount, currency),
                tolerance_units,
                max_group_size=grouping.max_group_size,
                max_solutions=grouping.max_solutions,
                node_budget=grouping.node_budget,
                time_budget_ms=grouping.time_budget_ms,
            )

            if not search.exhausted:
                stats["budget_exhausted"] += 1
                continue

            # Groups of one are 1:1 matches; ignore them at this stage.
            real = [s for s in search.solutions if len(s) >= 2]
            if not real:
                continue
            if grouping.require_unique_solution and len(real) > 1:
                stats["ambiguous"] += 1
                continue

            members = [by_id[tx_id] for tx_id in real[0]]
            side_a_members = [anchor] if anchor_is_a else members
            side_b_members = members if anchor_is_a else [anchor]

            matches.append(
                make_group(
                    context=context,
                    rule=_grouping_rule(grouping),
                    a=side_a_members,
                    b=side_b_members,
                    scored=None,
                    stage="grouping",
                    # A subset sum is arithmetic, not evidence of intent, so a
                    # group is always reviewed by a human before it is final.
                    status=MatchGroupStatus.SUGGESTED,
                    decision=DecisionOutcome.SUGGEST,
                    confidence=0.9,
                )
            )
            consumed_anchors.add(anchor.id)
            for member in members:
                consumed_pool.add(member.id)
            stats["one_to_many" if anchor_is_a else "many_to_one"] += 1

        return matches

    def _candidate_pool(
        self,
        anchor: CanonicalTransaction,
        pool: list[CanonicalTransaction],
        consumed: set[UUID],
    ) -> list[CanonicalTransaction]:
        """Narrow the pool before any search runs (spec section 20).

        Filtering by currency, date window, direction and shared batch or
        counterparty is what keeps the search space small enough for the bounds
        above to be generous rather than binding.
        """
        grouping = self.grouping
        window = self.config.tolerances.date_days
        anchor_date = anchor.best_date
        anchor_sign = anchor.amount > 0

        candidates: list[CanonicalTransaction] = []
        for tx in pool:
            if tx.id in consumed or tx.id == anchor.id:
                continue
            if tx.currency != anchor.currency:
                continue
            if (tx.amount > 0) != anchor_sign:
                continue
            if abs(tx.amount) > abs(anchor.amount) + self.config.tolerances.max_amount_difference:
                continue
            if anchor_date and tx.best_date:
                if abs((anchor_date - tx.best_date).days) > window:
                    continue
            candidates.append(tx)

        # Prefer records that share a batch, settlement or counterparty with the
        # anchor: those are the groups that actually exist in the real world.
        def affinity(tx: CanonicalTransaction) -> tuple[int, str]:
            shared = 0
            if anchor.batch_id and tx.batch_id == anchor.batch_id:
                shared -= 3
            if anchor.settlement_id and tx.settlement_id == anchor.settlement_id:
                shared -= 3
            if (
                anchor.normalized_counterparty
                and tx.normalized_counterparty == anchor.normalized_counterparty
            ):
                shared -= 2
            return (shared, str(tx.id))

        candidates.sort(key=affinity)
        return candidates[: grouping.max_candidate_pool]


def _grouping_rule(grouping: GroupingConfig) -> MatchingRule:
    """A synthetic rule record so grouped matches carry a version like any other."""
    from packages.matching.rules import ConditionOperator, RuleCondition

    return MatchingRule(
        id="bounded_subset_sum",
        name="Bounded subset-sum grouping",
        version=f"v1_max{grouping.max_group_size}",
        deterministic=False,
        stage="grouping",
        description=(
            "One transaction explained as the exact sum of several counterparty "
            "records, found by bounded branch-and-bound over integer minor units."
        ),
        conditions=(
            RuleCondition(
                field="amount",
                operator=ConditionOperator.BOTH_PRESENT,
                weight=100.0,
                code="GROUP_SUM_EXACT",
            ),
        ),
        decision=RuleDecision(auto_match_min_score=101.0, suggest_min_score=0.0),
        risk=RuleRisk(require_unique_candidate=True, precision_estimate=0.0),
    )
