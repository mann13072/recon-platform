# Matching engine

`MatchingEngine.run` is a deterministic, staged pipeline. Earlier stages consume proven relationships; later stages only see the remaining records. At every match-producing boundary, `assert_invariants` checks exclusivity and prevents over-allocation.

## The eight stages

1. **Eligibility.** Rules and candidate pools reject incompatible tenant/source context, currency, direction, missing required fields, or dates outside configured windows. Eligibility narrows what may be compared; it does not create a match.
2. **Exact deterministic matching.** `ExactMatcher` evaluates versioned exact rules. Each rule stage first collects proposals, then rejects counterparties claimed by more than one anchor, making duplicate handling order-independent.
3. **Rules and tolerances.** `RuleMatcher` applies non-exact versioned conditions such as invoice/amount/date or amount/date/counterparty. Precision floors and risk flags remain part of the decision.
4. **Bounded grouping.** `GroupMatcher` searches for 1:N and N:1 sums in integer minor units. A grouping result is always a suggestion, never an automatic approval based on arithmetic alone.
5. **Candidate generation.** `CandidateGenerator` creates a bounded set from indexes/buckets rather than comparing every row on side A with every row on side B.
6. **Evidence scoring.** `Scorer.score_candidate` evaluates available amount, identifier, date, counterparty, text, and other signals. The score is earned weight divided by weight available on both records. If neither side has a strong identifier, a 45-point penalty applies.
7. **Ambiguity analysis.** `AmbiguityAnalyzer` ranks candidates per anchor, retains the runner-up, counts competitors, and detects reverse contention where multiple anchors want the same counterparty.
8. **Decision policy.** `PolicyEngine.decide_all` combines score-derived confidence, margin, conflicts, contention, materiality, rule reliability, and thresholds. A global bipartite assignment has the final say for surviving 1:1 pairs.

Items not matched by these eight stages flow to `packages.exceptions`; exception handling is downstream rather than an additional matching algorithm.

## Why 96 versus 95 does not auto-match

Suppose one anchor has Candidate A with score 96 and Candidate B with score 95. In `MatchingEngine.run`, scored candidates are sorted, then `AmbiguityAnalyzer.analyze` stores A as `top` and B as `second`. `decide` computes a margin of one point (or `0.01` on a 0-to-1 scale). If this is below `minimum_candidate_margin`, the `MARGIN_TOO_SMALL` blocker prevents `AUTO_MATCH` even if the top candidate clears the auto threshold. The result can be a suggestion if it clears the suggestion threshold; otherwise it becomes an exception.

The engine also calls `reverse_contention` and `assign_one_to_one`. Therefore a locally best candidate cannot be used if another anchor contests it or the global assignment selects a different pairing. This is the actual code path that enforces the specification's 96-versus-95 example; score alone is never the decision.

## Rule configuration

Rules are versioned `MatchingRule` data selected by a reconciliation configuration. The golden exact fixture uses this real YAML shape:

```yaml
reconciliation:
  name: "Exact match"
  side_a:
    type: bank
    account: bank
  side_b:
    type: ledger
    account: ledger
  period:
    frequency: monthly
  rules:
    - bank_gl_exact_reference
    - bank_gl_exact_external_id
    - bank_gl_invoice_amount_date
    - bank_gl_amount_date_counterparty
  tolerances:
    date_days: 3
    amount_absolute: "0.01"
  grouping:
    enabled: false
    max_group_size: 5
  decision:
    auto_match_threshold: 0.995
    suggested_match_threshold: 0.80
    minimum_candidate_margin: 0.08
  controls:
    materiality_threshold: "50000.00"
    manual_approval_above_materiality: true
```

The named rules resolve to objects in `packages/matching/templates.py` and `packages/matching/rules.py`. Individual conditions include a field, operator, weight, reason code, and optional parameters. A condition may use `date` for the transaction/posting/value fallback or a specific date field for strict comparison. Changing rules creates a new version; it is not a hidden code branch.

## Score, confidence, ambiguity, and risk

These values answer different questions:

- **Evidence score** measures how strongly the two records agree on the signals actually available on both. It is explainable through per-feature reason codes.
- **Confidence** is the policy-facing estimate derived from score. `confidence_from_score` is explicitly uncalibrated until labelled outcomes exist; it is not claimed as empirical probability.
- **Ambiguity** describes alternatives: runner-up margin, candidate count, and reverse contention. Strong evidence can still be ambiguous.
- **Risk** applies controls independent of similarity, including hard conflicts, materiality, required approval, and measured rule precision. A likely match may still be unsafe to automate.

The optimization order is precision, explainability, auditability, then recall. A false match is worse than an unmatched record.

## Bounded subset sums

`find_subsets` works over integer minor units and sorts by descending absolute amount. Suffix-positive and suffix-negative totals prune branches that cannot reach the target. It enforces all of the following bounds: a prefiltered `max_candidate_pool`, `max_group_size`, `max_solutions`, `node_budget`, and `time_budget_ms`. The clock is checked every 1,024 nodes to avoid excessive timing overhead.

The search continues after its first solution because multiple ways to reach the same total are accounting ambiguity. A group is created only when the bounded search is exhaustive and produces exactly one real multi-record solution. A competing single-record solution also makes the result ambiguous. If the node or time budget is exhausted, `exhausted` is false, `budget_exhausted` is counted, and no match group is created. The records remain unmatched for human review; the engine never treats an incomplete search as proof of uniqueness.
