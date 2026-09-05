"""Shipped reconciliation templates (spec section 68).

Opinionated defaults matter: arbitrary configuration turns onboarding into
consulting (spec section 69). Every template here is editable per customer, but
a new tenant gets something that works on day one.

Templates are also the canonical example of the rule format, so the YAML in
``docs/matching-engine.md`` and these objects must stay in step - the test suite
checks they do.
"""

from __future__ import annotations

from decimal import Decimal

from packages.domain.dates import DateField
from packages.domain.models.reconciliation import (
    ControlsConfig,
    DecisionThresholds,
    GroupingConfig,
    ReconciliationConfig,
    SideConfig,
    ToleranceConfig,
)
from packages.matching.rules import (
    ConditionOperator,
    MatchingRule,
    RuleCondition,
    RuleDecision,
    RuleFilters,
    RuleRisk,
    RuleScope,
    RuleSet,
)

__all__ = [
    "BANK_GL_RULES",
    "STRIPE_PAYOUT_RULES",
    "TEMPLATES",
    "bank_gl_template",
    "default_rule_set",
    "stripe_payout_template",
]


# ---------------------------------------------------------------------------
# Template 1 - Bank <-> General Ledger
# ---------------------------------------------------------------------------

BANK_GL_RULES: tuple[MatchingRule, ...] = (
    MatchingRule(
        id="bank_gl_exact_reference",
        name="Exact amount + exact reference",
        version="v3",
        deterministic=True,
        stage="exact",
        description=(
            "Same currency, identical amount and identical normalised payment "
            "reference. The strongest bank/ledger evidence that exists."
        ),
        scope=RuleScope(source_a="bank", source_b="ledger"),
        filters=RuleFilters(currency_equal=True),
        conditions=(
            RuleCondition(
                field="amount",
                operator=ConditionOperator.EXACT,
                weight=50.0,
                code="AMOUNT_EXACT",
            ),
            RuleCondition(
                field="reference",
                operator=ConditionOperator.NORMALIZED_EXACT,
                weight=50.0,
                code="REFERENCE_EXACT",
            ),
        ),
        decision=RuleDecision(auto_match_min_score=100.0, suggest_min_score=100.0),
        risk=RuleRisk(require_unique_candidate=True, precision_estimate=0.9999),
    ),
    MatchingRule(
        id="bank_gl_exact_external_id",
        name="Exact external transaction ID",
        version="v2",
        deterministic=True,
        stage="exact",
        description="Both systems carry the same upstream transaction identifier.",
        scope=RuleScope(source_a="bank", source_b="ledger"),
        conditions=(
            RuleCondition(
                field="external_transaction_id",
                operator=ConditionOperator.NORMALIZED_EXACT,
                weight=60.0,
                code="EXTERNAL_ID_EXACT",
            ),
            RuleCondition(
                field="amount",
                operator=ConditionOperator.EXACT,
                weight=40.0,
                code="AMOUNT_EXACT",
            ),
        ),
        decision=RuleDecision(auto_match_min_score=100.0, suggest_min_score=100.0),
        risk=RuleRisk(require_unique_candidate=True, precision_estimate=0.9999),
    ),
    MatchingRule(
        id="bank_gl_invoice_amount_date",
        name="Exact amount + invoice number within 3 days",
        version="v2",
        deterministic=True,
        stage="exact",
        description=(
            "Identical amount and invoice number, posted within three days of "
            "each other. Spec section 13, rule B."
        ),
        scope=RuleScope(source_a="bank", source_b="ledger"),
        filters=RuleFilters(currency_equal=True, date_window_days=3),
        conditions=(
            RuleCondition(
                field="amount",
                operator=ConditionOperator.EXACT,
                weight=45.0,
                code="AMOUNT_EXACT",
            ),
            RuleCondition(
                field="invoice_number",
                operator=ConditionOperator.NORMALIZED_EXACT,
                weight=45.0,
                code="INVOICE_EXACT",
            ),
            RuleCondition(
                field="transaction_date",
                operator=ConditionOperator.WITHIN_DAYS,
                value=3,
                weight=10.0,
                code="DATE_WITHIN_WINDOW",
            ),
        ),
        decision=RuleDecision(auto_match_min_score=95.0, suggest_min_score=80.0),
        risk=RuleRisk(require_unique_candidate=True, precision_estimate=0.999),
    ),
    MatchingRule(
        id="bank_gl_amount_date_counterparty",
        name="Amount within tolerance, close date, same counterparty",
        version="v2",
        deterministic=False,
        stage="rule",
        description=(
            "No shared identifier. Amount within tolerance, dates close and the "
            "counterparty names resolve to the same entity. Suggest only."
        ),
        scope=RuleScope(source_a="bank", source_b="ledger"),
        filters=RuleFilters(currency_equal=True, date_window_days=5),
        conditions=(
            RuleCondition(
                field="amount",
                operator=ConditionOperator.WITHIN_ABSOLUTE,
                value=Decimal("0.50"),
                weight=40.0,
                code="AMOUNT_WITHIN_TOLERANCE",
            ),
            RuleCondition(
                field="transaction_date",
                operator=ConditionOperator.WITHIN_DAYS,
                value=3,
                weight=20.0,
                code="DATE_WITHIN_WINDOW",
            ),
            RuleCondition(
                field="counterparty",
                operator=ConditionOperator.SIMILARITY_AT_LEAST,
                value=0.90,
                weight=40.0,
                code="COUNTERPARTY_SIMILAR",
            ),
        ),
        # Never auto-matches: the score cannot reach 101.
        decision=RuleDecision(auto_match_min_score=101.0, suggest_min_score=75.0),
        risk=RuleRisk(require_unique_candidate=True, precision_estimate=0.0),
    ),
)


