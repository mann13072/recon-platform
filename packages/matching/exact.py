"""Stage 1 - exact deterministic matching, and Stage 2 - rule/tolerance matching.

Spec section 13. Exact rules run first and produce matches directly, without a
score, because there is nothing probabilistic about them. Every match records
the exact rule version that made it, e.g. ``BANK_GL_EXACT_REFERENCE_V3``.

Both stages return their unconsumed remainders, so a transaction consumed at an
earlier stage can never be re-consumed later (spec sections 22, 51 and 86).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID, uuid5

from packages.domain.enums import (
    DecisionOutcome,
    MatchCardinality,
    MatchGroupStatus,
    Side,
)
from packages.domain.models.matching import (
    CandidateMatch,
    MatchGroup,
    MatchGroupMember,
    ScoredCandidate,
)
from packages.domain.models.reconciliation import ReconciliationConfig
from packages.domain.models.transaction import CanonicalTransaction
from packages.matching.candidate_generation import (
    BlockingIndex,
    CandidateGenerator,
    candidate_namespace,
    eligible,
)
from packages.matching.rules import MatchingRule, RuleSet
from packages.matching.scoring import Scorer

__all__ = ["ExactMatcher", "RuleMatcher", "StageContext", "StageResult", "make_group"]


@dataclass(slots=True)
class StageResult:
    """What one pipeline stage produced and what it left behind."""

    matches: list[MatchGroup] = field(default_factory=list)
    remaining_a: list[CanonicalTransaction] = field(default_factory=list)
    remaining_b: list[CanonicalTransaction] = field(default_factory=list)
    scored: list[ScoredCandidate] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class StageContext:
    tenant_id: UUID
    run_id: UUID
    reconciliation_id: UUID
    rule_set_version: str


def make_group(
    *,
    context: StageContext,
    rule: MatchingRule,
    a: list[CanonicalTransaction],
    b: list[CanonicalTransaction],
    scored: ScoredCandidate | None,
    stage: str,
    status: MatchGroupStatus,
    decision: DecisionOutcome,
    confidence: float,
) -> MatchGroup:
    members = [
        MatchGroupMember(transaction_id=tx.id, side=Side.A, allocated_amount=tx.amount)
        for tx in a
    ] + [
        MatchGroupMember(transaction_id=tx.id, side=Side.B, allocated_amount=tx.amount)
        for tx in b
    ]

    if len(a) == 1 and len(b) == 1:
        cardinality = MatchCardinality.ONE_TO_ONE
    elif len(a) == 1:
        cardinality = MatchCardinality.ONE_TO_MANY
    elif len(b) == 1:
        cardinality = MatchCardinality.MANY_TO_ONE
    else:
        cardinality = MatchCardinality.MANY_TO_MANY

    # Deterministic ID: the same snapshot and the same rule always produce the
    # same match group ID, which is what makes a re-run byte-comparable.
    seed = f"{context.run_id}:{rule.versioned_id}:" + ",".join(
        sorted(str(m.transaction_id) for m in members)
    )

    return MatchGroup(
        id=uuid5(candidate_namespace, seed),
        tenant_id=context.tenant_id,
        run_id=context.run_id,
        reconciliation_id=context.reconciliation_id,
        members=tuple(members),
        cardinality=cardinality,
        status=status,
        decision=decision,
        confidence=confidence,
        score=scored.score if scored else 1.0,
        currency=a[0].currency,
        rule_id=rule.id,
        rule_version=rule.version,
        rule_set_version=context.rule_set_version,
        engine_stage=stage,
        reasons=scored.reasons if scored else (),
        warnings=scored.warnings if scored else (),
        competing_candidate_count=0,
    )


@dataclass(slots=True)
class ExactMatcher:
    """Applies deterministic rules that either fire or do not."""

    config: ReconciliationConfig
    rule_set: RuleSet
    scorer: Scorer

    def match(
        self,
        side_a: list[CanonicalTransaction],
        side_b: list[CanonicalTransaction],
        context: StageContext,
    ) -> StageResult:
        rules = [r for r in self.rule_set.enabled_rules() if r.deterministic]
        return _run_rule_stage(
            rules,
            side_a,
            side_b,
            context=context,
            config=self.config,
            scorer=self.scorer,
            stage="exact",
            require_unique=True,
        )


@dataclass(slots=True)
class RuleMatcher:
    """Applies weighted, non-deterministic rules under tolerance."""

    config: ReconciliationConfig
    rule_set: RuleSet
    scorer: Scorer

    def match(
        self,
        side_a: list[CanonicalTransaction],
        side_b: list[CanonicalTransaction],
        context: StageContext,
    ) -> StageResult:
        rules = [r for r in self.rule_set.enabled_rules() if not r.deterministic]
        return _run_rule_stage(
            rules,
            side_a,
            side_b,
            context=context,
            config=self.config,
            scorer=self.scorer,
            stage="rule",
            require_unique=True,
        )


def _run_rule_stage(
    rules: list[MatchingRule],
    side_a: list[CanonicalTransaction],
    side_b: list[CanonicalTransaction],
    *,
    context: StageContext,
    config: ReconciliationConfig,
    scorer: Scorer,
    stage: str,
    require_unique: bool,
) -> StageResult:
    """Run a list of rules in order, consuming transactions as they match.

    Each rule runs in two passes, and the second pass is the important one.

    *Pass 1* collects, for every unconsumed anchor, the set of counterparty
    records that satisfy the rule. An anchor with more than one hit is
    *forward-ambiguous* and is left for the scoring stages.

    *Pass 2* inverts that map. A counterparty record claimed by more than one
    anchor is *reverse-ambiguous*, and none of the claiming anchors may match
    it. Two identical payments competing for one ledger line is precisely the
    duplicate-payment case the spec warns about (section 1.1): matching either
    one would hide the duplicate and would silently depend on input order.

    Deciding both directions before consuming anything is also what makes the
    stage order-independent: a greedy single pass would let whichever anchor
    happened to come first in the list win.
    """
    remaining_a = list(side_a)
    remaining_b = list(side_b)
    consumed_a: set[UUID] = set()
    consumed_b: set[UUID] = set()
    matches: list[MatchGroup] = []
    stats: dict[str, int] = {}

    def bump(key: str, amount: int = 1) -> None:
        stats[key] = stats.get(key, 0) + amount

    for rule in rules:
        available_b = [tx for tx in remaining_b if tx.id not in consumed_b]
        if not available_b:
            break
        index = BlockingIndex.build(available_b)
        generator = CandidateGenerator.for_config(config)
        require_unique_for_rule = require_unique and rule.risk.require_unique_candidate

        # -- pass 1: what does each anchor want? ---------------------------
        proposals: dict[UUID, tuple[CanonicalTransaction, CanonicalTransaction, ScoredCandidate]] = {}
        claims: dict[UUID, list[UUID]] = {}

        for a in remaining_a:
            if a.id in consumed_a:
                continue

            partners = [
                b
                for b in generator.lookup(a, index)
                if b.id not in consumed_b and eligible(a, b, generator.eligibility)
            ]
            if not partners:
                continue

            hits: list[tuple[CanonicalTransaction, ScoredCandidate]] = []
            for b in sorted(partners, key=lambda tx: str(tx.id)):
                candidate = CandidateMatch(
                    id=uuid5(candidate_namespace, f"{context.run_id}:{a.id}:{b.id}"),
                    tenant_id=context.tenant_id,
                    run_id=context.run_id,
                    side_a_ids=(a.id,),
                    side_b_ids=(b.id,),
                    cardinality=MatchCardinality.ONE_TO_ONE,
                    generated_by=f"rule:{rule.versioned_id}",
                )
                scored = scorer.score_candidate(candidate, a, b, rule=rule)
                if scored is None:
                    continue
                if scored.hard_conflicts:
                    continue
                if scored.score * 100 < rule.decision.suggest_min_score:
                    continue
                hits.append((b, scored))

            if not hits:
                continue

            if require_unique_for_rule and len(hits) > 1:
                # Two records satisfy the rule equally for this anchor. Exactly
                # the case the spec says must not auto-match; leave it to scoring.
                bump(f"{rule.versioned_id}_ambiguous")
                continue

            b, scored = hits[0]
            proposals[a.id] = (a, b, scored)
            claims.setdefault(b.id, []).append(a.id)

        # -- pass 2: resolve contention, then consume ----------------------
        contested_b = {
            b_id for b_id, anchors in claims.items() if len(anchors) > 1
        }
        if contested_b:
            bump(
                f"{rule.versioned_id}_contested",
                sum(len(claims[b_id]) for b_id in contested_b),
            )

        rule_matches = 0
        # Sort by anchor id so the emitted order does not depend on the input
        # list order either.
        for anchor_id in sorted(proposals, key=str):
            a, b, scored = proposals[anchor_id]
            if require_unique_for_rule and b.id in contested_b:
                continue

            auto = scored.score * 100 >= rule.decision.auto_match_min_score
            if rule.risk.max_auto_match_amount is not None:
                auto = auto and abs(a.amount) <= rule.risk.max_auto_match_amount

            matches.append(
                make_group(
                    context=context,
                    rule=rule,
                    a=[a],
                    b=[b],
                    scored=scored,
                    stage=stage,
                    status=(
                        MatchGroupStatus.AUTO_APPROVED
                        if auto
                        else MatchGroupStatus.SUGGESTED
                    ),
                    decision=(
                        DecisionOutcome.AUTO_MATCH if auto else DecisionOutcome.SUGGEST
                    ),
                    confidence=scored.score,
                )
            )
            consumed_a.add(a.id)
            consumed_b.add(b.id)
            rule_matches += 1

        if rule_matches:
            bump(rule.versioned_id, rule_matches)

    return StageResult(
        matches=matches,
        remaining_a=[tx for tx in remaining_a if tx.id not in consumed_a],
        remaining_b=[tx for tx in remaining_b if tx.id not in consumed_b],
        stats=stats,
    )
