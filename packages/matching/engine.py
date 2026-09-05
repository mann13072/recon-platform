"""The staged matching engine (spec sections 11 and 38).

    Stage 0 - Eligibility filters
    Stage 1 - Exact deterministic matching
    Stage 2 - Rule/tolerance matching
    Stage 3 - Grouping matching
    Stage 4 - Candidate generation
    Stage 5 - Candidate feature scoring
    Stage 6 - Ambiguity analysis
    Stage 7 - Decision policy
    Stage 8 - Human exception workflow (handed to ``packages.exceptions``)

The engine is a pure function of (side_a, side_b, config, rule_set, context). It
touches no database, no queue and no clock beyond the search budgets, which is
what lets the golden accounting cases run as ordinary unit tests.

Two invariants hold at every stage boundary and are asserted before the result
is returned:

* a transaction appears in at most one active match group;
* the amount allocated to a transaction never exceeds its own amount.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from uuid import UUID

from packages.domain.enums import DecisionOutcome, MatchGroupStatus
from packages.domain.models.matching import (
    DecisionResult,
    MatchGroup,
    ScoredCandidate,
)
from packages.domain.models.reconciliation import ReconciliationConfig
from packages.domain.models.transaction import CanonicalTransaction
from packages.matching.ambiguity import AmbiguityAnalyzer, PolicyEngine
from packages.matching.bipartite import assign_one_to_one
from packages.matching.candidate_generation import CandidateGenerator
from packages.matching.exact import (
    ExactMatcher,
    RuleMatcher,
    StageContext,
    make_group,
)
from packages.matching.grouping import GroupMatcher
from packages.matching.rules import MatchingRule, RuleSet
from packages.matching.scoring import Scorer

__all__ = [
    "MatchExclusivityError",
    "MatchingContext",
    "MatchingEngine",
    "MatchingResult",
    "assert_invariants",
]


class MatchExclusivityError(AssertionError):
    """Raised when a transaction would belong to two active match groups."""


class OverAllocationError(AssertionError):
    """Raised when more than a transaction's amount has been allocated."""


@dataclass(frozen=True, slots=True)
class MatchingContext:
    tenant_id: UUID
    reconciliation_id: UUID
    run_id: UUID
    rule_set_version: str
    model_version: str | None = None

    def to_stage_context(self) -> StageContext:
        return StageContext(
            tenant_id=self.tenant_id,
            run_id=self.run_id,
            reconciliation_id=self.reconciliation_id,
            rule_set_version=self.rule_set_version,
        )


@dataclass(slots=True)
class MatchingResult:
    """Everything one run produced, ready to persist."""

    matches: list[MatchGroup] = field(default_factory=list)
    decisions: dict[UUID, DecisionResult] = field(default_factory=dict)
    scored_candidates: list[ScoredCandidate] = field(default_factory=list)
    unmatched_a: list[CanonicalTransaction] = field(default_factory=list)
    unmatched_b: list[CanonicalTransaction] = field(default_factory=list)
    stage_stats: dict[str, dict[str, int]] = field(default_factory=dict)
    result_hash: str = ""

    @property
    def auto_matched(self) -> list[MatchGroup]:
        return [m for m in self.matches if m.decision is DecisionOutcome.AUTO_MATCH]

    @property
    def suggested(self) -> list[MatchGroup]:
        return [m for m in self.matches if m.decision is DecisionOutcome.SUGGEST]

    def counts(self) -> dict[str, int]:
        by_decision = Counter(m.decision.value for m in self.matches)
        return {
            "matches": len(self.matches),
            "auto_matched": by_decision.get(DecisionOutcome.AUTO_MATCH.value, 0),
            "suggested": by_decision.get(DecisionOutcome.SUGGEST.value, 0),
            "unmatched_a": len(self.unmatched_a),
            "unmatched_b": len(self.unmatched_b),
        }


