# HANDOVER — AI Reconciliation Platform

**To:** the next engineer or agent picking this up
**From:** the previous session
**Date of handover:** 2026-09-05
**Repository root:** `C:\Users\13072\Desktop\Finance\AI reconciliation Software`
**Source specification:** `C:\Users\13072\Downloads\AI_RECONCILIATION_PLATFORM_BUILD_SPEC.md` (4090 lines, 110 sections)

---

## 0. READ THIS FIRST — how to use this document

You have **zero implicit context**. Do not infer anything. Everything you need is stated
explicitly below, including exact file paths, exact commands, exact values, and exact
things you must NOT do.

**Before you write a single line of code:**

1. Run `cd "C:\Users\13072\Desktop\Finance\AI reconciliation Software"`
2. Run `python -m pytest tests -q --basetemp=./.pytest-tmp` — you MUST see `312 passed`.
   Then `rm -rf ./.pytest-tmp`.
   **Read the note on `--basetemp` below before running this any other way.**
3. Run `python scripts/verify_build_checklist.py` — you MUST see `26/26 checklist items pass`.
4. Run `git rev-list --count HEAD` — you MUST see `9`. Run `git branch --show-current` —
   you MUST see `main`.
5. Read `README.md` and then Section 3 of this document (Locked Decisions).

### The `--basetemp` flag is not optional in a sandbox

`tests/integration/test_end_to_end.py` is the **only** file that uses pytest's `tmp_path`
fixture, and it contains **exactly 8 tests**. If your environment denies write access to
the system temp directory, those 8 tests report as **errors**, and you will see:

```
304 passed, 8 errors
```

**That is an environment problem, not an application failure.** 304 + 8 = 312. Confirm it
is the temp directory and not something real by running the suite with a temp directory
inside the repository:

```bash
python -m pytest tests -q --basetemp=./.pytest-tmp
rm -rf ./.pytest-tmp
```

If that shows `312 passed`, the baseline is correct and you may proceed. If it shows any
failure, stop and report it — that would be a genuine regression.

### If the baseline still does not match

Report the discrepancy rather than working around it. Do not "fix" tests to make numbers
line up.

---

## 1. What this project is

A production-quality multi-tenant SaaS reconciliation platform that matches financial
records across bank, ledger and payment-processor data sources.

**The governing design principle, from the spec, which is non-negotiable:**

> Deterministic accounting logic first. AI assists ambiguity; it does not own financial truth.

**The optimisation order, from spec section 1.1, which is non-negotiable:**

1. Precision
2. Explainability
3. Auditability
4. Recall / automation rate

A false match is worse than an unmatched record. When in doubt, do not match.

---

## 2. Current state — what is DONE

All of the following is implemented, tested, and committed. **Do not rewrite any of it.**

### 2.1 Verified facts (measured, not claimed)

| Fact | Value | How it was verified |
| --- | --- | --- |
| Test suite | **312 passing, 0 failing** | `python -m pytest tests -q --basetemp=./.pytest-tmp` |
| Build checklist (spec §109) | **26 / 26 items pass** | `python scripts/verify_build_checklist.py` |
| API endpoints implemented | **45** | `app.openapi()["paths"]` |
| Database tables | **25** | `Base.metadata.tables` |
| Golden accounting fixtures | **15 scenarios × 5 files each** | `tests/reconciliation_fixtures/` |
| Permissions / roles | **37 permissions, 8 roles** | `packages/controls/permissions.py` |
| 100k-row benchmark | **124.3 s, 1,610 tx/s, 1.01 candidates/row** | `python scripts/benchmark_matching.py --sizes 100000` |
| End-to-end §102 demo | **passes over HTTP** | `tests/integration/test_end_to_end.py::TestGoldenDemo` |
| Run reproducibility | **verified against persisted data** | `python scripts/replay_reconciliation.py --all` |

### 2.2 Component inventory

