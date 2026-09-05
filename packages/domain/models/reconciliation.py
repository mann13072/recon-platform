"""Reconciliation definitions, configuration, runs and snapshots.

The configuration model mirrors spec section 70 so a YAML file from the docs
loads directly into :class:`ReconciliationConfig`.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from packages.domain.dates import DateField
from packages.domain.enums import ExceptionCategory, ReconciliationStatus

__all__ = [
    "CloseCertificate",
    "DecisionThresholds",
    "GroupingConfig",
    "ReconciliationConfig",
    "ReconciliationDefinition",
    "ReconciliationRun",
    "RunSnapshot",
    "RunSummary",
    "SideConfig",
    "ToleranceConfig",
    "ControlsConfig",
]


class SideConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: str
    account: str | None = None
    connection_id: UUID | None = None
    label: str | None = None


class ToleranceConfig(BaseModel):
    """Spec section 16. Tolerances are configuration, never hardcoded."""

    model_config = ConfigDict(frozen=True)

    date_days: int = 0
    amount_absolute: Decimal = Decimal("0")
    amount_percentage: Decimal | None = None
    fee_absolute: Decimal = Decimal("0")
    fx_percentage: Decimal | None = None
    rounding_absolute: Decimal = Decimal("0")
    date_field: DateField = DateField.TRANSACTION

    @property
    def max_amount_difference(self) -> Decimal:
        return max(self.amount_absolute, self.fee_absolute, self.rounding_absolute)


class GroupingConfig(BaseModel):
    """Bounds for 1:N / N:1 search (spec section 20)."""

    model_config = ConfigDict(frozen=True)

    enabled: bool = False
    max_group_size: int = 5
    max_candidate_pool: int = 60
    max_solutions: int = 8
    node_budget: int = 200_000
    time_budget_ms: int = 2_000
    require_unique_solution: bool = True

    @model_validator(mode="after")
    def _validate(self) -> GroupingConfig:
        if self.max_group_size < 1:
            raise ValueError("max_group_size must be >= 1")
        if self.max_group_size > 12:
            # Beyond this the search space is unmanageable and the resulting
            # groups are not reviewable by a human anyway.
            raise ValueError("max_group_size above 12 is not supported")
        return self


class DecisionThresholds(BaseModel):
    """Spec sections 18 and 19."""

    model_config = ConfigDict(frozen=True)

    auto_match_threshold: float = 0.995
    suggested_match_threshold: float = 0.80
    minimum_candidate_margin: float = 0.08
    min_rule_precision: float = 0.999
    require_unique_candidate: bool = True

    @model_validator(mode="after")
    def _validate(self) -> DecisionThresholds:
        if not 0.0 < self.suggested_match_threshold <= self.auto_match_threshold <= 1.0:
            raise ValueError("thresholds must satisfy 0 < suggest <= auto <= 1")
        if self.minimum_candidate_margin < 0:
            raise ValueError("minimum_candidate_margin must not be negative")
        return self


class ControlsConfig(BaseModel):
    """Spec sections 33 and 57."""

    model_config = ConfigDict(frozen=True)

    materiality_threshold: Decimal = Decimal("50000.00")
    manual_approval_above_materiality: bool = True
    always_review_types: tuple[str, ...] = ()
    maker_checker_required: bool = True
    max_unexplained_difference: Decimal = Decimal("0.00")
    require_evidence_for_categories: tuple[ExceptionCategory, ...] = ()


class ReconciliationConfig(BaseModel):
    """Full configuration for one reconciliation (spec section 70)."""

    model_config = ConfigDict(frozen=True)

    name: str
    side_a: SideConfig
    side_b: SideConfig
    frequency: str = "monthly"
    allow_fx: bool = False
    rules: tuple[str, ...] = ()
    tolerances: ToleranceConfig = Field(default_factory=ToleranceConfig)
    grouping: GroupingConfig = Field(default_factory=GroupingConfig)
    decision: DecisionThresholds = Field(default_factory=DecisionThresholds)
    controls: ControlsConfig = Field(default_factory=ControlsConfig)
    max_candidates_per_transaction: int = 50
    config_version: str = "v1"

    @classmethod
    def from_yaml_dict(cls, data: dict[str, Any]) -> ReconciliationConfig:
        """Load the exact YAML shape used in the spec and the docs."""
        block = data.get("reconciliation", data)
        period = block.get("period") or {}
        return cls(
            name=block["name"],
            side_a=SideConfig(**block["side_a"]),
            side_b=SideConfig(**block["side_b"]),
            frequency=period.get("frequency", "monthly"),
            allow_fx=bool(block.get("allow_fx", False)),
            rules=tuple(block.get("rules", ())),
            tolerances=ToleranceConfig(**(block.get("tolerances") or {})),
            grouping=GroupingConfig(**(block.get("grouping") or {})),
            decision=DecisionThresholds(**(block.get("decision") or {})),
            controls=ControlsConfig(**(block.get("controls") or {})),
            config_version=str(block.get("config_version", "v1")),
        )


class ReconciliationDefinition(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    tenant_id: UUID
    name: str
    template: str | None = None
    config: ReconciliationConfig
    rule_set_id: UUID | None = None
    created_by: UUID | None = None
    created_at: datetime | None = None
    version: int = 1


class RunSnapshot(BaseModel):
    """Spec section 36. A run must be reproducible from this alone."""

    model_config = ConfigDict(frozen=True)

    run_id: UUID
    tenant_id: UUID
    reconciliation_id: UUID
    side_a_transaction_ids: tuple[UUID, ...]
    side_b_transaction_ids: tuple[UUID, ...]
    source_checksums: dict[str, str]
    rule_set_version: str
    rule_versions: dict[str, str]
    config_version: str
    config_hash: str
    model_version: str | None = None
    fx_rate_source: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    initiated_by: UUID | None = None
    created_at: datetime | None = None
    snapshot_hash: str = ""


class ReconciliationRun(BaseModel):
    """One execution of a reconciliation against a snapshot."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    tenant_id: UUID
    reconciliation_id: UUID
    status: ReconciliationStatus = ReconciliationStatus.DRAFT
    period_start: date | None = None
    period_end: date | None = None
    snapshot: RunSnapshot | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    initiated_by: UUID | None = None
    idempotency_key: str | None = None
    failure_reason: str | None = None
    version: int = 1


