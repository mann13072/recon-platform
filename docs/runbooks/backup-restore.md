# Runbook: backup and restore

**Owner:** platform engineering
**Reviewed:** quarterly, and after any schema change that adds a table

Financial records that cannot be restored are not records. This runbook is
written to be executed during a drill, not read.

## Objectives

| Metric | Target | Rationale |
| --- | --- | --- |
| RPO (recovery point objective) | 5 minutes | Continuous WAL archiving; at most five minutes of ingested transactions can be lost. |
| RTO (recovery time objective) | 2 hours | A tenant can tolerate a morning without the platform. It cannot tolerate a wrong balance. |
| Retention | 35 days point-in-time, 7 years of monthly archives | Seven years matches the evidence retention policy in `docs/controls.md`. |

## What is backed up

| Store | Method | Frequency |
| --- | --- | --- |
| PostgreSQL | Automated snapshot plus continuous WAL archiving | snapshot daily, WAL continuous |
| Object storage (uploads, evidence, exports) | Cross-region replication with versioning and object lock | continuous |
| Secrets | Managed by the cloud KMS; keys are never exported | n/a |
| Rule sets and configuration | Rows in PostgreSQL, covered by the database backup | n/a |

Uploaded source files are included because they are the only copy of the
original data. Losing them breaks lineage even if every derived row survives.

## Restore drill

Run this **quarterly**, into an isolated account. A backup that has never been
restored is a hypothesis.

1. **Pick a target.** A timestamp roughly 24 hours old, mid business day, so the
   WAL segment is non-trivial.
2. **Provision.** Restore the snapshot into a fresh instance, then replay WAL to
   the chosen timestamp.
3. **Point an API at it.** Deploy the current image with `DATABASE_URL` set to
   the restored instance and `ENVIRONMENT=drill`.
4. **Verify structure.** `alembic current` reports the expected revision.
5. **Verify the audit chain.** For every tenant, call `GET /api/v1/audit/verify`.
   Every tenant must return `verified: true`. A broken chain means the restore is
   partial and must not be promoted.
6. **Verify reproducibility.** Pick a closed run and call
   `POST /api/v1/runs/{id}/replay`. `reproducible` must be `true`: the snapshot,
   the rules and the transactions survived together.
7. **Verify object storage.** Download one evidence file and compare its SHA-256
   with `exception_evidence.sha256`.
8. **Verify a close certificate.** Recompute one closed run's certificate hash
   and compare it with the stored value.
9. **Record the timings.** Note the RPO and RTO actually achieved against the
   targets above.
10. **Destroy the drill environment.** It holds real customer data.

## Real incident

Follow the drill, with these differences.

- Declare the incident and freeze deployments before restoring.
- Restore to the last point *before* the corrupting event, not to the latest.
- Do not delete the damaged instance. It is evidence.
- After promoting the restored instance, re-run every reconciliation whose
  period overlaps the lost window and tell the affected tenants.
- **Closed runs are not re-executed.** A closed run carries a certificate a human
  signed; re-running it would produce a second result for a period already
  signed off. If a closed run's data was lost, reopen it formally with the
  incident as the reason, which is itself an audit event.

## What breaks if this is skipped

Point-in-time recovery depends on an unbroken WAL chain, and a failed archive
job is silent until someone attempts a restore. That is why the drill is
quarterly and why step 5 verifies the audit chain rather than only checking that
the service starts.