| Path | Status | What it contains |
| --- | --- | --- |
| `packages/domain/` | DONE | `Money` (Decimal-only, rejects float), four distinct date concepts, all enums (exception taxonomy, roles, statuses), `CanonicalTransaction`, match/exception/reconciliation models |
| `packages/ingestion/` | DONE | CSV/XLSX/JSON parsers, column profiler, mapping suggester + applier with lineage, conservative normalisers, deterministic reference extraction, data-quality gate (PASS/WARN/FAIL) |
| `packages/matching/` | DONE | All 8 stages: eligibility, exact, rules, bounded grouping, candidate generation, scoring, ambiguity, decision policy. Plus bipartite assignment, fuzzy similarity, rule model, 2 shipped templates |
| `packages/exceptions/` | DONE | Deterministic classifier, 11-state workflow machine, aging, escalation |
| `packages/controls/` | DONE | RBAC (37 permissions), maker-checker, materiality, period lock, journal balance validation |
| `packages/audit/` | DONE | Hash-chained append-only events, lineage store, 9-file audit export package |
| `packages/ai/` | DONE | Provider abstraction, `DeterministicProvider` (no network), 8-step privacy guard, schema validation, entity resolution, investigation, document-understanding interface |
| `packages/connectors/` | PARTIAL | Contract DONE. Stripe DONE (injectable transport). File DONE. Webhook signatures DONE. **QuickBooks / Xero / bank_generic / SFTP are deliberately NOT implemented** — see §4.2 |
| `packages/observability/` | DONE | 19 metric definitions, correlation IDs + spans, 8 alert rules |
| `apps/api/` | DONE | FastAPI app, 45 endpoints, JWT auth, tenant-scoped repositories, 5 services, 25-table schema |
| `workers/` | DONE | Celery app + 10 tasks across 5 worker modules |
| `scripts/` | DONE | `seed_demo.py`, `benchmark_matching.py`, `verify_build_checklist.py`, `generate_fixture.py`, `replay_reconciliation.py` |
| `tests/` | DONE | 12 test files, 312 tests |
| `docs/runbooks/` | DONE | 4 runbooks |
| **`migrations/`** | **EMPTY — YOUR TASK 1** | |
| **`docs/*.md` (7 files)** | **MISSING — YOUR TASK 2** | |
| **`.github/workflows/`** | **MISSING — YOUR TASK 3** | |
| **`tests/security/`, `tests/performance/`** | **EMPTY — YOUR TASK 4** | |
| **`apps/web/`** | **EMPTY — YOUR TASK 5** | |
| **`infra/terraform/`, `infra/monitoring/`, `infra/kubernetes/`** | **EMPTY — YOUR TASK 6 (LOWEST PRIORITY)** | |

### 2.3 Git history (9 commits, branch `main`)

```
3ff6cdd Add HANDOVER.md for the next engineer
13bbb51 Observability, Celery workers, seed and replay scripts
4f4c32a Connectors, golden accounting cases, benchmark and the build checklist
ef121de API and services: the full upload-to-close workflow
b432f04 Persistence: schema, tenant-scoped repositories, audit sink, storage, crypto
75fc159 Fix reverse contention and rescale the evidence score
c7048ee Audit, controls, exception workflow and the AI layer
38038f2 Matching engine: staged pipeline with ambiguity-aware decision policy
af9daf2 Foundation: repo layout, domain model, money/date utilities, ingestion pipeline
```

---

## 3. LOCKED DECISIONS — do not re-open these

These were decided deliberately. **Do not change them. Do not "improve" them. Do not
refactor them.** If you believe one is wrong, write your reasoning in a comment in your
final report and leave the code alone.