class RunSummary(BaseModel):
    """Numbers behind the dashboard (spec section 41)."""

    model_config = ConfigDict(frozen=True)

    run_id: UUID
    reconciliation_id: UUID
    status: ReconciliationStatus
    period_start: date | None = None
    period_end: date | None = None
    currency: str = "EUR"

    side_a_balance: Decimal = Decimal("0")
    side_b_balance: Decimal = Decimal("0")
    difference: Decimal = Decimal("0")

    matched_amount: Decimal = Decimal("0")
    matched_transaction_count: int = 0
    auto_matched_count: int = 0
    human_approved_count: int = 0
    suggested_count: int = 0
    exception_count: int = 0
    high_risk_exception_count: int = 0
    unmatched_a_count: int = 0
    unmatched_b_count: int = 0
    oldest_exception_age_days: int | None = None
    completion_pct: float = 0.0

    # Never headline automation rate without the false-match rate beside it.
    auto_match_rate: float = 0.0
    false_match_rate: float | None = None
    manual_review_rate: float = 0.0


class CloseCertificate(BaseModel):
    """Signed summary produced when a run closes (spec section 58)."""

    model_config = ConfigDict(frozen=True)

    run_id: UUID
    period: str
    status: ReconciliationStatus
    book_balance: Decimal
    external_balance: Decimal
    difference: Decimal
    unresolved_items: int
    closed_by: UUID
    closed_at: datetime
    config_version: str
    snapshot_hash: str
    certificate_hash: str = ""