# ---------------------------------------------------------------------------
# Template 2 - Payment processor payout <-> Bank
# ---------------------------------------------------------------------------

STRIPE_PAYOUT_RULES: tuple[MatchingRule, ...] = (
    MatchingRule(
        id="processor_payout_net_and_reference",
        name="Bank deposit equals net payout, payout ID in the description",
        version="v3",
        deterministic=True,
        stage="exact",
        description=(
            "Spec section 13, rule C: the bank amount equals the payout's net "
            "amount and the payout ID appears inside the bank narrative."
        ),
        scope=RuleScope(source_a="bank", source_b="processor"),
        filters=RuleFilters(currency_equal=True, date_window_days=5),
        conditions=(
            RuleCondition(
                field="amount",
                operator=ConditionOperator.NET_EQUALS,
                field_b="net_amount",
                weight=50.0,
                code="NET_AMOUNT_EXACT",
            ),
            RuleCondition(
                field="raw_description",
                operator=ConditionOperator.CONTAINS,
                field_b="settlement_id",
                weight=50.0,
                code="SETTLEMENT_REFERENCE_EXACT",
            ),
        ),
        decision=RuleDecision(auto_match_min_score=100.0, suggest_min_score=100.0),
        risk=RuleRisk(require_unique_candidate=True, precision_estimate=0.9999),
    ),
    MatchingRule(
        id="processor_payout_settlement_id",
        name="Shared settlement identifier",
        version="v2",
        deterministic=True,
        stage="exact",
        description="Both sides carry the same settlement or payout identifier.",
        scope=RuleScope(source_a="bank", source_b="processor"),
        filters=RuleFilters(currency_equal=True, date_window_days=7),
        conditions=(
            RuleCondition(
                field="settlement_id",
                operator=ConditionOperator.NORMALIZED_EXACT,
                weight=60.0,
                code="SETTLEMENT_REFERENCE_EXACT",
            ),
            RuleCondition(
                field="amount",
                operator=ConditionOperator.WITHIN_ABSOLUTE,
                value=Decimal("0.00"),
                weight=40.0,
                code="AMOUNT_EXACT",
            ),
        ),
        decision=RuleDecision(auto_match_min_score=100.0, suggest_min_score=100.0),
        risk=RuleRisk(require_unique_candidate=True, precision_estimate=0.9999),
    ),
)


def default_rule_set(version: str = "v1") -> RuleSet:
    """Every shipped rule, for a tenant that has not customised anything."""
    return RuleSet(
        id="default",
        version=version,
        rules=BANK_GL_RULES + STRIPE_PAYOUT_RULES,
    )


def bank_gl_template(
    *,
    name: str = "Bank vs General Ledger",
    bank_account: str = "bank",
    ledger_account: str = "ledger",
    currency_tolerance: Decimal = Decimal("0.01"),
    date_days: int = 3,
) -> tuple[ReconciliationConfig, RuleSet]:
    """Template 1: keys are amount, date, reference and counterparty."""
    config = ReconciliationConfig(
        name=name,
        side_a=SideConfig(type="bank", account=bank_account, label="Bank"),
        side_b=SideConfig(type="ledger", account=ledger_account, label="General ledger"),
        frequency="monthly",
        rules=tuple(rule.id for rule in BANK_GL_RULES),
        tolerances=ToleranceConfig(
            date_days=date_days,
            amount_absolute=currency_tolerance,
            date_field=DateField.TRANSACTION,
        ),
        grouping=GroupingConfig(enabled=True, max_group_size=5),
        decision=DecisionThresholds(),
        controls=ControlsConfig(),
    )
    return config, RuleSet(id="bank_gl", version="v1", rules=BANK_GL_RULES)


def stripe_payout_template(
    *,
    name: str = "Stripe payout vs Bank",
    bank_account: str = "bank",
    date_days: int = 5,
) -> tuple[ReconciliationConfig, RuleSet]:
    """Template 2: keys are net amount, payout ID and bank arrival date."""
    config = ReconciliationConfig(
        name=name,
        side_a=SideConfig(type="bank", account=bank_account, label="Bank"),
        side_b=SideConfig(type="processor", account="stripe", label="Stripe payouts"),
        frequency="monthly",
        rules=tuple(rule.id for rule in STRIPE_PAYOUT_RULES),
        tolerances=ToleranceConfig(
            date_days=date_days,
            amount_absolute=Decimal("0.00"),
            date_field=DateField.TRANSACTION,
        ),
        grouping=GroupingConfig(enabled=True, max_group_size=6),
        decision=DecisionThresholds(),
        controls=ControlsConfig(),
    )
    return config, RuleSet(id="stripe_payout", version="v1", rules=STRIPE_PAYOUT_RULES)


TEMPLATES: dict[str, object] = {
    "bank_gl": bank_gl_template,
    "stripe_payout": stripe_payout_template,
}