| # | Locked decision | Why | Where |
| --- | --- | --- | --- |
| L1 | Build directly in the repo root, NOT in a `recon-platform/` subdirectory | The user pointed at this folder | repo root |
| L2 | Money is `Decimal` everywhere; `float` raises `TypeError` at the domain boundary AND at the database boundary | Spec §83, §109 | `packages/domain/money/money.py`, `apps/api/app/infrastructure/types.py` |
| L3 | On SQLite, money is stored as a 25-char zero-padded offset-scaled integer STRING | SQLite `NUMERIC` is a float64 and silently rounds past ~15 digits. The encoding is order-preserving so `ORDER BY` still works | `apps/api/app/infrastructure/types.py`, `_SQLITE_OFFSET` |
| L4 | Python import root is `packages.*`, `apps.*`, `workers.*` from the repo root | Configured in `pyproject.toml` `[tool.pytest.ini_options] pythonpath = ["."]` | `pyproject.toml` |
| L5 | Tests run on in-memory SQLite; production targets PostgreSQL | Lets the whole API be tested without a server | `tests/conftest.py` |
| L6 | Authentication is BOUGHT, not built. No user store, no passwords | Spec §4. JWT verified against a JWKS issuer; `AUTH_DEV_SECRET` is a dev-only HS256 signer that `Settings` REFUSES to accept when `ENVIRONMENT=production` | `apps/api/app/infrastructure/auth.py`, `apps/api/app/config.py` |
| L7 | Default AI provider is `DeterministicProvider` — rule-based, no network, no API key | Makes every AI code path testable with `AI_ENABLED=false` | `packages/ai/provider.py` |
| L8 | A non-human actor (AI, SYSTEM, CONNECTOR, MATCH_ENGINE) has `FORBIDDEN_FOR_NON_HUMAN` (13 permissions) subtracted unconditionally | Spec §34: "AI cannot approve anything". Enforced structurally, not by convention | `packages/controls/permissions.py` |
| L9 | Rules are versioned DATA (`MatchingRule` objects), not hardcoded branches | Spec §14 | `packages/matching/rules.py`, `packages/matching/templates.py` |
| L10 | A run reads its transactions from its FROZEN `RunSnapshot`, never from live tables | Spec §36. Makes replay meaningful | `apps/api/app/services/reconciliation.py`, `rerun()` |
| L11 | Money crosses the HTTP wire as a decimal STRING, never a JSON number | A JSON number becomes a float in most clients | `apps/api/app/api/schemas.py`, `MoneyMixin` |
| L12 | Rule conditions may use field `"date"` (= transaction, else posting, else value date) OR a specific date field name (strict) | A ledger export often carries only a posting date; a rule demanding `transaction_date` from both sides would never fire | `packages/matching/rules.py`, `_FIELD_ACCESSORS` |
| L13 | Unimplemented connectors RAISE `NotImplementedError`; they never return empty results | An empty result looks like a healthy sync of a quiet account (spec §89) | `workers/connector_sync_worker/tasks.py`, `_run_sync` |
| L14 | Each rule stage runs TWO passes: collect proposals, then reject counterparty records claimed by more than one anchor | Prevents auto-matching one leg of a duplicate payment and makes the result order-independent | `packages/matching/exact.py`, `_run_rule_stage` |
| L15 | The evidence score is `earned / available` where a signal enters the denominator only if BOTH records carry that field, plus a `no_identifier_penalty` of 45.0 when neither carries a strong identifier | Normalising by the sum of all weights crushed every score to ~0.28 and made the whole suggestion stage dead code | `packages/matching/scoring.py`, `_score_with_features` |
| L16 | "Unexplained difference" at close = raw difference MINUS the signed contribution of RESOLVED/CLOSED exceptions | A resolved exception IS an explanation; otherwise the close gate is unreachable | `apps/api/app/services/close.py`, `_difference` |

---

## 4. Known limitations — state these, do not silently fix

### 4.1 Performance
- 100k rows/side takes **124 seconds** and peaks at **1.9 GB** of memory, single-threaded.
- This is acceptable for a batch worker job and satisfies spec §99 criterion 1 ("import at least 100k transactions reliably").
- The memory figure is high because the engine holds all transactions as Pydantic models in memory. If you need 1M rows, that is a **chunked/streaming redesign**, not a micro-optimisation. Do not attempt it as part of the tasks below.

### 4.2 Connectors deliberately not implemented
`packages/connectors/quickbooks/`, `xero/`, `bank_generic/`, `sftp/` contain ONLY a
docstring and `IMPLEMENTED = False`. This is intentional (spec §77 puts them in Phase 2).
**Do not stub them to return empty data.** See L13.

### 4.3 Calibration
`packages/matching/ambiguity.py::confidence_from_score` is an explicitly uncalibrated
placeholder (documented as such in its docstring). Spec §26 says do not build custom ML
before labelled data exists. **Do not build an ML model.**

### 4.4 `false_match_rate` is always `None`
This is deliberate (spec §48). It reads as "not yet measured" rather than implying a proven
zero. It becomes a real number only when human review feedback is collected over time.

---

## 5. YOUR TASKS — in priority order

Do them **in this order**. Each task has: exact files to create, exact content
requirements, and an exact verification command that must pass before you move on.

---

### TASK 1 — Alembic migrations (HIGHEST PRIORITY)

**Why first:** `make migrate` is in the Makefile and currently fails. `docker-compose.yml`
starts PostgreSQL but nothing creates the schema there. Without this the platform cannot
run on its real production database.

