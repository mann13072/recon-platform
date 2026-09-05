# Accounting model

The platform keeps source facts, normalized facts, possible relationships, approved relationships, exceptions, and actions separate. Collapsing these concepts would make provenance and review impossible.

## The eleven concepts

| Concept | Meaning | Persistence |
| --- | --- | --- |
| Source record | Exact upstream row and checksum; immutable after import | `source_records`, with its parent file in `source_files` |
| Canonical transaction | Conservative normalized representation used for matching | `canonical_transactions`; field provenance in `transaction_lineage` |
| Reconciliation | Versioned configuration for comparing two sources | `reconciliation_definitions`, referring to `matching_rule_sets` |
| Reconciliation run | One execution for a period and frozen data/configuration | `reconciliation_runs` and `run_transaction_snapshots` |
| Candidate match | A possible relationship that has not been accepted | `candidate_matches` |
| Match group | A 1:1, 1:N, N:1, or N:N relationship and its evidence | `match_groups` and `match_group_members` |
| Exception | Unresolved item or controlled problem requiring workflow | `exceptions`; discussion in `exception_comments` |
| Decision | A system or human outcome such as suggest, approve, reject, close, or transition | There is no generic decision table. Current state and actors are stored on `match_groups`, `exceptions`, and `reconciliation_runs`; every action is also captured in `audit_events` |
| Evidence | Supporting file and retention metadata | `exception_evidence`; bytes live in object storage under its `storage_key` |
| Accounting action | Proposed downstream journal adjustment, never an automatic posting | `accounting_action_proposals` |
| Audit event | Immutable record of who did what, why, and in what sequence | `audit_events`, unique by tenant and sequence and linked by hashes |

Supporting identity and policy tables are `tenants`, `users`, `user_roles`, `connections`, `connector_credentials`, `entity_aliases`, `period_locks`, `ai_calls`, and `idempotency_keys`. The local user row is only a projection of an external identity provider; it contains no password.

Source rows are content-addressed and protected by `uq_source_records_identity`. Canonical transactions use tenant, connection, source record ID, and checksum as their identity. A transaction can belong to at most one active match group, and its allocated amount cannot exceed its own absolute amount. These are asserted by `packages.matching.engine.assert_invariants` before results are persisted.

## Four date concepts

The model preserves four dates because they answer different questions:

- `transaction_date`: when the underlying economic event occurred. Use it when both systems describe the event itself and the rule explicitly requires that date.
- `posting_date`: when a system recorded the entry. Ledger exports often provide this without a transaction date; period listing falls back to it.
- `value_date`: when funds begin or stop accruing value. It is useful for bank timing differences and is the third fallback for the effective date.
- `settlement_date`: when a processor batch or transfer settled. Settlement and payout rules may select it explicitly; it is not folded into the generic effective date.

No ingestion step overwrites these into a single field. `CanonicalTransaction.best_date` and the repository `_effective_date()` use transaction date, otherwise posting date, otherwise value date. That fallback supports sources with different date availability. A matching rule may instead request `transaction_date`, `posting_date`, `value_date`, or `settlement_date` strictly. The rule field `date` means the documented fallback, not an additional stored date.

## Money contract

Money is `Decimal` throughout Python. `packages.domain.money.Money` rejects a Python `float`; SQLAlchemy's `Money` type also rejects floats at the persistence boundary. Integers and decimal strings may be converted exactly. Arithmetic begins from `Decimal("0")`, and bounded grouping converts amounts to integer minor units before subset search.

Production columns use `NUMERIC(24, 8)`. SQLite cannot safely preserve that precision with its numeric affinity, so tests use a 25-character zero-padded, offset-scaled integer string. The encoding preserves exact values and numeric ordering. This is a portability mechanism, not a different money model.

The HTTP contract serializes every monetary value as a decimal string, for example:

```json
{
  "amount": "982.45",
  "currency": "EUR",
  "difference": "0.00"
}
```

Clients must not parse these values through binary floating point. Currency is always a separate three-character code. Formatting, rounding, and minor-unit conversion must know the currency; an unlabelled decimal is not sufficient accounting data.

## Runs, matches, and close

A run snapshot records both side transaction ID lists, source checksums, rule versions, configuration hash/version, optional model version, and its own snapshot hash. Reruns read this frozen snapshot rather than current tables. Candidate rows store features and explanations; they do not consume transactions. Only active match-group members consume allocation.

Exact deterministic rules can create auto-approved match groups. Grouped arithmetic relationships and ambiguous scoring results remain suggestions until reviewed. A resolved exception explains a difference but does not become a fabricated match. At close, unexplained difference is the raw side difference minus the signed contribution of resolved or closed exceptions.

A run closes only when required exceptions are resolved, approvals are complete, the remaining unexplained difference is within threshold, required evidence exists, and no source file has blocking quality errors. Closing records a certificate with balances, difference, period, actor, configuration version, snapshot hash, and a certificate hash bound to the run result.
