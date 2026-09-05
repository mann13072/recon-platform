# API reference

The FastAPI application exposes versioned business routes under `/api/v1`. `/health` and `/health/ready` are unversioned and unauthenticated. OpenAPI is available at `/openapi.json` and interactive documentation at `/docs` in the current application configuration.

## Authentication and common behavior

Send the externally issued JWT as:

```http
Authorization: Bearer <token>
```

The token must be valid for the configured issuer and audience and contain the subject and tenant claims expected by `apps/api/app/infrastructure/auth.py`. The tenant and active-user projection are checked before a business route runs. `GET /api/v1/me` returns the effective `permissions[]`; clients should use that list to render actions, while treating API authorization as the final control.

Money is always a JSON **string**, never a JSON number. Examples include `"amount": "982.45"`, `"side_a_balance": "1200.00"`, and `"difference": "0.00"`. Clients must retain decimal strings or use an exact decimal library; parsing through `float`, JavaScript `Number`, or `parseFloat` violates the contract.

Responses include `X-Correlation-ID`; callers may supply that header to correlate a workflow. Security headers disable caching and framing. Lists currently use route-specific query filters plus `limit` and `offset` where exposed.

## Identity

- `GET /api/v1/me` — current external subject, tenant, roles, actor type, and effective permissions.

## Files and ingestion

- `GET /api/v1/files` — list tenant files.
- `POST /api/v1/files` — upload a source file; multipart content is scanned/validated, hashed, and stored.
- `GET /api/v1/files/{file_id}/profile` — column profile and mapping suggestions.
- `POST /api/v1/files/{file_id}/mapping` — confirm or change column mappings.
- `POST /api/v1/files/{file_id}/ingest` — normalize and persist only after mapping confirmation and the quality gate.

## Transactions

- `GET /api/v1/transactions` — filtered tenant transaction list.
- `GET /api/v1/transactions/{transaction_id}` — one canonical transaction.
- `GET /api/v1/transactions/{transaction_id}/lineage` — per-field source and transformation lineage.

## Reconciliations and runs

- `GET /api/v1/reconciliations` — list definitions.
- `POST /api/v1/reconciliations` — create a versioned definition from configuration/template data.
- `GET /api/v1/reconciliations/{reconciliation_id}` — retrieve one definition.
- `POST /api/v1/reconciliations/{reconciliation_id}/runs` — freeze inputs and start a run.
- `GET /api/v1/runs/{run_id}` — run state and version.
- `GET /api/v1/runs/{run_id}/summary` — balances, difference, match/exception counts, completion, automation rate, and false-match rate.
- `GET /api/v1/runs/{run_id}/matches` — match groups for the run.
- `GET /api/v1/runs/{run_id}/exceptions` — exceptions for the run.
- `POST /api/v1/runs/{run_id}/investigate` — request optional, policy-checked AI investigation.
- `POST /api/v1/runs/{run_id}/replay` — replay the frozen snapshot and compare reproducibility.
- `GET /api/v1/runs/{run_id}/close-preflight` — five close-gate blockers and unexplained difference.
- `POST /api/v1/runs/{run_id}/close` — close and create a signed certificate.
- `POST /api/v1/runs/{run_id}/reopen` — formally reopen with a reason.
- `GET /api/v1/runs/{run_id}/audit-package` — deterministic audit export assembled from stored state.

`POST /api/v1/reconciliations/{reconciliation_id}/runs` accepts an optional header:

```http
Idempotency-Key: <client-generated-unique-key>
```

The key is scoped by tenant and endpoint. Repeating the same request returns the stored response instead of launching a duplicate run. Reusing a key for a different body is refused.

## Matches

- `GET /api/v1/matches/{match_id}` — group, members, reason codes, warnings, confidence, score, actors, and version.
- `POST /api/v1/matches/{match_id}/approve` — approve subject to permission, maker-checker, materiality, and version controls.
- `POST /api/v1/matches/{match_id}/reject` — reject with a reason.
- `POST /api/v1/matches/{match_id}/unmatch` — formally undo an active match with a reason.
- `POST /api/v1/matches/manual` — create a manual relationship with selected side A/B transaction IDs and a reason.

Approve, reject, and unmatch bodies include `expected_version` and `reason`. A stale version returns a concurrency conflict rather than overwriting newer review work.

## Exceptions

- `GET /api/v1/exceptions` — queue with run/status/category/severity/owner filters.
- `GET /api/v1/exceptions/aging` — aging buckets.
- `GET /api/v1/exceptions/escalations` — currently due escalation items.
- `GET /api/v1/exceptions/{exception_id}` — exception detail and version.
- `POST /api/v1/exceptions/{exception_id}/assign` — assign an owner.
- `POST /api/v1/exceptions/{exception_id}/transition` — apply a legal state-machine transition.
- `POST /api/v1/exceptions/{exception_id}/propose-resolution` — add controlled proposed resolution and code.
- `POST /api/v1/exceptions/{exception_id}/approve-resolution` — maker-checker approval.
- `POST /api/v1/exceptions/{exception_id}/close` — close an eligible resolved exception.
- `POST /api/v1/exceptions/{exception_id}/comment` — add a comment.
- `GET /api/v1/exceptions/{exception_id}/comments` — list comments.
- `POST /api/v1/exceptions/{exception_id}/evidence` — upload allowed supporting evidence.
- `GET /api/v1/exceptions/{exception_id}/evidence` — list evidence metadata.
- `POST /api/v1/exceptions/{exception_id}/ai-classification` — request advisory classification under tenant policy.
- `POST /api/v1/exceptions/{exception_id}/ai-decision` — record the human accept/reject decision on an AI suggestion; it does not approve the financial item.

Mutation bodies that expose `expected_version` use it for optimistic locking. Some exception schemas permit it to be omitted for compatibility, but clients should always send the version last read and reload after a conflict.

## Audit

- `GET /api/v1/audit/events` — tenant event stream.
- `GET /api/v1/audit/entity/{entity_type}/{entity_id}` — history for one entity.
- `GET /api/v1/audit/verify` — verify tenant audit sequence and hash chain.

## Health

- `GET /health` — liveness response.
- `GET /health/ready` — readiness including a database probe; degraded readiness should not receive production traffic.

The generated OpenAPI schema contains 45 paths and 48 method/path operations. The three paths that support both GET and POST each represent two operations.

## Errors

Errors use an RFC 7807-style problem document with application extensions:

```json
{
  "type": "about:blank",
  "title": "Forbidden",
  "status": 403,
  "detail": "The current actor lacks match:approve.",
  "code": "permission_denied",
  "instance": "/api/v1/matches/…/approve"
}
```

Validation errors use status 422 and add an `errors` array. Typical statuses are 401 for invalid authentication, 403 for tenant/user/permission refusal, 404 for a tenant-scoped resource that is not visible, 409 for control, state-transition, period-lock, idempotency, or concurrency conflicts, 413 for oversized uploads, 422 for invalid request data, and 500 when an accounting invariant fails. An invariant failure saves nothing.

## Optimistic-locking example

```http
POST /api/v1/matches/6b…/approve
Authorization: Bearer ey…
Content-Type: application/json

{
  "expected_version": 3,
  "reason": "Bank reference and amount verified against remittance."
}
```

If another reviewer changed version 3 first, the caller must fetch the resource again, review the change, and submit a decision against the new version. Clients must not automatically retry a stale write by merely incrementing the number.
