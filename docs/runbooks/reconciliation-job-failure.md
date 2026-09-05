# Runbook: reconciliation run failure

**Symptom:** a run finished `FAILED`, or never left `RUNNING`.

## Status meanings

| Status | Meaning |
| --- | --- |
| `RUNNING` | Matching in progress. |
| `REVIEW_REQUIRED` | Finished; suggestions or exceptions need a human. |
| `READY_TO_CLOSE` | Finished with nothing outstanding. |
| `FAILED` | The run was refused and nothing was saved. |

A run is not "complete" merely because matching finished (spec section 90).
`REVIEW_REQUIRED` is a successful run.

## 1. `FAILED` with `invariant_violation`

The engine produced a result that violated an accounting integrity invariant -
a transaction in two active match groups, or more allocated than exists - and
the run was refused rather than persisted.

**This is a platform bug, not a data problem. Do not retry.**

1. Capture `failure_reason` and the run ID.
2. Reproduce offline. The snapshot is in `run_transaction_snapshots`, so
   `python scripts/replay_reconciliation.py <run-id>` re-executes the exact
   input that failed.
3. Add the failing case as a fixture under `tests/reconciliation_fixtures/`.
4. Escalate to engineering. Nothing was written, so the tenant's data is intact
   and there is no cleanup.

## 2. Stuck in `RUNNING`

The worker died mid-run.

1. Check worker liveness and queue depth.
2. The run holds no locks. A stuck run blocks nothing except the close gate.
3. Do not delete it. Start a fresh run; the old one keeps its snapshot and stays
   explainable.

## 3. Completed, but matched far less than expected

Check in this order.

1. **Both sides have data.** `GET /runs/{id}/summary`. If `side_b_balance` is 0,
   one side selected nothing - usually the period filter, or the side's
   configured source system does not match what was ingested.
2. **The connector is healthy.** A failed connector must never produce a
   "complete" reconciliation. Check its health state.
3. **Tolerances.** A zero-day date tolerance against a ledger that posts two
   days late matches nothing.
4. **Identifiers survived mapping.** `GET /transactions/{id}/lineage` shows which
   source column produced each canonical field. A reference mapped from the
   wrong column is the most common cause.
5. **Rule versions.** `run_transaction_snapshots.rule_versions` records exactly
   which rules ran. A recent rule change is the usual cause of a sudden drop.

## 4. Results changed between two runs of the same data

This should be impossible for the same snapshot. Confirm with
`POST /api/v1/runs/{id}/replay`. If `reproducible` is `false`, the engine has a
non-determinism bug - most likely iteration over a set or dict without a stable
sort. Escalate, and close no reconciliation until it is resolved.

Two *different* runs may legitimately differ if data was imported between them.
Compare the two snapshots' `snapshot_hash` before concluding anything.

## 5. Candidate explosion or a very slow run

`stage_stats.candidates.generated` divided by the row count should stay roughly
constant as data grows. A rising value means a blocking key stopped
discriminating - typically a source that now sends the same reference on every
row. Check `stage_stats` for a `_contested` count, which indicates many records
competing for the same counterparty.

Grouping has explicit node and time budgets, so it can slow a run but cannot
hang it. `stage_stats.grouping.budget_exhausted` counts anchors whose search was
truncated; those are left unmatched rather than guessed.