**Files to create:**

1. `alembic.ini` (repo root)
2. `migrations/env.py`
3. `migrations/script.py.mako`
4. `migrations/versions/0001_initial_schema.py`

**Exact requirements:**

- `migrations/env.py` MUST import `from apps.api.app.infrastructure.models import Base` and
  set `target_metadata = Base.metadata`.
- `migrations/env.py` MUST read the database URL from `apps.api.app.config.get_settings().database_url`,
  NOT from a hardcoded string in `alembic.ini`.
- The initial revision MUST create **all 25 tables**. Get the exact list by running:
  ```
  python -c "from apps.api.app.infrastructure.models import Base; print('\n'.join(sorted(Base.metadata.tables)))"
  ```
- The revision MUST include these named constraints exactly (the build checklist asserts
  their names, and `scripts/verify_build_checklist.py` items 3 and 4 will fail without them):
  - `uq_source_records_identity` on `source_records (tenant_id, source_system, source_record_id, checksum)`
  - `uq_transactions_identity` on `canonical_transactions (tenant_id, source_connection_id, source_record_id, source_checksum)`
  - `uq_audit_tenant_sequence` on `audit_events (tenant_id, sequence)`
  - `uq_idempotency_identity` on `idempotency_keys (tenant_id, endpoint, key)`
- Money columns MUST be `NUMERIC(24, 8)` in PostgreSQL. Import the type via
  `from apps.api.app.infrastructure.types import Money, GUID` and use those, so the
  migration and the models cannot drift.
- JSON columns MUST be `JSONB` on PostgreSQL.
- Add a `downgrade()` that drops the tables in reverse dependency order.

**Easiest correct approach:** use Alembic autogenerate against a scratch database, then
hand-review the output. Do NOT ship an unreviewed autogenerated file — autogenerate misses
`server_default` and gets index names wrong.

```bash
cd "C:\Users\13072\Desktop\Finance\AI reconciliation Software"
python -m pip install alembic
alembic init -t generic migrations   # then edit env.py per the requirements above
alembic revision --autogenerate -m "initial schema"
```

**VERIFICATION (all four must pass):**

```bash
# 1. SQLite round trip
rm -f ./migration-test.sqlite3
DATABASE_URL="sqlite+pysqlite:///./migration-test.sqlite3" alembic upgrade head
DATABASE_URL="sqlite+pysqlite:///./migration-test.sqlite3" alembic downgrade base
DATABASE_URL="sqlite+pysqlite:///./migration-test.sqlite3" alembic upgrade head

# 2. The migrated schema matches the models exactly — MUST print no differences
python -c "
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine
from apps.api.app.infrastructure.models import Base
e = create_engine('sqlite+pysqlite:///./migration-test.sqlite3')
with e.connect() as c:
    diff = compare_metadata(MigrationContext.configure(c), Base.metadata)
print('DIFFERENCES:', diff)
assert not diff, diff
print('schema matches models')
"

# 3. Table count
python -c "
from sqlalchemy import create_engine, inspect
i = inspect(create_engine('sqlite+pysqlite:///./migration-test.sqlite3'))
n = [t for t in i.get_table_names() if t != 'alembic_version']
assert len(n) == 25, f'expected 25 tables, got {len(n)}: {sorted(n)}'
print('25 tables created')
"

# 4. Nothing else broke
python -m pytest tests -q --basetemp=./.pytest-tmp   # MUST still be 312 passed
python scripts/verify_build_checklist.py   # MUST still be 26/26
rm -f ./migration-test.sqlite3
```

**DO NOT:** change any file under `apps/api/app/infrastructure/models.py` to make the
migration easier. The models are correct; the migration must match them.

---

### TASK 2 — The seven missing docs

**Why:** spec §5 names them explicitly. `docs/runbooks/` (4 files) is already DONE.

**Files to create, all under `docs/`:**

