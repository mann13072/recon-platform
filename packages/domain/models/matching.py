"""Matching domain objects: features, candidates, groups, decisions.

These are pure data. The engine in ``packages.matching`` produces them; the API
and persistence layers store them. Nothing here touches a database.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from packages.domain.enums import (
    DecisionOutcome,
    MatchCardinality,
    MatchGroupStatus,
    Side,
)

__all__ = [
    "CandidateMatch",
    "DecisionResult",
    "MatchFeatures",
    "MatchGroup",
    "MatchGroupMember",
    "MatchReason",
    "MatchWarning",
    "ScoredCandidate",
]


class MatchFeatures(BaseModel):
    """Explainable per-candidate features (spec section 17).

    Every field is deterministic and computable from the two transactions. No
    model output is stored here.
    """

    model_config = ConfigDict(frozen=True)

    amount_exact: bool = False
    amount_difference: Decimal = Decimal("0")
    amount_difference_pct: Decimal | None = None

    currency_exact: bool = False

    reference_exact: bool = False
    reference_similarity: float = 0.0

    invoice_exact: bool = False

    counterparty_exact: bool = False
    counterparty_similarity: float = 0.0

    date_distance_days: int | None = None
    value_date_distance_days: int | None = None

    settlement_exact: bool = False
    external_id_exact: bool = False
    description_similarity: float = 0.0
    description_contains_reference: bool = False

    net_amount_exact: bool = False

    historical_pattern_score: float = 0.0


class MatchReason(BaseModel):
    """One evidence code shown to the user (spec section 40)."""

    model_config = ConfigDict(frozen=True)

    code: str
    contribution: float
    description: str


class MatchWarning(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    description: str


class CandidateMatch(BaseModel):
    """A possible relationship between transactions before scoring."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    tenant_id: UUID
    run_id: UUID
    side_a_ids: tuple[UUID, ...]
    side_b_ids: tuple[UUID, ...]
    cardinality: MatchCardinality
    generated_by: str

    @property
    def sort_key(self) -> tuple[str, ...]:
        """Stable ordering key. Reproducibility depends on never sorting by
        score alone, because equal scores would then order arbitrarily."""
        return tuple(sorted(str(i) for i in self.side_a_ids)) + tuple(
            sorted(str(i) for i in self.side_b_ids)
        )


class ScoredCandidate(BaseModel):
    """A candidate plus its evidence score and explanation."""

    model_config = ConfigDict(frozen=True)

    candidate: CandidateMatch
    features: MatchFeatures
    score: float
    reasons: tuple[MatchReason, ...] = ()
    warnings: tuple[MatchWarning, ...] = ()
    hard_conflicts: int = 0
    rule_id: str | None = None
    rule_version: str | None = None
    rule_precision_estimate: float = 0.0
    total_amount: Decimal = Decimal("0")
    currency: str | None = None

    @property
    def sort_key(self) -> tuple[float, tuple[str, ...]]:
        """Descending score, then candidate identity as a deterministic tiebreak."""
        return (-self.score, self.candidate.sort_key)


class DecisionResult(BaseModel):
    """The outcome of the decision policy for one anchor transaction set."""

    model_config = ConfigDict(frozen=True)

    outcome: DecisionOutcome
    top: ScoredCandidate | None = None
    second: ScoredCandidate | None = None
    score_margin: float = 0.0
    competing_candidate_count: int = 0
    confidence: float = 0.0
    policy_codes: tuple[str, ...] = ()
    explanation: str = ""


class MatchGroupMember(BaseModel):
    model_config = ConfigDict(frozen=True)

    transaction_id: UUID
    side: Side
    allocated_amount: Decimal


class MatchGroup(BaseModel):
    """An approved (or proposed) relationship of arbitrary cardinality."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    tenant_id: UUID
    run_id: UUID
    reconciliation_id: UUID

    members: tuple[MatchGroupMember, ...]
    cardinality: MatchCardinality
    status: MatchGroupStatus

    decision: DecisionOutcome
    confidence: float
    score: float
    currency: str

    rule_id: str | None = None
    rule_version: str | None = None
    rule_set_version: str | None = None
    model_version: str | None = None
    engine_stage: str = ""

    reasons: tuple[MatchReason, ...] = ()
    warnings: tuple[MatchWarning, ...] = ()
    competing_candidate_count: int = 0

    created_by_actor_type: str = "MATCH_ENGINE"
    created_at: datetime | None = None
    approved_by: UUID | None = None
    approved_at: datetime | None = None
    override_reason: str | None = None
    version: int = 1

    metadata: dict[str, str] = Field(default_factory=dict)

    @property
    def side_a_ids(self) -> tuple[UUID, ...]:
        return tuple(m.transaction_id for m in self.members if m.side is Side.A)

    @property
    def side_b_ids(self) -> tuple[UUID, ...]:
        return tuple(m.transaction_id for m in self.members if m.side is Side.B)

    @property
    def transaction_ids(self) -> tuple[UUID, ...]:
        return tuple(m.transaction_id for m in self.members)

    @property
    def total_amount(self) -> Decimal:
        return sum(
            (m.allocated_amount for m in self.members if m.side is Side.A),
            Decimal("0"),
        )
