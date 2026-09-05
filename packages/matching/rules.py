"""Rule configuration (spec section 14): rules are data, not hardcoded branches.

A rule loaded from YAML carries its own version. Every match records the exact
rule version that produced it, so a result from three months ago can still be
explained after the rule has changed (spec sections 1.2 and 36).
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from packages.domain.dates import DateField, date_distance_days
from packages.domain.models.transaction import CanonicalTransaction
from packages.ingestion.normalization import description_contains
from packages.matching.fuzzy import (
    jaro_winkler_similarity,
    token_set_ratio,
    trigram_similarity,
)

__all__ = [
    "ConditionOperator",
    "MatchingRule",
    "RuleCondition",
    "RuleDecision",
    "RuleFilters",
    "RuleRisk",
    "RuleScope",
    "RuleSet",
    "evaluate_condition",
    "load_rule_set",
]


class ConditionOperator(StrEnum):
    EXACT = "exact"
    NORMALIZED_EXACT = "normalized_exact"
    WITHIN_DAYS = "within_days"
    WITHIN_ABSOLUTE = "within_absolute"
    WITHIN_PERCENTAGE = "within_percentage"
    SIMILARITY_AT_LEAST = "similarity_at_least"
    TRIGRAM_AT_LEAST = "trigram_at_least"
    TOKEN_SET_AT_LEAST = "token_set_at_least"
    CONTAINS = "contains"
    BOTH_PRESENT = "both_present"
    SIGN_OPPOSITE = "sign_opposite"
    NET_EQUALS = "net_equals"


# Fields a condition may address, and which transaction attribute each reads.
_FIELD_ACCESSORS: dict[str, str] = {
    "amount": "amount",
    "gross_amount": "gross_amount",
    "fee_amount": "fee_amount",
    "net_amount": "net_amount",
    "tax_amount": "tax_amount",
    "currency": "currency",
    "reference": "normalized_reference",
    "raw_reference": "reference",
    "invoice_number": "normalized_invoice_number",
    "external_transaction_id": "external_transaction_id",
    "bank_reference": "bank_reference",
    "settlement_id": "settlement_id",
    "payout_id": "payout_id",
    "batch_id": "batch_id",
    "counterparty": "normalized_counterparty",
    "counterparty_account": "counterparty_account",
    "description": "normalized_description",
    "raw_description": "description",
    "check_number": "check_number",
    "purchase_order": "purchase_order",
    "customer_id": "customer_id",
    "vendor_id": "vendor_id",
    "source_account_id": "source_account_id",
    "transaction_date": "transaction_date",
    "posting_date": "posting_date",
    "value_date": "value_date",
    "settlement_date": "settlement_date",
}


class RuleScope(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_a: str | None = None
    source_b: str | None = None


class RuleFilters(BaseModel):
    model_config = ConfigDict(frozen=True)

    currency_equal: bool = True
    date_window_days: int | None = None
    date_field: DateField = DateField.TRANSACTION
    direction_opposite: bool = False
    max_amount_difference: Decimal | None = None


class RuleCondition(BaseModel):
    model_config = ConfigDict(frozen=True)

    field: str
    operator: ConditionOperator
    value: Decimal | float | int | str | None = None
    weight: float = 0.0
    # A hard condition must hold or the rule does not fire at all. Weighted
    # conditions only contribute score.
    hard: bool = True
    # Compare a different field on side B (e.g. side A amount vs side B net).
    field_b: str | None = None
    code: str | None = None
    description: str | None = None

    @model_validator(mode="after")
    def _validate(self) -> RuleCondition:
        if self.field not in _FIELD_ACCESSORS:
            raise ValueError(f"unknown rule field: {self.field}")
        if self.field_b is not None and self.field_b not in _FIELD_ACCESSORS:
            raise ValueError(f"unknown rule field_b: {self.field_b}")
        needs_value = {
            ConditionOperator.WITHIN_DAYS,
            ConditionOperator.WITHIN_ABSOLUTE,
            ConditionOperator.WITHIN_PERCENTAGE,
            ConditionOperator.SIMILARITY_AT_LEAST,
            ConditionOperator.TRIGRAM_AT_LEAST,
            ConditionOperator.TOKEN_SET_AT_LEAST,
        }
        if self.operator in needs_value and self.value is None:
            raise ValueError(f"operator {self.operator} requires a value")
        if self.weight < 0:
            raise ValueError("condition weight must not be negative")
        return self

    @property
    def reason_code(self) -> str:
        if self.code:
            return self.code
        return f"{self.field.upper()}_{self.operator.name}"


class RuleDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    auto_match_min_score: float = 100.0
    suggest_min_score: float = 75.0


class RuleRisk(BaseModel):
    model_config = ConfigDict(frozen=True)

    require_unique_candidate: bool = True
    # Measured precision of this rule on approved history. Until a rule has
    # history the estimate stays conservative and the decision policy will not
    # auto-match on it alone.
    precision_estimate: float = 0.0
    max_auto_match_amount: Decimal | None = None


class MatchingRule(BaseModel):
    """One named, versioned matching rule."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    version: str = "v1"
    enabled: bool = True
    description: str = ""
    stage: str = "rule"
    deterministic: bool = False

    scope: RuleScope = Field(default_factory=RuleScope)
    filters: RuleFilters = Field(default_factory=RuleFilters)
    conditions: tuple[RuleCondition, ...] = ()
    decision: RuleDecision = Field(default_factory=RuleDecision)
    risk: RuleRisk = Field(default_factory=RuleRisk)

    @model_validator(mode="after")
    def _validate(self) -> MatchingRule:
        if not self.conditions:
            raise ValueError(f"rule {self.id} has no conditions")
        if self.deterministic and any(not c.hard for c in self.conditions):
            raise ValueError(
                f"rule {self.id} is marked deterministic but has weighted conditions"
            )
        return self

    @property
    def versioned_id(self) -> str:
        """The identifier stored on every match this rule produces."""
        return f"{self.id}_{self.version}".upper()

    @property
    def max_score(self) -> float:
        return sum(c.weight for c in self.conditions)

    def fingerprint(self) -> str:
        """Content hash, used to detect a rule edited without a version bump."""
        payload = self.model_dump(mode="json")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