| File | Must contain (minimum) |
| --- | --- |
| `architecture.md` | The layer diagram from spec §107; the dependency rule (`packages` never imports from `apps` or `workers`); why the engine is a pure function; the request→service→repository→database path |
| `accounting-model.md` | The 11 concepts from spec §6 (source record, canonical transaction, reconciliation, run, candidate, match group, exception, decision, evidence, accounting action, audit event) and how each maps to a table; the four date concepts from §84 and when each is used; the `Money` contract |
| `matching-engine.md` | All 8 stages; the 96-vs-95 example from §18 with the actual code path; the rule YAML format (copy the real format from `tests/reconciliation_fixtures/exact/rule_config.yaml`); the difference between evidence score, confidence, ambiguity and risk; the bounded subset-sum bounds and what happens when a budget is exhausted |
| `security.md` | The §53 minimum posture list mapped to where each is implemented; tenant isolation (§54) and the repository pattern; the PII classification from §55; what is never logged; the AI privacy guard's 8 steps (§56) |
| `controls.md` | The 8 roles and their permissions (generate the table from `ROLE_PERMISSIONS`); maker-checker; materiality; period lock; the close gate's 5 conditions from §58; evidence retention |
| `connector-sdk.md` | The `Connector` ABC contract; the 12 connector requirements from §9; a worked example using `StripeConnector`; how to add a new connector; why unimplemented connectors raise (L13) |
| `api.md` | All 45 endpoints grouped by resource; auth header format; the `Idempotency-Key` header on `POST /reconciliations/{id}/runs`; the `expected_version` optimistic-locking field; the RFC-7807 error shape; **the rule that money is a string** |

**Generate the endpoint list for `api.md` with:**
```bash
python -c "
from apps.api.app.main import app
p = app.openapi()['paths']
for path in sorted(p):
    print(','.join(sorted(m.upper() for m in p[path])), path)
"
```

**Generate the permission table for `controls.md` with:**
```bash
python -c "
from packages.controls.permissions import ROLE_PERMISSIONS
for role, perms in sorted(ROLE_PERMISSIONS.items()):
    print(f'## {role.value}')
    for p in sorted(perms): print(' -', p.value)
"
```

**VERIFICATION:**
```bash
python -c "
from pathlib import Path
required = ['architecture','accounting-model','matching-engine','security','controls','connector-sdk','api']
for name in required:
    f = Path(f'docs/{name}.md')
    assert f.exists(), f'{f} missing'
    assert len(f.read_text(encoding='utf-8')) > 2000, f'{f} is too thin to be useful'
print('all 7 docs present and substantial')
"
```

**DO NOT:** write documentation that contradicts the code. Every claim you make must be
checkable against a file. If you are unsure, read the file.

---

### TASK 3 — CI pipeline

**Why:** spec §81 specifies it exactly.

**File to create:** `.github/workflows/ci.yml`

**Exact requirements — the PR job MUST run these steps in this order (spec §81):**

1. format check — `ruff format --check .`
2. lint — `ruff check .`
3. type check — `mypy apps packages`
4. unit tests — `pytest tests/unit -q`
5. integration tests — `pytest tests/integration -q`
6. golden accounting cases — `pytest tests/accounting_golden_cases -q`
7. migration validation — upgrade then downgrade then upgrade (see Task 1 verification)
8. dependency scan — `pip-audit`
9. secret scan — `gitleaks` or `trufflehog`
10. container scan — `trivy image`
11. frontend build — `cd apps/web && npm ci && npm run build` (guard with `if: hashFiles('apps/web/package.json') != ''` until Task 5 is done)
12. backend build — `docker build -f infra/docker/api.Dockerfile .`

**Also add, as a separate job:** `python scripts/verify_build_checklist.py`. This is the
single highest-value CI step in the repository — it executes all 26 items of spec §109.

**Main-branch job (spec §81):** all PR checks, build an immutable image tagged with the
commit SHA, deploy to staging, run smoke tests, then a **manual approval gate**
(`environment: production`), then deploy production.

**Runner:** `ubuntu-latest`. **Python:** `3.12`. **Services:** postgres:16 and redis:7 for
the integration job.

**IMPORTANT — steps 1-3 will currently FAIL.** Run them locally first and fix what they
report. Expect `ruff format --check` to want reformatting and `mypy` to report missing
annotations. Fix the code, not the CI config. If a mypy error is a genuine false positive,
add a narrowly-scoped `# type: ignore[specific-code]` with a comment explaining why —
never a blanket ignore or a config-level exclusion.

**VERIFICATION:**
```bash
python -m pip install "ruff>=0.4" "mypy>=1.9" types-PyYAML types-python-dateutil
ruff format --check .    # must exit 0
ruff check .             # must exit 0
mypy apps packages       # must exit 0
python -m pytest tests -q --basetemp=./.pytest-tmp  # must still be 312 passed
python -c "
import yaml, pathlib
w = yaml.safe_load(pathlib.Path('.github/workflows/ci.yml').read_text(encoding='utf-8'))
print('jobs:', list(w['jobs']))
assert len(w['jobs']) >= 2
print('workflow parses')
"
```

