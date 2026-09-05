# Accounting controls and permissions

Controls are enforced in services and packages, not left to UI convention. The UI may hide unavailable actions, but the API always rechecks permission, segregation of duties, materiality, period lock, version, and close conditions.

## Roles

The following table is generated from the effective sets in `ROLE_PERMISSIONS` (37 distinct permission values across eight roles).

| Role | Effective permissions |
| --- | --- |
| Administrator | `admin:users`, `ai:configure`, `audit:export`, `data:connections`, `rules:edit`, `rules:simulate`, `view:audit`, `view:dashboard`, `view:exceptions`, `view:matches`, `view:settings`, `view:transactions` |
| Approver | `ai:use`, `data:ingest`, `data:upload`, `exception:approve`, `exception:assign`, `exception:close`, `exception:comment`, `exception:evidence`, `exception:propose`, `journal:approve`, `journal:propose`, `match:approve`, `match:create`, `match:override`, `match:reject`, `match:unmatch`, `recon:run`, `rules:simulate`, `view:audit`, `view:dashboard`, `view:exceptions`, `view:matches`, `view:transactions` |
| Auditor | `audit:export`, `view:audit`, `view:dashboard`, `view:exceptions`, `view:matches`, `view:settings`, `view:transactions` |
| Controller | `ai:use`, `audit:export`, `data:ingest`, `data:upload`, `exception:approve`, `exception:assign`, `exception:close`, `exception:comment`, `exception:evidence`, `exception:propose`, `journal:approve`, `journal:propose`, `match:approve`, `match:create`, `match:override`, `match:reject`, `match:unmatch`, `period:lock`, `period:unlock`, `recon:create`, `recon:edit`, `recon:run`, `rules:edit`, `rules:simulate`, `run:close`, `run:reopen`, `thresholds:edit`, `view:audit`, `view:dashboard`, `view:exceptions`, `view:matches`, `view:settings`, `view:transactions` |
| Integration administrator | `data:connections`, `data:ingest`, `data:sync`, `data:upload`, `view:dashboard`, `view:exceptions`, `view:matches`, `view:transactions` |
| Preparer | `ai:use`, `data:ingest`, `data:upload`, `exception:assign`, `exception:comment`, `exception:evidence`, `exception:propose`, `journal:propose`, `match:create`, `recon:run`, `rules:simulate`, `view:dashboard`, `view:exceptions`, `view:matches`, `view:transactions` |
| Reviewer | `ai:use`, `data:ingest`, `data:upload`, `exception:assign`, `exception:comment`, `exception:evidence`, `exception:propose`, `journal:propose`, `match:approve`, `match:create`, `match:reject`, `match:unmatch`, `recon:run`, `rules:simulate`, `view:audit`, `view:dashboard`, `view:exceptions`, `view:matches`, `view:transactions` |
| Viewer | `view:dashboard`, `view:exceptions`, `view:matches`, `view:transactions` |

Roles describe maximum capability, not automatic authorization for every record. Non-human actors always lose privileged permissions even if a role would otherwise grant them.

## Maker-checker and segregation of duties

`packages/controls/segregation_of_duties.py` separates proposing from approving. A preparer can create a manual match, propose an exception resolution, or propose a journal, but approval requires the appropriate approval permission and a different eligible actor where maker-checker applies. Material adjustments cannot be self-approved. Manual match rejection, unmatch, override, reopen, and other sensitive actions require an actor and a written reason. These decisions are persisted and audited.

Optimistic locking is part of the control environment. Editable records carry `version`; mutation requests supply `expected_version`. An update whose version no longer matches raises `ConcurrencyConflict` instead of overwriting another reviewer's work. The client must reload and review before retrying.

## Materiality

Materiality is configuration, not a universal number. `packages/controls/materiality.py` classifies exposure against the reconciliation control threshold. When `manual_approval_above_materiality` is enabled, a scoring result at or above the materiality threshold cannot be auto-matched even when similarity evidence is strong. Journal and resolution workflows use approval limits and required roles. Threshold changes require permission and remain attributable through versioned configuration and audit history.

Materiality never makes a weak match acceptable. It can make a strong match require more review; it cannot lower evidence, ambiguity, or precision requirements.

## Period lock

`packages/controls/period_lock.py` checks an entity and accounting date against active `period_locks`. A lock has tenant, entity, start/end dates, actor, timestamp, and optional release/reason data. Posting or changing an accounting action in a locked period is refused. Controllers hold `period:lock` and `period:unlock`; ordinary preparers and reviewers do not.

Closing and period locking are distinct. Closing certifies a reconciliation run. A period lock protects accounting dates from later actions. Reopening a run requires `run:reopen` and a reason and does not silently erase the original close record.

## Five-condition close gate

`CloseService.preflight` exposes the same checks used by `CloseService.close`. A run can close only when:

1. all required exceptions are resolved or closed;
2. required approvals are completed—no suggested or proposed matches remain;
3. unexplained difference is less than or equal to `max_unexplained_difference`;
4. evidence exists for every configured evidence-required exception category; and
5. there are no source files in `QUALITY_FAILED` state.

Unexplained difference is not simply the raw difference. It subtracts the signed contribution of resolved/closed exceptions, because a reviewed timing item, fee, duplicate, or other resolution is an explanation. Closing creates a certificate containing the run, period, balances, remaining difference, actor and time, configuration version, snapshot hash, and a hash bound to the run's result.

## Evidence and retention

Evidence metadata is stored in `exception_evidence`; content is stored by content hash in tenant-prefixed object storage. MIME type, maximum size, empty content, unsafe filenames, executable signatures, and path traversal are checked before acceptance. Each record has a retention policy (default `default-7y`), uploader, upload time, SHA-256, and relation to an exception or match. Evidence-required categories block close until evidence is present.

Audit exports include an evidence manifest with hashes and retention metadata along with source, match, exception, approval, rule, model, event, and close data. Retention is therefore visible to reviewers and reproducible in the audit package rather than being an undocumented storage convention.