def assert_invariants(
    matches: list[MatchGroup],
    transactions_by_id: dict[UUID, CanonicalTransaction],
) -> None:
    """Enforce the accounting integrity invariants of spec section 86.

    These are assertions, not warnings. A violation means the engine produced a
    financially incoherent result and the run must fail rather than persist it.
    """
    seen: dict[UUID, UUID] = {}
    allocated: dict[UUID, Decimal] = {}

    for group in matches:
        if not group.status.is_active:
            continue
        for member in group.members:
            previous = seen.get(member.transaction_id)
            if previous is not None and previous != group.id:
                raise MatchExclusivityError(
                    f"transaction {member.transaction_id} appears in active match "
                    f"groups {previous} and {group.id}"
                )
            seen[member.transaction_id] = group.id
            allocated[member.transaction_id] = allocated.get(
                member.transaction_id, Decimal("0")
            ) + abs(member.allocated_amount)

    for transaction_id, total in allocated.items():
        transaction = transactions_by_id.get(transaction_id)
        if transaction is None:
            raise AssertionError(f"match group references unknown transaction {transaction_id}")
        if total > abs(transaction.amount):
            raise OverAllocationError(
                f"transaction {transaction_id} has {total} allocated but its amount "
                f"is only {abs(transaction.amount)}"
            )


@dataclass(slots=True)
class MatchingEngine:
    """Wires the stages together and enforces the invariants between them."""

    config: ReconciliationConfig
    rule_set: RuleSet
    scorer: Scorer = field(init=False)
    exact_matcher: ExactMatcher = field(init=False)
    rule_matcher: RuleMatcher = field(init=False)
    group_matcher: GroupMatcher = field(init=False)
    candidate_generator: CandidateGenerator = field(init=False)
    ambiguity_analyzer: AmbiguityAnalyzer = field(init=False)
    policy_engine: PolicyEngine = field(init=False)

    def __post_init__(self) -> None:
        self.scorer = Scorer(config=self.config)
        self.exact_matcher = ExactMatcher(self.config, self.rule_set, self.scorer)
        self.rule_matcher = RuleMatcher(self.config, self.rule_set, self.scorer)
        self.group_matcher = GroupMatcher(self.config)
        self.candidate_generator = CandidateGenerator.for_config(self.config)
        self.ambiguity_analyzer = AmbiguityAnalyzer()
        self.policy_engine = PolicyEngine(self.config)

    def run(
        self,
        side_a: list[CanonicalTransaction],
        side_b: list[CanonicalTransaction],
        context: MatchingContext,
    ) -> MatchingResult:
        """Execute the pipeline.

        ``remaining_a`` / ``remaining_b`` are threaded through every stage. A
        transaction consumed by an earlier stage is physically absent from the
        input of every later stage, which is how re-consumption is prevented
        structurally rather than by a post-hoc check.
        """
        stage_context = context.to_stage_context()
        by_id: dict[UUID, CanonicalTransaction] = {tx.id: tx for tx in (*side_a, *side_b)}
        result = MatchingResult()

        # -- Stage 1: exact deterministic rules ----------------------------
        exact = self.exact_matcher.match(side_a, side_b, stage_context)
        result.matches.extend(exact.matches)
        result.stage_stats["exact"] = exact.stats
        assert_invariants(result.matches, by_id)

        remaining_a, remaining_b = exact.remaining_a, exact.remaining_b

        # -- Stage 2: rule / tolerance matching ----------------------------
        ruled = self.rule_matcher.match(remaining_a, remaining_b, stage_context)
        result.matches.extend(ruled.matches)
        result.stage_stats["rule"] = ruled.stats
        assert_invariants(result.matches, by_id)

        remaining_a, remaining_b = ruled.remaining_a, ruled.remaining_b

        # -- Stage 3: bounded grouping -------------------------------------
        grouped = self.group_matcher.match(remaining_a, remaining_b, stage_context)
        result.matches.extend(grouped.matches)
        result.stage_stats["grouping"] = grouped.stats
        assert_invariants(result.matches, by_id)

        remaining_a, remaining_b = grouped.remaining_a, grouped.remaining_b

        # -- Stage 4: candidate generation ---------------------------------
        candidates = self.candidate_generator.generate(
            remaining_a,
            remaining_b,
            run_id=context.run_id,
            tenant_id=context.tenant_id,
        )
        result.stage_stats["candidates"] = {"generated": len(candidates)}

        # -- Stage 5: scoring ----------------------------------------------
        scored: list[ScoredCandidate] = []
        for candidate in candidates:
            a = by_id[candidate.side_a_ids[0]]
            b = by_id[candidate.side_b_ids[0]]
            evaluated = self.scorer.score_candidate(candidate, a, b, rule=None)
            if evaluated is not None:
                scored.append(evaluated)
        result.scored_candidates = sorted(scored, key=lambda c: c.sort_key)
        result.stage_stats["scoring"] = {"scored": len(scored)}

        # Global 1:1 assignment, so one side-B record cannot satisfy two anchors.
        assignment = assign_one_to_one(result.scored_candidates)

        # -- Stage 6: ambiguity --------------------------------------------
        analyses = self.ambiguity_analyzer.analyze(result.scored_candidates)
        contention = self.ambiguity_analyzer.reverse_contention(analyses)

        # -- Stage 7: decision policy --------------------------------------
        amounts = {tx.id: tx.amount for tx in remaining_a}
        decisions = self.policy_engine.decide_all(analyses, contention=contention, amounts=amounts)
        result.decisions = decisions

        consumed_a: set[UUID] = set()
        consumed_b: set[UUID] = set()

        for analysis in analyses:
            decision = decisions[analysis.anchor_id]
            if decision.outcome is DecisionOutcome.EXCEPTION or decision.top is None:
                continue

            top = decision.top
            # The global assignment has the final say on which pairing survives:
            # a locally best candidate that lost the assignment must not match.
            assigned = assignment.pairs.get(analysis.anchor_id)
            if assigned is None or assigned.candidate.id != top.candidate.id:
                continue

            anchor = by_id[analysis.anchor_id]
            partners = [by_id[i] for i in top.candidate.side_b_ids]
            if anchor.id in consumed_a or any(p.id in consumed_b for p in partners):
                continue

            auto = decision.outcome is DecisionOutcome.AUTO_MATCH
            group = make_group(
                context=stage_context,
                rule=_scoring_pseudo_rule(),
                a=[anchor],
                b=partners,
                scored=top,
                stage="scoring",
                status=(MatchGroupStatus.AUTO_APPROVED if auto else MatchGroupStatus.SUGGESTED),
                decision=decision.outcome,
                confidence=decision.confidence,
            )
            group = group.model_copy(
                update={
                    "competing_candidate_count": analysis.competing_count,
                    "model_version": context.model_version,
                    "metadata": {
                        "score_margin": f"{decision.score_margin:.6f}",
                        "policy_codes": ",".join(decision.policy_codes),
                        "explanation": decision.explanation,
                    },
                }
            )
            result.matches.append(group)
            consumed_a.add(anchor.id)
            consumed_b.update(p.id for p in partners)

        assert_invariants(result.matches, by_id)

        result.unmatched_a = [tx for tx in remaining_a if tx.id not in consumed_a]
        result.unmatched_b = [tx for tx in remaining_b if tx.id not in consumed_b]
        result.result_hash = _hash_result(result)
        return result