class RuleSet(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    version: str
    rules: tuple[MatchingRule, ...]

    @model_validator(mode="after")
    def _validate(self) -> RuleSet:
        seen: set[str] = set()
        for rule in self.rules:
            if rule.versioned_id in seen:
                raise ValueError(f"duplicate rule version: {rule.versioned_id}")
            seen.add(rule.versioned_id)
        return self

    def enabled_rules(self) -> tuple[MatchingRule, ...]:
        """Deterministic rules first, then by descending max score, then by id.

        The ordering is total and content-independent of dict iteration, which
        is what makes a re-run reproduce the previous run exactly.
        """
        return tuple(
            sorted(
                (r for r in self.rules if r.enabled),
                key=lambda r: (not r.deterministic, -r.max_score, r.id),
            )
        )

    def by_id(self, rule_id: str) -> MatchingRule | None:
        for rule in self.rules:
            if rule.id == rule_id:
                return rule
        return None

    def versions(self) -> dict[str, str]:
        return {rule.id: rule.version for rule in self.rules}


def load_rule_set(data: dict[str, Any]) -> RuleSet:
    """Load a rule set from a parsed YAML/JSON document."""
    return RuleSet(
        id=data.get("id", "default"),
        version=str(data.get("version", "v1")),
        rules=tuple(MatchingRule(**rule) for rule in data.get("rules", [])),
    )


# ---------------------------------------------------------------------------
# Condition evaluation
# ---------------------------------------------------------------------------


def _get(tx: CanonicalTransaction, field: str) -> Any:
    return getattr(tx, _FIELD_ACCESSORS[field])


def evaluate_condition(
    condition: RuleCondition,
    a: CanonicalTransaction,
    b: CanonicalTransaction,
) -> tuple[bool, float]:
    """Evaluate one condition. Returns ``(satisfied, similarity_or_1)``.

    A condition on a missing value is never satisfied. Treating ``None == None``
    as a match would let two records with no reference "agree" on it, which is
    the single most common source of false matches in naive engines.
    """
    left = _get(a, condition.field)
    right = _get(b, condition.field_b or condition.field)
    op = condition.operator

    if op is ConditionOperator.BOTH_PRESENT:
        return (left is not None and right is not None), 1.0

    if op is ConditionOperator.SIGN_OPPOSITE:
        if left is None or right is None:
            return False, 0.0
        return (left > 0) != (right > 0) and left != 0 and right != 0, 1.0

    if op is ConditionOperator.CONTAINS:
        # Directional: does B's identifier appear inside A's description?
        return description_contains(str(left) if left else None,
                                    str(right) if right else None), 1.0

    if left is None or right is None:
        return False, 0.0

    if op in (ConditionOperator.EXACT, ConditionOperator.NORMALIZED_EXACT):
        if isinstance(left, str) and isinstance(right, str):
            if op is ConditionOperator.NORMALIZED_EXACT:
                return left.strip().upper() == right.strip().upper(), 1.0
            return left == right, 1.0
        return left == right, 1.0

    if op is ConditionOperator.NET_EQUALS:
        return Decimal(str(left)) == Decimal(str(right)), 1.0

    if op is ConditionOperator.WITHIN_DAYS:
        if not isinstance(left, date) or not isinstance(right, date):
            return False, 0.0
        distance = date_distance_days(left, right)
        limit = int(condition.value)  # type: ignore[arg-type]
        if distance is None or distance > limit:
            return False, 0.0
        # Closer dates score higher within the allowed window.
        return True, 1.0 - (distance / (limit + 1))

    if op is ConditionOperator.WITHIN_ABSOLUTE:
        difference = abs(Decimal(str(left)) - Decimal(str(right)))
        limit = Decimal(str(condition.value))
        if difference > limit:
            return False, 0.0
        return True, 1.0 if limit == 0 else float(1 - (difference / limit))

    if op is ConditionOperator.WITHIN_PERCENTAGE:
        base = abs(Decimal(str(right)))
        if base == 0:
            return Decimal(str(left)) == 0, 1.0
        ratio = abs(Decimal(str(left)) - Decimal(str(right))) / base
        limit = Decimal(str(condition.value))
        if ratio > limit:
            return False, 0.0
        return True, 1.0 if limit == 0 else float(1 - (ratio / limit))

    similarity_fn = {
        ConditionOperator.SIMILARITY_AT_LEAST: jaro_winkler_similarity,
        ConditionOperator.TRIGRAM_AT_LEAST: trigram_similarity,
        ConditionOperator.TOKEN_SET_AT_LEAST: token_set_ratio,
    }[op]
    similarity = similarity_fn(str(left), str(right))
    threshold = float(condition.value)  # type: ignore[arg-type]
    return similarity >= threshold, similarity
