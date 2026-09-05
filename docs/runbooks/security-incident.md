# Runbook: security incident

**Scope:** suspected credential compromise, unauthorised access, tampering with
financial records, or data exfiltration.

## Immediate actions (first 15 minutes)

1. **Declare.** Name an incident lead. Do not investigate alone and silently.
2. **Preserve.** Delete nothing - not instances, not logs, not rows. The audit
   log is append-only and is evidence.
3. **Contain, in this order:**
   - revoke the suspected credential at the identity provider;
   - rotate the affected connector credentials. They are encrypted per
     connection, so the blast radius is one connection;
   - if a tenant's data may have been reached, lock the affected periods so
     nothing can change while the investigation runs.
4. **Do not deploy a fix yet.** A deploy destroys forensic state.

## Establishing what happened

The audit log answers most questions directly.

```
GET /api/v1/audit/verify                    # is the chain intact?
GET /api/v1/audit/events?limit=2000         # everything, in order
GET /api/v1/audit/entity/MATCH_GROUP/{id}   # one entity's whole history
```

- **`verified: false`** means a historical event was altered. The response names
  the first invalid index; everything from there is suspect. This is the most
  serious finding in this runbook.
- **Filter by `actor_id`** to reconstruct one principal's activity.
- **`PERMISSION_DENIED` events** show attempts to exceed authority. A burst of
  them often precedes a successful escalation.

## Specific scenarios

### A leaked bearer token

Tokens are issued by the identity provider, so revocation happens there. Then:

1. List everything that subject did, filtered by actor.
2. Look for `MATCH_APPROVED`, `RUN_CLOSED`, `RULE_CHANGED` and
   `THRESHOLD_CHANGED`. Those change financial state or weaken a control.
3. Any reconciliation closed by the compromised actor is reopened, with the
   incident as the reason, and re-reviewed.

### A leaked connector credential

1. Rotate at the provider, then re-encrypt in `connector_credentials`.
2. Keys are derived per connection, so confirm the others were not also read
   rather than assuming they were all exposed.
3. Re-sync from a point before the compromise and compare checksums. Source
   records are immutable, so altered upstream data arrives as *new* records
   rather than overwriting the originals - the change is visible, not silent.

### Suspected tampering with financial records

1. `GET /api/v1/audit/verify` for every tenant.
2. For each closed run, recompute the close certificate hash and compare it with
   the stored value. The hash covers the run's result hash, so any change to the
   result invalidates the certificate.
3. Restore from backup to a point before the tampering (see
   `backup-restore.md`) and diff, rather than trusting the live database.

## Never do these

- Never delete audit events, for any reason, including "to clean up noise".
- Never edit a source record. A correction is a new record.
- Never disable the audit chain to make a deploy easier.
- Never put a token, key or account number in an incident ticket. Reference the
  credential's fingerprint instead.

## After the incident

- Write the timeline from audit events, not from memory.
- Notify affected tenants. A customer whose reconciliation was closed by an
  attacker needs to hear it from you, not from their auditor.
- Add a regression test for the specific path that was abused.
- Review whether the permission that allowed it was scoped correctly.
