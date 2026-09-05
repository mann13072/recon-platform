# Security

Security is layered across external identity, request middleware, permission checks, tenant-scoped persistence, encrypted credentials, controlled uploads, object storage, audit logging, and deployment infrastructure. Controls that depend on the production platform are identified as deployment obligations rather than presented as application features.

## Minimum production posture

| Requirement | Implementation or deployment owner |
| --- | --- |
| TLS 1.2+ in transit | Terminate and enforce at the production load balancer/API gateway; not provided by the development Uvicorn process |
| AES-256 or cloud-equivalent at rest | `S3ObjectStorage.put` requests AES-256 server-side encryption; database and volume encryption are infrastructure obligations |
| Encrypted backups | Production database/object-store backup configuration and restore drills; see `docs/runbooks/backup-restore.md` |
| KMS-managed secrets | Connector credential encryption in `apps/api/app/infrastructure/crypto.py`; production key IDs come from settings |
| Encrypted OAuth tokens | Only ciphertext is stored in `connector_credentials`; no plaintext token column exists |
| Tenant scoping on every query | `TenantScopedRepository` requires `tenant_id` and starts queries through `_scoped` |
| MFA | Required at the purchased identity provider; authentication is not built locally |
| RBAC | `packages/controls/permissions.py`, enforced by route/service permission checks |
| Audit logs | Hash-chained, append-only events in `packages/audit` and `audit_events` |
| Rate limiting | Configuration exists as `RATE_LIMIT_PER_MINUTE`; enforcement belongs at the gateway until an application limiter is added |
| CSRF/XSS protection | Bearer-token API, restrictive CORS, CSP, `nosniff`, frame denial, no-store responses; frontend must escape output and avoid unsafe HTML |
| Dependency, container, and secret scanning | CI pipeline responsibility; these scans are not runtime application controls |
| Secure file upload | Size, type, executable-signature, filename, content hash, and path controls in file/evidence services and storage helpers |
| Malware scanning | `malware_scan` state and ingestion gate; production must connect a scanner before treating uploads as clean |
| Signed object-storage URLs | `ObjectStorage.presigned_url`; S3 links default to short-lived access |
| Short-lived service credentials | Configure through the identity/cloud platform; do not place static credentials in source or `.env` committed files |
| Structured incident response | `docs/runbooks/incident-response.md`, correlation IDs, audit events, and security-denial logging |

Every HTTP response receives `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, a restrictive `Content-Security-Policy`, HSTS, and `Cache-Control: no-store`. The middleware also returns an `X-Correlation-ID` used to trace user actions through audit and worker activity.

## Authentication and authorization

Clients send `Authorization: Bearer <JWT>`. Production tokens are verified against the configured issuer, audience, and JWKS. `AUTH_DEV_SECRET` enables local HS256 tokens only in development; `Settings` refuses to start in production if that secret is set or if production issuer settings are missing. Expired, forged, incomplete, unknown-tenant, and inactive-user identities are rejected.

The database contains a projection of external users for stable attribution, not a password store. Permissions come from the user's roles. Non-human actors (`AI`, `SYSTEM`, `CONNECTOR`, and `MATCH_ENGINE`) have the 13 privileged permissions in `FORBIDDEN_FOR_NON_HUMAN` removed structurally, so AI cannot approve financial actions even if roles are misassigned.

## Tenant isolation

Financial tables carry `tenant_id`. A `RequestContext` obtains that tenant from the verified principal. Repositories cannot be constructed without it, and `_scoped(model)` applies `model.tenant_id == self.tenant_id` before resource IDs or filters. Writes also replace or validate tenant IDs. A request with Tenant A's token therefore cannot express a repository read of Tenant B's transaction, run, match, or exception by guessing its UUID.

Application scoping is the current enforcement boundary and is covered by cross-tenant tests. PostgreSQL row-level security, separate databases for the highest-security tenants, and region-specific data planes are future defense-in-depth options, not current claims.

## Data classification and logging

Use five classes: **Public**, **Internal**, **Confidential financial**, **PII**, and **Credentials/secrets**. Transaction amounts and reconciliation results are confidential financial data. Names, e-mail addresses, and account identifiers are PII. OAuth refresh tokens, API keys, database passwords, signing secrets, and encryption keys are credentials/secrets.

Never log bank credentials, OAuth refresh tokens, full account or card numbers, API secrets, encryption keys, unredacted sensitive documents, or raw authorization headers. Account numbers should be masked in the UI. Audit metadata passes through secret redaction; it records hashes, identifiers, decision facts, and safe summaries rather than credential values. Free-text AI inputs are separately redacted for IBANs, card-like numbers, long account numbers, and e-mail addresses.

## AI privacy guard

The eight-step contract is implemented across `packages/ai/privacy.py`, the provider service, schema validation, and AI-call audit records:

1. **Classify the requested task.** Unknown tasks are rejected against `TASK_FIELD_ALLOWLIST`.
2. **Minimize fields.** Only the allowlist for that task survives; dropped fields are recorded.
3. **Redact unnecessary PII.** Sensitive patterns are masked; metadata-only policy replaces strings with length descriptors.
4. **Check tenant AI policy.** `AI_DISABLED` refuses before a provider call; task-level permission is also checked.
5. **Check data-region policy.** Provider region must be in the tenant's allowed region set.
6. **Send the request.** The provider receives only the minimized payload; the default deterministic provider makes no network call.
7. **Validate structured output.** Invalid schema produces no suggestion and no state change.
8. **Store audit metadata.** `ai_calls` records task/provider/model/prompt versions, input hash, policy, redaction count, latency, cost, validation result, and user decision—not hidden reasoning or secrets.

Supported policies are `AI_DISABLED`, `AI_METADATA_ONLY`, `AI_REDACTED_DATA`, and `AI_PRIVATE_PROVIDER`. AI output is advisory under every policy.