---

### TASK 4 — Fill `tests/security/` and `tests/performance/`

**Why:** spec §49 names both directories. Both currently contain only `__init__.py`.

**File to create:** `tests/security/test_security.py`

Must contain tests for, at minimum:

- An expired JWT is rejected with 401. (Build one with `issue_dev_token(..., ttl_seconds=-10)`.)
- A JWT signed with the wrong secret is rejected with 401.
- A JWT with no tenant claim is rejected.
- A JWT whose tenant does not exist in the database is rejected with 403.
- A deactivated user (`UserRow.is_active = False`) is rejected with 403.
- Tenant A's token cannot read, approve, or close ANY of tenant B's resources. Test each
  of: `GET /transactions/{id}`, `GET /runs/{id}`, `POST /matches/{id}/approve`, `GET /exceptions/{id}`.
- A path-traversal filename (`../../etc/passwd`) does not escape the storage root.
  (`packages/audit/evidence.py` and `apps/api/app/infrastructure/storage.py::storage_key`
  already defend this — assert it.)
- An executable upload (bytes starting `MZ` or `\x7fELF`) is refused.
- An oversized upload is refused with 413.
- A disallowed evidence MIME type is refused.
- Every security header is present on a response: `X-Content-Type-Options`,
  `X-Frame-Options`, `Referrer-Policy`, `Content-Security-Policy`,
  `Strict-Transport-Security`, `Cache-Control: no-store`.
- Secrets never appear in audit metadata (already covered in `tests/unit/test_exception_workflow.py::TestAuditChain::test_secrets_never_reach_the_audit_log` — add the API-level equivalent).
- `Settings(ENVIRONMENT="production", AUTH_DEV_SECRET="x")` raises `ValueError`.

Reuse the fixtures in `tests/integration/test_end_to_end.py` (`client`, `settings`,
`tenant`, `auth`) — copy the pattern, do not import across test modules.

**File to create:** `tests/performance/test_performance.py`

Must contain, marked so they can be deselected in a fast CI run
(`@pytest.mark.performance`, and register the marker in `pyproject.toml`):

- Candidate generation stays flat: run `scripts.benchmark_matching.run_size` at 500 and
  2000 rows and assert `candidates_per_row` does not grow by more than 50%. **This is the
  test that catches a regression back to O(n·m).**
- The bounded subset search always terminates within its time budget: assert
  `find_subsets(..., time_budget_ms=50).elapsed_ms < 2000` on an adversarial 80-item pool.
- Ingestion of 5,000 CSV rows completes in under 30 seconds.
- `assert_invariants` over 5,000 match groups completes in under 5 seconds.

**VERIFICATION:**
```bash
python -m pytest tests/security -q          # all must pass
python -m pytest tests/performance -q       # all must pass
python -m pytest tests -q --basetemp=./.pytest-tmp   # total > 312, 0 failures
```

**DO NOT:** weaken a security control to make a test pass. If a test fails, you have found
a real vulnerability — fix the code and say so explicitly in your report.

---

### TASK 5 — The frontend (`apps/web/`)

**Why:** spec §4 and §5 specify Next.js + TypeScript + React. Currently completely empty.

**This is the largest remaining task. Budget accordingly.**

**Stack (LOCKED by spec §4 — do not substitute):**
- Next.js (App Router) + TypeScript in **strict** mode
- TanStack Table for grids
- React Hook Form + Zod for forms and validation
- shadcn/ui or another accessible component system
- **No `any` in financial domain code** (spec §82)

**Files to create (minimum):**

```
apps/web/package.json
apps/web/tsconfig.json          # "strict": true
apps/web/next.config.js
apps/web/.env.local.example     # NEXT_PUBLIC_API_BASE_URL, NEXT_PUBLIC_DEV_TOKEN
apps/web/lib/api-client.ts      # typed fetch wrapper
apps/web/lib/schemas.ts         # Zod schemas mirroring apps/api/app/api/schemas.py
apps/web/lib/money.ts           # money is a STRING — see below
apps/web/app/layout.tsx
apps/web/app/page.tsx
apps/web/features/dashboard/
apps/web/features/sources/
apps/web/features/mappings/
apps/web/features/reconciliations/
apps/web/features/matches/
apps/web/features/exceptions/
apps/web/features/approvals/
apps/web/features/settings/
apps/web/tests/
```