def _scoring_pseudo_rule() -> MatchingRule:
    """A rule record for matches produced by the scorer rather than a named rule."""
    from packages.matching.rules import (
        ConditionOperator,
        RuleCondition,
        RuleDecision,
        RuleRisk,
    )

    return MatchingRule(
        id="feature_scoring",
        name="Explainable feature scoring",
        version="v1",
        deterministic=False,
        stage="scoring",
        description=("Weighted deterministic features with ambiguity-aware decision policy."),
        conditions=(
            RuleCondition(
                field="amount",
                operator=ConditionOperator.BOTH_PRESENT,
                weight=100.0,
                code="FEATURE_SCORE",
            ),
        ),
        decision=RuleDecision(),
        risk=RuleRisk(precision_estimate=0.0),
    )


def _hash_result(result: MatchingResult) -> str:
    """A content hash over the run's decisions.

    Two runs of the same snapshot with the same rules must produce the same
    hash. This is the cheapest possible reproducibility check and the audit
    export records it.
    """
    payload = {
        "matches": sorted(
            [
                {
                    "id": str(group.id),
                    "members": sorted(str(m.transaction_id) for m in group.members),
                    "decision": group.decision.value,
                    "status": group.status.value,
                    "rule": f"{group.rule_id}:{group.rule_version}",
                    "score": round(group.score, 6),
                }
                for group in result.matches
            ],
            key=lambda item: item["id"],
        ),
        "unmatched_a": sorted(str(tx.id) for tx in result.unmatched_a),
        "unmatched_b": sorted(str(tx.id) for tx in result.unmatched_b),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
