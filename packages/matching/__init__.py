"""The matching engine: deterministic first, explainable always."""

from packages.matching.ambiguity import (
    AmbiguityAnalysis,
    AmbiguityAnalyzer,
    PolicyEngine,
    confidence_from_score,
    decide,
)
from packages.matching.bipartite import Assignment, assign_one_to_one
from packages.matching.candidate_generation import (
    BlockingIndex,
    CandidateGenerator,
    EligibilityConfig,
    eligible,
)
from packages.matching.engine import (
    MatchExclusivityError,
    MatchingContext,
    MatchingEngine,
    MatchingResult,
    assert_invariants,
)
from packages.matching.exact import (
    ExactMatcher,
    RuleMatcher,
    StageContext,
    StageResult,
)
from packages.matching.grouping import GroupMatcher, SubsetSearchResult, find_subsets
from packages.matching.rules import (
    ConditionOperator,
    MatchingRule,
    RuleCondition,
    RuleSet,
    load_rule_set,
)
from packages.matching.scoring import FeatureWeights, Scorer, extract_features
from packages.matching.templates import (
    bank_gl_template,
    default_rule_set,
    stripe_payout_template,
)
from packages.matching.tolerances import amount_close, date_close, fee_explains_difference

__all__ = [
    "AmbiguityAnalysis",
    "AmbiguityAnalyzer",
    "Assignment",
    "BlockingIndex",
    "CandidateGenerator",
    "ConditionOperator",
    "EligibilityConfig",
    "ExactMatcher",
    "FeatureWeights",
    "GroupMatcher",
    "MatchExclusivityError",
    "MatchingContext",
    "MatchingEngine",
    "MatchingResult",
    "MatchingRule",
    "PolicyEngine",
    "RuleCondition",
    "RuleMatcher",
    "RuleSet",
    "Scorer",
    "StageContext",
    "StageResult",
    "SubsetSearchResult",
    "amount_close",
    "assert_invariants",
    "assign_one_to_one",
    "bank_gl_template",
    "confidence_from_score",
    "date_close",
    "decide",
    "default_rule_set",
    "eligible",
    "extract_features",
    "fee_explains_difference",
    "find_subsets",
    "load_rule_set",
    "stripe_payout_template",
]