**CRITICAL RULE — money handling in TypeScript:**
The API returns money as a **string** (`"982.45"`). You MUST NOT do
`parseFloat(amount)` or `Number(amount)` anywhere. Use a decimal library
(`decimal.js` or `dinero.js`) or keep values as strings and format them for display only.
Putting a monetary value through a JavaScript `number` reintroduces exactly the floating-
point error the entire backend was built to avoid. Add an ESLint rule banning
`parseFloat`/`Number` on any variable named `*amount*` or `*balance*` if you can.

**Screen requirements:**

1. **Dashboard** (spec §41). The first screen must answer: *"Are my accounts reconciled,
   and if not, why not?"* Show: reconciliation, period, status, side A balance, side B
   balance, difference, matched amount and count, auto-matched, human-approved, suggested,
   exceptions, high-risk exceptions, oldest exception, completion %.
   **Spec §41 explicitly says: do NOT make "AI insights" the first thing users see.**
   **Spec §48: never show the automation rate without the false-match rate beside it.**
   Data source: `GET /api/v1/runs/{run_id}/summary`.

2. **Transaction review — three-pane layout** (spec §42). This is the core screen.
   - LEFT: unmatched / suggested transactions
   - CENTER: candidate matches + confidence + **the reason codes** (`reasons[]` from the
     match response — render `code` and `description` for each)
   - RIGHT: source details, evidence, comments, history
   - Actions: Approve, Reject, Create manual match, Split/group, Assign exception, Add
     note, Request evidence, Propose adjustment, Escalate
   - **Keyboard-first** (spec §42: "Keyboard-first workflows are valuable for finance
     teams"). At minimum: `j`/`k` to move, `a` approve, `r` reject, `?` help.

3. **Sources** — file upload, list, status.
4. **Mappings** — show the profile from `GET /files/{id}/profile`, let the user confirm or
   change each column mapping, then `POST /files/{id}/mapping`. **The user must confirm
   before ingest** — never auto-ingest.
5. **Reconciliations** — list, create from a template, start a run.
6. **Exceptions** — queue with filters, the state machine transitions, comments, evidence upload.
7. **Approvals** — pending approvals for the current user.
8. **Settings** — thresholds, rules, AI policy per tenant.

**Permission-driven UI:** call `GET /api/v1/me` and use the returned `permissions[]` array
to decide which actions to render. Rendering an action the user cannot perform and then
failing at the API is a poor experience.

**Getting a working token to develop against:**
```bash
cd "C:\Users\13072\Desktop\Finance\AI reconciliation Software"
python scripts/seed_demo.py
# copy the "bob" (CONTROLLER) token into apps/web/.env.local as NEXT_PUBLIC_DEV_TOKEN
python -m uvicorn apps.api.app.main:app --reload   # API on http://localhost:8000
cd apps/web && npm run dev                         # UI on http://localhost:3000
```
CORS for `http://localhost:3000` is already configured in `apps/api/app/main.py`.

**VERIFICATION:**
```bash
cd apps/web
npm ci
npx tsc --noEmit          # must exit 0 with strict mode
npm run lint              # must exit 0
npm run build             # must exit 0
npm test                  # must pass
# Then start both servers and confirm by hand:
#   - the dashboard shows the seeded run's real numbers
#   - the three-pane review screen shows reason codes for a real match
#   - approving a match as "bob" succeeds; as "alice" (PREPARER) it is refused with 403
```

**DO NOT:** mock the API. Develop against the real running backend using a seeded token.
A frontend built against mocks will not match the real response shapes.

---

### TASK 6 — Infrastructure (LOWEST PRIORITY — only if time remains)

**Files:**
- `infra/terraform/` — VPC, RDS PostgreSQL (encrypted, automated backups), ElastiCache
  Redis, S3 bucket (versioning + object lock + SSE), KMS key, ECS/Fargate services,
  secrets in AWS Secrets Manager.
- `infra/monitoring/` — Prometheus scrape config and alert rules generated from
  `packages/observability/alerts.py` (the 8 rules there are the source of truth — do not
  duplicate the thresholds by hand), plus a Grafana dashboard JSON.
- `infra/kubernetes/` — spec §5 marks this "later". **Skip it** unless explicitly asked.

**VERIFICATION:** `terraform validate` and `terraform fmt -check` exit 0. Do not run
`terraform apply`.

---

## 6. Rules you must follow while working

These come from `C:\Users\13072\.claude\CLAUDE.md` (the user's global instructions) and
from the spec. They are not optional.

1. **Work on the `main` branch.** Do NOT create a worktree, a sandbox branch, or an
   isolated copy unless the user explicitly says "create a worktree".
2. **Read files before changing them.**
3. **Do not create new files unless necessary** — prefer editing existing ones. (The tasks
   above all legitimately require new files.)
4. **Do not refactor, clean up, or improve code that is not part of your task.** The 312
   passing tests are the contract.
5. **Do not push to GitHub** unless explicitly asked.
6. **Commit from `main`. Never force push. Never use `--no-verify`.**
7. **End every commit message with:**
   ```
   Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
   ```
8. **An unexpected result is not automatically a bug.** Investigate neutrally before
   concluding something is broken.
9. **Never weaken a control to make a test pass.**
10. **Never let a `float` touch money.**

---

## 7. Proof-of-execution requirements

A completion summary is a **claim**, not evidence. Before you report any task as done, you
must paste the **actual terminal output** of its verification commands into your report.

**Your final report MUST include, verbatim:**

```bash
python -m pytest tests -q --basetemp=./.pytest-tmp | tail -3
python scripts/verify_build_checklist.py | tail -3
git branch --show-current
git log --oneline | head -15
git status --short
```

**A task counts as done only if:**
- Its verification commands exit 0 and you have pasted the output.
- `python -m pytest tests -q --basetemp=./.pytest-tmp` still shows **0 failures** (the count will rise above 312 once you add Task 4's tests — that is expected and correct).
- `python scripts/verify_build_checklist.py` still shows **26/26**.
- The work is committed on `main`.

**If a task is blocked or you could not finish it, say so explicitly.** State which task,
what blocked it, and what you tried. Do not report partial work as complete. Do not
silently narrow the scope.

---

## 8. Quick reference — commands

```bash
cd "C:\Users\13072\Desktop\Finance\AI reconciliation Software"

# Verify the world is sane
python -m pytest tests -q --basetemp=./.pytest-tmp && rm -rf ./.pytest-tmp
python scripts/verify_build_checklist.py

# Run one test file
python -m pytest tests/unit/test_decision_policy.py -q

# The end-to-end demo (spec §102)
python -m pytest tests/integration/test_end_to_end.py -q

# Seed a demo tenant and print dev tokens
python scripts/seed_demo.py

# Benchmark (default 10k and 100k; 100k takes ~2 minutes)
python scripts/benchmark_matching.py --sizes 1000 10000

# Prove stored runs still reproduce
python scripts/replay_reconciliation.py --all

# Regenerate the 15 golden fixtures after editing scripts/generate_fixture.py
python scripts/generate_fixture.py

# Run the API locally
python -m uvicorn apps.api.app.main:app --reload
# then open http://localhost:8000/docs
```

---

## 9. Where to look when you are confused

| Question | File |
| --- | --- |
| How does a match get decided? | `packages/matching/ambiguity.py::decide` |
| Why did this pair not match? | `packages/matching/explanations.py::build_warnings` |
| What are all the stages? | `packages/matching/engine.py::MatchingEngine.run` |
| What can this role do? | `packages/controls/permissions.py::ROLE_PERMISSIONS` |
| Why was this refused? | `packages/controls/segregation_of_duties.py` |
| What does the API return? | `apps/api/app/api/schemas.py` |
| What is stored where? | `apps/api/app/infrastructure/models.py` |
| Can AI do X? | `packages/controls/permissions.py::FORBIDDEN_FOR_NON_HUMAN` (answer: if it is in that set, no) |
| What does "done" mean? | `scripts/verify_build_checklist.py` |
| What must never regress? | `tests/accounting_golden_cases/test_golden_cases.py` |

---

## 10. The one thing to remember

If you must choose between automating more and being certain, **choose certainty**. The
product promise is not "AI reconciles everything". It is:

> The platform automatically reconciles what can be proven, intelligently suggests what is
> likely, and gives finance teams a controlled workflow for everything else.
