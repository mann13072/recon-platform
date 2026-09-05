# DASHBOARD REDESIGN — implementation instructions

**Status:** NOT STARTED. Nothing in this document has been implemented.
**Written:** 2026-09-05. **Branch:** `main`. **Repo root:** `C:\Users\13072\Desktop\Finance\AI reconciliation Software`

---

## 0. READ THIS FIRST — how to use this document

You are implementing a redesign of the Overview screen, plus five bug fixes that must land first.

This document assumes you have **zero prior context**. Everything you need is stated here. Do not
infer, do not guess, do not re-derive a decision already made. Every file path is exact. Every
number quoted was **measured**, not estimated — the measurement command is given so you can
reproduce it.

**Work in this order. Do not skip ahead to the visual work.** Section 6 is bug fixes and comes
first because the user chose "Fix bugs first, then redesign". Section 7 is the redesign phases.

### Establish the baseline before you touch anything

Run these four commands. If they do not produce these results, STOP and find out why before editing
anything — you cannot attribute a regression without a known-good starting point.

```bash
cd "C:\Users\13072\Desktop\Finance\AI reconciliation Software"
python -m pytest tests -q --basetemp=./.pytest-tmp   # expect: 332 passed
python scripts/verify_build_checklist.py             # expect: 26/26 checklist items pass
git branch --show-current                            # expect: main
git status --short                                   # expect: no output
```

`--basetemp=./.pytest-tmp` is **not optional**. Without it pytest writes to a system temp path that
fails in this environment.

### Running the app locally

Two servers. Both are needed for any frontend work.

```bash
# Terminal 1 — seed the demo data (creates ./recon-demo.sqlite3, which is gitignored)
python scripts/seed_demo.py

# Terminal 2 — the API. The env block is MANDATORY.
# Without AUTH_DEV_SECRET every call returns:
#   {"detail":"No authentication method is configured. Set AUTH_JWKS_URL..."}
ENVIRONMENT=development \
DATABASE_URL="sqlite+pysqlite:///./recon-demo.sqlite3" \
AUTH_DEV_SECRET="local-development-secret-at-least-32-characters" \
AUTH_AUDIENCE="" \
AI_ENABLED=false \
LOCAL_STORAGE_PATH="./var/storage" \
OBJECT_STORAGE_ENDPOINT="" \
CREDENTIAL_ENCRYPTION_KEY="local-development-credential-key-32-chars" \
python -m uvicorn apps.api.app.main:app --host 127.0.0.1 --port 8000

# Terminal 3 — the frontend
cd apps/web && npm run dev     # http://localhost:3000
```

`apps/web/.env.local` is gitignored and will not exist on a fresh clone. Create it with the
**bob (CONTROLLER)** token printed by `seed_demo.py`:

```
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000/api/v1
NEXT_PUBLIC_DEV_TOKEN=<the bob token from seed_demo.py output>
```

The API command above runs **without `--reload`**. **Restart it after every backend edit** or you
will be testing stale code and drawing wrong conclusions.

The UI is keyboard-first: typing while no field is focused triggers navigation shortcuts. Click into
an input before typing, or you will change tabs by accident.

### Seeded reference values

Every example in this document uses the seeded demo run. Your run ID will differ — `seed_demo.py`
prints it. The run used while writing this document was `85bd6001-5e79-4e8c-8cb1-a964e3b1b5cb`.

| Figure | Value |
| --- | --- |
| Side A (bank) balance | `EUR 9012.00` |
| Side B (ledger) balance | `EUR 9248.70` |
| Difference | `EUR -236.70` |
| Transactions | 18 |
| Auto-matched | 5 |
| Suggested | 1 |
| Exceptions | 5 |
| Automation rate | 83.3% |
| Completion | 70.6% |
| False-match rate | `Not yet measured` |
| Audit chain | verified, 36 events |

---

## 1. What this product is

A **financial reconciliation platform**. Accountants and controllers use it to answer one question:
*"are the accounts reconciled, and can I prove it to an auditor?"*

It ingests two sides of transaction data (e.g. a bank export and a general ledger export), matches
transactions with a staged deterministic engine, raises exceptions for what does not match, routes
them through a maker-checker approval workflow, and produces an auditable close backed by a
hash-chained event log.

Four domain constraints that make this unlike a normal dashboard app:

1. **Money is `Decimal`, never float.** Amounts cross the API as exact decimal **strings** and are
   rendered as exact decimal strings. Rounding for display is a correctness bug.
2. **Every number shown must be traceable to a source record.** Decorative or approximated figures
   are unacceptable.
3. **Users are auditors, preparers and controllers**, working keyboard-first on desktop for hours.
4. **A metric that is not measured must render as "Not yet measured"** — never `0`, never invented.

---

## 2. The current frontend — exact inventory

Location: `apps/web/`

**Stack, already installed, these exact versions:**

- Next.js `16.3.4` App Router, React `19.2.8`, TypeScript `5.9.3` strict
- `@radix-ui/react-tabs` ^1.1.13 — the whole app shell is ONE page using Radix tabs
- `@tanstack/react-table` ^8.21.3, `react-hook-form` ^7.87.0, `@hookform/resolvers` ^5.2.2
- `zod` ^4.5.4 — every API response is parsed through a zod schema
- `decimal.js` ^10.6.0
- `vitest` ^5.0.0, `@testing-library/react`, `jsdom`

**There is NO charting library. NO Tailwind. NO CSS-in-JS. NO component library beyond Radix tabs.**
This is deliberate. See section 5.

**Every file in the frontend:**

| Path | Lines | Contents |
| --- | --- | --- |
| `apps/web/app/layout.tsx` | 11 | Root layout |
| `apps/web/app/page.tsx` | 72 | Shell: auth gate, sidebar nav, Radix tab panels |
| `apps/web/app/globals.css` | 38 | **All** styling, plain CSS + custom properties |
| `apps/web/features/dashboard/dashboard.tsx` | 93 | Overview screen |
| `apps/web/features/matches/review-workspace.tsx` | 169 | Three-pane review screen |
| `apps/web/features/mappings/mapping-form.tsx` | 107 | Column mapping |
| `apps/web/features/reconciliations/reconciliations.tsx` | 80 | Definitions + run start |
| `apps/web/features/exceptions/exceptions.tsx` | 65 | Exception queue |
| `apps/web/features/sources/sources.tsx` | 58 | File upload + profiling |
| `apps/web/features/approvals/approvals.tsx` | 26 | Pending approvals |
| `apps/web/features/settings/settings.tsx` | 13 | Tenant settings |
| `apps/web/lib/api-client.ts` | 59 | `fetch` wrapper, bearer token, zod parse |
| `apps/web/lib/schemas.ts` | 194 | All zod response schemas |
| `apps/web/lib/money.ts` | 16 | `formatMoney`, `isZero` |
| `apps/web/tests/money.test.ts` | — | 2 tests |
| `apps/web/tests/schemas.test.ts` | — | 2 tests |

**Navigation order** (from `page.tsx:17-19`), with ordinals `01`–`08`:
Overview, Review, Exceptions, Approvals, Sources, Mappings, Reconciliations, Settings.

**Design tokens** (literal values, `globals.css:2-3`):

```css
--ink: #14211f;  --muted: #65716e;  --paper: #f4f5f1;  --surface: #fff;  --line: #dfe4df;
--green: #0e6b58; --green-dark: #084b3f; --mint: #dceee8; --amber: #b66a1d; --red: #a63d40;
```

It is a **light, warm-paper theme**. Sidebar is dark forest green `#0d2823`. Body font Inter.
`h1` is **Georgia serif** at `clamp(2rem, 4vw, 3.6rem)` with tight negative letter-spacing.
Cards are white, 12px radius, 1px `--line` border, soft shadow. Focus rings are a 3px green halo.

**The `runSummarySchema`** (`apps/web/lib/schemas.ts:44`, exported as type `RunSummary` at line 187)
has 23 fields, in this order:

`run_id`, `reconciliation_id`, `status`, `period_start`, `period_end`, `currency`,
`side_a_balance`, `side_b_balance`, `difference`, `matched_amount`, `matched_transaction_count`,
`auto_matched_count`, `human_approved_count`, `suggested_count`, `exception_count`,
`high_risk_exception_count`, `unmatched_a_count`, `unmatched_b_count`, `oldest_exception_age_days`,
`completion_pct`, `auto_match_rate`, `false_match_rate`, `manual_review_rate`

Money fields (`side_a_balance`, `side_b_balance`, `difference`, `matched_amount`) use the local
`money` string regex at `schemas.ts:5`: `/^-?\d+(?:\.\d+)?$/`.
Only `oldest_exception_age_days` and `false_match_rate` are `.nullable()`. **This matters — see 6.3.**

---

## 3. The design input

The user supplied a screenshot of an unrelated product: a **dark-theme SaaS analytics dashboard**
for a project/CRM/subscription tool called "Soft". It is described here in full so you do not need
the image.

**Shell:** near-black canvas, cards on slightly lighter dark surfaces, ~16px radii, the whole app
framed as a rounded panel on a blue gradient backdrop.

**Sidebar (~180px, dark):** logo mark + wordmark. Icon+label nav rows (Dashboard, Overview,
Calendar, Projects, Clients, Sales Pipeline, Invoices, Support). Active row is a **solid purple
filled pill**. One row carries a **red circular count badge** ("2").

**Top bar:** page title left; pill **search input** with magnifier, placeholder "Search projects,
clients, etc."; message icon; **bell with red unread dot**; user chip (avatar + "Richard Rivers" +
"Product Lead" + chevron).

**Row 1 — four KPI tiles + one tall card.** Each tile: muted label, "…" overflow menu, very large
number, and a **delta pill** with directional arrow + percentage (green up / red down). Values:
Active Users `2,540` (+12.8%), New Trials `925` (−2.17%), Converted `382` (+9.2%), Churn Rate `628`
(−1.06%). The **first tile is filled solid accent purple** while the others are plain. The tall
right card "Cloud Resources" sits on a **light lavender** surface (the only light element) and holds
a thick **radial gauge** with `3,750` / "GB Total Storage" centred, above a **2×2 dot legend**:
`1,550` Project Files, `1,170` Media Assets, `630` Backups, `400` Shared Docs.

**Row 2 — two charts.** "Subscription Growth": pill **date-range selector** ("09–22 May"), two-dot
legend (New Trials purple, Paid Conversions lime), **vertical bar chart** with two overlaid series,
fully rounded bar caps, y-axis 0–400 with faint gridlines, x-axis 17–22 May. "Revenue by Plan": pill
"Today" selector, **donut** with `670` / "Total Accounts" in the hole, and a **legend list** to the
right — swatch + plan name left, value right-aligned: Enterprise 175, Business 125, Pro 95,
Basic 75, Free 50, Legacy 35.

**Row 3 — three cards.** "Active Feature Projects" with muted count `(104)`, a "Sort by: Popular"
pill and a "See All" pill; below, wide horizontal cards each with a coloured icon tile, title, "…"
menu, two **tag chips**, and three rows pairing a salary range with an applicant count. "Tasks" with
a "+" button; rows each carrying a **circular progress ring** (40%, 30%, 60%), title, and
`category • date` subtitle. "Schedule" with a "Today" pill; a **vertical timeline** with a left time
gutter (1:00 PM, 2:30 PM, 4:00 PM), dots on a rail, and coloured event blocks.

**System:** accent purple ~`#7c5cff`, secondary lime ~`#c8f169`. Pill controls everywhere. Small
muted labels over large numeric display type. Status via colour dots and swatches. A "…" affordance
on every card. Heavy padding; information grouped into cards rather than a flat grid.

---

## 4. LOCKED DECISIONS — do not re-open these

These are settled. Do not re-litigate them, do not "improve" on them, do not ask about them again.

### 4.1 Architectural (pre-existing)

1. Frontend stays **Next.js App Router + React + TypeScript strict**. No framework migration.
2. The frontend talks to the **real running backend**. **Mocking the API is forbidden.** Any
   component you build must be fed by a real endpoint.
3. Every API response is validated through a **zod schema** in `apps/web/lib/schemas.ts`.
4. Money is handled as **exact decimal strings**. No `parseFloat` on money.
5. `npm run lint` runs `eslint . --max-warnings=0` and must keep exiting 0. TS strict must keep
   compiling clean.
6. The app stays **keyboard-first and accessible** — visible focus rings, semantic elements, ARIA.
   Non-negotiable for the audit use case.
7. **Do not touch the Python backend as part of a styling change.** Backend work is called out
   explicitly where it is required, and gets its own commit.
8. Work targets `main` in the existing repo. No new sub-project, no parallel app.

### 4.2 Decisions the user made for this redesign

These four were asked and answered. They are inputs, not open questions.

| # | Question | **Decision** |
| --- | --- | --- |
| 1 | Headline copy for a run closeable within tolerance but with a non-zero difference | **"Ready to close" + show the difference.** The headline follows `can_close`; the difference stays visible beside it with the tolerance it was measured against. |
| 2 | How new money components render values, given `money.ts` rounds | **Render the raw decimal string.** New tiles show the exact stored value with no rounding. |
| 3 | Which rate gets promoted to tile weight | **`auto_match_rate`.** `manual_review_rate` stays in the demoted detail grid. |
| 4 | Bug handling order | **Fix bugs first, then redesign**, as separate commits. |

**Note on decision 1:** on the seeded tenant `max_unexplained_difference` is `"0.00"`, so *any*
non-zero difference blocks the close. The "within tolerance" case will not appear with seed data.
Build the copy to handle it correctly anyway — a real tenant will configure a real materiality
threshold.

---

## 5. Charting decision — ADD NO DEPENDENCY

**Decision: hand-roll one inline-SVG horizontal bar component and one CSS legend list.**

After the triage in section 8, exactly **one** chart-shaped dataset survives: the five exception
aging buckets from `GET /exceptions/aging`. Five bars, known ordering, no axes worth drawing, no
zoom, no tooltips, no time series.

Why not a library:

- **Recharts** — ~100 kB+ gzipped; drags in `d3-scale`, `d3-shape`, `d3-array` and a `victory-vendor`
  shim. History of lagging on React major-version peer ranges, and React here is `19.2.8`. Peer
  warnings are not free when `--max-warnings=0` must hold. Its SVG output has no accessible name and
  no table fallback. For five bars this is absurd.
- **Chart.js / react-chartjs-2** — canvas-based. Canvas is opaque to screen readers, cannot be
  selected or copied, and does not print at vector resolution. Disqualified by locked decision 4.1 #6
  and by the audit-package requirement.
- **visx** — least bad; `@visx/scale` + `@visx/shape` is ~15 kB. But for five bars it gives you a
  linear scale you can write as `count / max * 100` and a rect you can write as `<rect>`.
- **Nivo / Tremor** — Tremor assumes Tailwind, which is not installed.

An inline SVG bar row is ~40 lines, costs nothing in bundle, renders identically server- and
client-side (no hydration mismatch, no `ssr: false` dynamic import), prints as vector, and can be
made properly accessible: wrap it in `role="img"` with an `aria-label`, and pair it with a real
`<table>` inside the existing `.sr-only` class (`globals.css:35`) so a screen-reader user gets the
exact figures rather than a summary.

There is a second, product-specific argument. **A charting library encourages charts.** Once
Recharts is in `package.json`, the pressure to draw `auto_match_rate` as a gauge and the A/B balances
as a donut becomes real, and both of those are fabrications (see 8.2). Not installing one is a design
decision, not just a bundle decision.

---

## 6. BUGS — fix these first, in this order, as separate commits

All five were verified against the running code. Each is real. Commit them **separately** so a
regression is attributable.

### 6.1 BUG — `GET /exceptions/aging` returns HTTP 500 (crash)

**Severity: blocking.** This endpoint has never worked against the SQLite dev/demo path, and Phase 3
of the redesign depends on it.

**Reproduce:**

```bash
TOKEN=$(grep NEXT_PUBLIC_DEV_TOKEN apps/web/.env.local | cut -d= -f2)
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8000/api/v1/exceptions/aging?run_id=<your-run-id>"
# → Internal Server Error
```

Server log shows:

```
TypeError: can't subtract offset-naive and offset-aware datetimes
```

**Root cause.** `packages/exceptions/aging.py:41` computes:

```python
return ((now or utc_now()) - exception.first_detected_at).days
```

`utc_now()` (`packages/domain/dates/dates.py:46`) returns `datetime.now(UTC)` — timezone-**aware**.
The column is declared `DateTime(timezone=True)` (`apps/api/app/infrastructure/models.py:618`), but
**SQLite has no timezone type**: SQLAlchemy's SQLite dialect stores a naive string and returns a
naive `datetime` regardless of the `timezone=True` flag. So on the dev/demo path
`exception.first_detected_at` is naive and the subtraction raises.

This bites in **three** places:

| Location | Expression |
| --- | --- |
| `packages/exceptions/aging.py:41` | `(now or utc_now()) - exception.first_detected_at` |
| `packages/exceptions/aging.py:47` | `(now or utc_now()) > exception.due_at` |
| `packages/domain/models/exceptions.py:68` | `(utc_now() - self.first_detected_at).days` |

**Fix at the mapper boundary, not at each call site.** Three call sites means patching each one is
whack-a-mole, and the fourth will be written next month. The DB-row → domain-object boundary is the
correct layer, and **the codebase already does exactly this** for audit events —
`apps/api/app/infrastructure/mappers.py:334`:

```python
occurred_at=ensure_utc(row.occurred_at),
```

with a docstring above it explaining that a dropped offset would break the hash chain on read. The
same author already knew about this problem and fixed it for one table only.

**Change:** in `apps/api/app/infrastructure/mappers.py`, function `row_to_exception` (starts line
253), wrap the two datetime fields:

- line 268: `first_detected_at=row.first_detected_at,` → `first_detected_at=ensure_utc(row.first_detected_at),`
- line 269: `due_at=row.due_at,` → `due_at=ensure_utc(row.due_at) if row.due_at is not None else None,`

`first_detected_at` is non-nullable; `due_at` is `datetime | None` (`models.py:619`). **Do not pass
`None` into `ensure_utc`.**

`ensure_utc` is already imported in that file at line 22. Do not add an import.

**Then audit the other mappers in the same file** for the same hazard — any `row_to_*` function
returning a domain object with a datetime that later takes part in arithmetic. Fix what you find in
this same commit and list it in the commit message.

**Regression test — required.** The reason 332 tests missed this: every existing test constructs
`ExceptionRecord` objects **in memory**, where `utc_now()` produced the timestamps and everything is
aware. A pure unit test on `build_aging_report` with a naive datetime reproduces the crash but does
not cover the class of bug.

Write an **integration** test that hits `GET /exceptions/aging` through the API against a persisted
SQLite session — the DB round-trip is what makes the datetime naive. Home it in
`tests/integration/test_persistence.py`. Assert HTTP 200 and that bucket counts sum to `total_open`.

**Verify:**

```bash
python -m pytest tests -q --basetemp=./.pytest-tmp    # must be 333+ passed, 0 failed
python scripts/verify_build_checklist.py              # must still be 26/26
# restart the API, then:
curl -s -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:8000/api/v1/exceptions/aging?run_id=<run-id>"
# must return JSON, not "Internal Server Error"
```

**Once it returns 200, check one more thing before writing the zod schema in Phase 3:** the exposure
fields are serialised with Python `str(Decimal)` (`apps/api/app/services/exceptions.py:72,74,82`).
For very large or very small values `str(Decimal)` emits scientific notation (`1E+3`), which the
`money` regex at `schemas.ts:5` **rejects**. If you see that in a real response, it is a backend
serialisation fix — **do not loosen the regex.**

### 6.2 BUG — the Overview readiness verdict contradicts the backend close gate

**Severity: high.** The largest string on the screen can assert something the backend disagrees with.

`apps/web/features/dashboard/dashboard.tsx:50`:

```typescript
const reconciled = isZero(summary.difference) && summary.exception_count === 0;
```

The backend's actual close gate is `CloseService.preflight` (`apps/api/app/services/close.py:89-109`),
which returns `can_close` computed from **five different blockers**:

`unresolved_required_exceptions`, `pending_approvals`, `missing_evidence`,
`blocking_quality_errors`, and `unexplained_difference` compared against a configured
`max_unexplained_difference`.

These are not the same rules. The UI can therefore say "Accounts are reconciled / Ready" for a run
the backend would refuse to close, or "Attention is required" for a run that is closeable within
materiality tolerance.

Live proof against the seeded run — the endpoint works today and returns:

```json
{"run_id":"85bd6001-5e79-4e8c-8cb1-a964e3b1b5cb","status":"REVIEW_REQUIRED","can_close":false,
 "unresolved_required_exceptions":0,"pending_approvals":1,"unexplained_difference":"-236.70",
 "max_unexplained_difference":"0.00","missing_evidence":0,"blocking_quality_errors":0,
 "entity":"default"}
```

Note that the real blocker is **`pending_approvals: 1`** — which the current UI never mentions —
while `unresolved_required_exceptions` is `0` even though the summary reports 5 exceptions. The two
screens are telling different stories about the same run.

**This bug is fixed by Phase 1 (section 7.1), not by a separate commit.** Wiring preflight *is* the
fix. Do not patch `dashboard.tsx:50` with a different local formula.

### 6.3 BUG — "Not yet measured" is implemented for one rate field out of four

**Severity: medium.** A documented product rule is only 25% applied.

`apps/web/features/dashboard/dashboard.tsx:10-12` defines:

```typescript
function percent(value: number | null): string {
  return value === null ? "Not yet measured" : `${(value * 100).toFixed(1)}%`;
}
```

It handles `null` correctly. But in `apps/web/lib/schemas.ts` only `false_match_rate` is
`.nullable()` (line 66). Lines 64, 65 and 67 declare `completion_pct`, `auto_match_rate` and
`manual_review_rate` as plain `z.number()`.

The backend confirms the hazard — `apps/api/app/services/reconciliation.py:607-609`:

```python
completion = (matched_transactions / total_transactions) if total_transactions else 0.0
auto_rate  = (len(auto) / len(result.matches)) if result.matches else 0.0
review_rate = (...)
```

A run with **no attempted matches** yields `0.0`, which renders as `0.0%` — indistinguishable from
a measured-and-genuinely-zero automation rate. That is precisely the ambiguity the rule exists to
prevent.

The author's intent is documented at `reconciliation.py:635-637`, above `false_match_rate=None`:

> No false matches have been confirmed yet for this run. The value stays None rather than 0.0 so the
> dashboard shows "not yet measured" instead of implying a proven zero.

The same reasoning applies to the other three; it was simply not carried through.

**Fix — this one crosses into the backend, so it gets its own commit and its own review.**

1. `apps/api/app/services/reconciliation.py:607-609` — return `None` instead of `0.0` when the
   denominator is zero. Type the three `RunSummary` fields as `float | None`.
2. Find the Pydantic `RunSummary` model and make `completion_pct`, `auto_match_rate` and
   `manual_review_rate` optional.
3. `apps/web/lib/schemas.ts:64,65,67` — add `.nullable()` to all three.
4. `dashboard.tsx` needs no change; `percent()` already handles `null`.

**⚠ Contract warning.** These endpoints have **no FastAPI `response_model`** — they return bare
`dict[str, Any]`. Nothing but your zod schema will catch a drift between backend and frontend. Add a
case to `apps/web/tests/schemas.test.ts` parsing a payload with all three rates `null`.

**Verify:** full pytest suite still 0 failures; checklist still 26/26; a live
`GET /runs/{run_id}/summary` still parses.

### 6.4 BUG — two buttons in the review workspace do the same thing

**Severity: medium.** One button is lying to the user today.

`apps/web/features/matches/review-workspace.tsx:159-160`:

```tsx
{can(me.permissions, "match:create") && <button onClick={() => void manualMatch()}>Create manual match</button>}
{can(me.permissions, "match:create") && <button onClick={() => void manualMatch()}>Split / group</button>}
```

Both call `manualMatch()`. "Split / group" does not split or group anything.

**Fix: delete line 160.** Removing it is the honest minimal change.

**Do NOT implement split/group.** That is unrequested scope. Note in the commit message that the
permissions `match:override` and `match:unmatch` both exist, so a real split/group action may have
been intended — if the user wants it built, that is a separate task with its own spec.

**Verify:** `npx tsc --noEmit` exits 0; `npm run lint` exits 0; `npm test` passes; the review screen
still renders and "Create manual match" still works against the live API.

### 6.5 Endpoint reachability audit (do this while you are in here)

`GET /exceptions/aging` was broken because **nothing called it** — not the frontend, not a test.
Two more read endpoints are in the same position and are dependencies of Phases 1 and 4. Both were
curled while writing this document and **both currently work**:

| Endpoint | Status | Live response against the seeded run |
| --- | --- | --- |
| `GET /runs/{run_id}/close-preflight` | ✅ 200 | see 6.2 |
| `GET /audit/verify` | ✅ 200 | `{"verified":true,"event_count":36,"first_invalid_index":null,"first_invalid_event_id":null}` |

Re-curl both before you build against them. If either regresses, fix it before writing its schema.

---

## 7. THE REDESIGN — phased

Start only after section 6 is complete and the baseline still holds.

### 7.1 PHASE 1 — Wire close-preflight (fixes bug 6.2)

**This is the highest-value change on the screen and it has nothing to do with the screenshot.**
It replaces a locally-invented verdict with the backend's real one.

**Files:**

1. **Modify `apps/web/lib/schemas.ts`** — add `closePreflightSchema` and `type ClosePreflight`.
   Fields, exactly matching `close.py:98-109`:

   ```typescript
   export const closePreflightSchema = z.object({
     run_id: z.string().uuid(),
     status: z.string(),
     can_close: z.boolean(),
     unresolved_required_exceptions: z.number().int(),
     pending_approvals: z.number().int(),
     unexplained_difference: money,
     max_unexplained_difference: money,
     missing_evidence: z.number().int(),
     blocking_quality_errors: z.number().int(),
     entity: z.string(),
   });
   ```

   The counts are `int` — `_blockers` (`close.py:295`) is typed `dict[str, int]`. The two difference
   fields use the existing `money` string regex, **not** `z.number()`.

2. **Create `apps/web/features/dashboard/close-checklist.tsx`** — renders the four blocker counts
   plus the difference-vs-tolerance comparison as a checklist. Each row states the blocker, its
   count, and pass/fail. Use a semantic `<ul>`. **Convey state with text, not colour alone.**

3. **Modify `apps/web/features/dashboard/dashboard.tsx`** — replace the local `reconciled` at line 50
   with `preflight.can_close`. Derive the `h1` and the `.status` badge from it, per **locked decision
   4.2 #1**: headline `"Ready to close"` when `can_close`, with the difference shown beside it and
   the tolerance it was measured against. Keep a graceful fallback if the preflight call fails —
   never fall back to the old local formula.

**Endpoint:** `GET /runs/{run_id}/close-preflight`. Exists (`apps/api/app/api/reconciliations.py:202`),
requires only `VIEW_DASHBOARD`. **No backend work.**

**Verify:** on the seeded run, the checklist names `pending_approvals: 1` as the blocker, and the
headline matches `can_close` exactly. Add a preflight-parsing case to `apps/web/tests/schemas.test.ts`.

### 7.2 PHASE 2 — Restructure Overview into a hierarchy

**This is the one structural idea worth taking from the reference.** Today 15 metrics render at
identical visual weight in `.metric-grid` (`globals.css:28`), so "Side A balance" and
"Human-approved" shout equally loudly. That is the actual problem with the screen — not that it is
plain.

**Files:**

1. **Modify `apps/web/app/globals.css`** — add `.stat-row` (4-column grid of tall tiles) and
   `.overview-grid` (2-column card region below). **Do not delete `.metric-grid`** — demote it to an
   "All measures" block below the fold so nothing currently visible disappears.

2. **Modify `apps/web/features/dashboard/dashboard.tsx`** — promote four figures to large tiles:
   - `difference` — **dominant**, with a severity fill: `--mint` when `isZero`, `#fff0da` / `--amber`
     when non-zero. This is the reference's "filled first tile" idea repurposed as **semantics, not
     decoration**.
   - `exception_count`
   - `high_risk_exception_count`
   - `unmatched_a_count` / `unmatched_b_count`
   - Plus `auto_match_rate` per **locked decision 4.2 #3**. `manual_review_rate` stays demoted.

3. **Money rendering in the new tiles** — per **locked decision 4.2 #2**, render the **raw decimal
   string**, not `formatMoney`. `apps/web/lib/money.ts:11` does
   `precise.toDecimalPlaces(2).toFixed(2)`, which is rounding for display and in tension with locked
   decision 4.1 #4. That matters more in large display type, where a rounded figure is more likely
   to be read as authoritative and transcribed into a workpaper.
   **Do not change `money.ts` itself** — existing callers depend on its current behaviour. Add a new
   exact-rendering helper alongside it and use that in the new tiles only.

**Endpoint:** `/runs/{run_id}/summary`, unchanged, already parsed by `runSummarySchema`.

**Verify:** every one of the 23 `runSummarySchema` fields rendered today is still rendered
**somewhere** on the page — nothing may be lost in the promotion. Keyboard tab order still reaches
the run picker and every control. Focus rings from `globals.css:17` intact.

### 7.3 PHASE 3 — Aging bars + severity/category legend lists

**Blocked on 6.1.** Do not start until `/exceptions/aging` returns 200.

**Files:**

1. **Modify `apps/web/lib/schemas.ts`** — add `exceptionAgingSchema`:

   ```typescript
   export const exceptionAgingSchema = z.object({
     total_open: z.number().int(),
     total_exposure: money,
     overdue_count: z.number().int(),
     overdue_exposure: money,
     oldest_age_days: z.number().int().nullable(),
     by_category: z.record(z.string(), z.number().int()),
     by_severity: z.record(z.string(), z.number().int()),
     buckets: z.array(z.object({
       label: z.string(),
       count: z.number().int(),
       exposure: money,
       high_risk_count: z.number().int(),
     })),
   });
   ```

   Bucket labels are exactly `"0-7"`, `"8-30"`, `"31-60"`, `"61-90"`, `"90+"`
   (`packages/exceptions/aging.py:17-23`).

2. **Create `apps/web/components/bar-list.tsx`** — inline SVG **horizontal** bars.
   Horizontal because the bucket labels are text, and because this is the form every controller
   already recognises from an AR/AP aging report. **Drop the reference's rounded bar caps** — they
   distort the readable length of short bars.
   `role="img"` + `aria-label`, with a `.sr-only` `<table>` sibling carrying the exact figures.
   **No dependency** — see section 5.

3. **Create `apps/web/components/legend-list.tsx`** — swatch + label left, right-aligned count.
   This is the reference's donut legend **with the donut removed**. Reuse the `.severity` colours
   already defined at `globals.css:34` (LOW / MEDIUM / HIGH / CRITICAL).

4. **Create `apps/web/features/dashboard/aging-card.tsx`** — composes both, one fetch.

**Endpoint:** `GET /exceptions/aging?run_id={runId}`, requires `VIEW_EXCEPTIONS`.

**⚠ Permission gating.** `dashboard.tsx` currently receives **no `me` prop**. To gate this fetch on
`can(me.permissions, ...)` you must pass `me` through from `page.tsx:62`. That is a one-line prop
addition — **not** a refactor. Do not restructure the shell.

**Verify:** bucket counts sum to `total_open`; a screen reader announces the numeric table; the card
prints legibly in browser print preview.

### 7.4 PHASE 4 — Audit chain verification strip

Directly serves *"can I prove it to an auditor?"* — which the Overview screen currently says nothing
about.

1. **Modify `apps/web/lib/schemas.ts`** — add `auditVerifySchema` (per `apps/api/app/api/audit.py:41-59`):

   ```typescript
   export const auditVerifySchema = z.object({
     verified: z.boolean(),
     event_count: z.number().int(),
     first_invalid_index: z.number().int().nullable(),
     first_invalid_event_id: z.string().nullable(),
   });
   ```

2. **Modify `apps/web/features/dashboard/dashboard.tsx`** — a small integrity strip:
   "Audit chain verified · 36 events", or on failure the exact divergence index. Reuse `.notice` /
   `.error` from `globals.css:30`.

**Endpoint:** `GET /audit/verify`, requires `VIEW_AUDIT` — gate the render with `can()`.

**Verify:** renders `verified: true` against the seeded chain (36 events); renders **nothing at all**
for a principal lacking `VIEW_AUDIT`.

### 7.5 PHASE 5 — Polish, only after 1–4 land

- `globals.css:24` — change `.nav button[data-state=active]` from `rgba(220,238,232,.12)` to a
  **filled `--green` pill**. This is the reference's purple active-pill idea in our palette.
- Card radius 12px → 14px. Cosmetic, free.
- **Live** pending-approvals count on the Approvals nav row — the reference's "+2" badge, but fed by
  a real count and coloured `--amber`, **not** red (`--red` is reserved for severity, `globals.css:34`).
  Ship this **only** if the count is live. A hardcoded badge is worse than no badge.
- "See all exceptions" as a **genuine Radix tab switch** to the `exceptions` tab (`page.tsx:64`),
  not a decorative pill.

---

## 8. Component triage — the full reference design, item by item

### 8.1 ADOPT / ADAPT

| Reference element | Call | Our equivalent |
| --- | --- | --- |
| KPI tile: label + very large number | **ADOPT** | `difference`, `exception_count`, `high_risk_exception_count`, unmatched counts. Phase 2. |
| First tile visually filled/accented | **ADAPT** | Repurpose as **severity**, not selection: fill the Difference tile `--mint` when zero, `--amber` when not. |
| 2×2 dot-legend grid | **ADOPT** | Exception composition by severity, colours from `.severity`. Phase 3. |
| Bar chart | **ADAPT → horizontal** | The five aging buckets. Horizontal; no rounded caps. Phase 3. |
| Donut + right-hand legend list | **ADAPT — keep the list, drop the donut** | `by_category` counts, swatch + name left, value right-aligned. Phase 3. |
| "Tasks" list with "+" | **ADAPT** | The **close checklist** from `close-preflight`. No "+" — nothing to add. Phase 1. **The single most valuable card on the new screen.** |
| Solid filled active-nav pill | **ADAPT** | Same shape, our `--green`. Phase 5. |
| Red count badge on a nav row | **ADAPT, carefully** | Pending approvals, from a **live** count, in `--amber`. Phase 5, conditional. |
| "See All" link | **ADAPT** | Only where the destination exists — a real Radix tab switch. Phase 5. |
| ~16px card radii | **ADAPT** | 12px → 14px. Phase 5. |
| Small muted label over large numeric type | **ADOPT** | Already half-present via `.eyebrow` + `.metric strong`. Push it further. |
| User chip (avatar + name + role) | **ALREADY HAVE IT** | `page.tsx:58` `.identity`. Nothing to do. Skip the chevron — it opens nothing. |
| Vertical timeline with time gutter | **ADAPT — DEFER** | Audit event chain (`/audit/events`). Belongs on the **Review detail pane**, not Overview. Not in this plan. |
| Pill-shaped controls generally | **ADAPT, sparingly** | `.status` and `.severity` are already `border-radius: 999px`. Extend to nav active state only. **Do not pill-ify `button`** — 8px rectangles read as pressable; pills already mean "passive label" in this codebase. |

### 8.2 REJECT — and why

Each of these was considered and rejected on **domain** grounds, not taste. Do not add them back.

| Element | Why not |
| --- | --- |
| **Dark theme** | Three concrete reasons. (a) **Print parity** — this product exists to produce audit packages (`/runs/{run_id}/audit-package`) that get PDF'd into workpapers; a dark screenshot pasted into a workpaper is unusable. (b) **Dense numeric reading** — the core screen is a three-pane table-heavy grid read for hours; light-on-dark degrades legibility for long numeric strings and hairline rules. (c) **Convention** — ledger and audit tooling is light because it descends from paper. It would also mean re-deriving every semantic colour in `globals.css:27,30,34` for dark contrast: days of work whose main effect is worse printing. |
| **Donut / radial gauge** | **A correctness objection.** `difference` is **signed** — the seeded value is `EUR -236.70`, and an arc cannot encode a negative. And `side_a_balance` / `side_b_balance` are two **independent** totals from two independent systems, not slices of one whole. Any donut here would drop the sign or fake a whole. |
| **Delta / trend pills (+12.8%)** | **No data exists.** `runSummarySchema` has no prior-period field, no trend, no comparison basis. The only way to render them is to invent a baseline. Would require a backend change; flagged, not built. |
| **Global search box** | No search endpoint. `/transactions` takes `limit`/`offset`, not a free-text query. |
| **Bell, message icons, unread dot** | No notification system, no messaging. Comments exist but are per-exception, not a global inbox. |
| **"…" overflow menu on every card** | Overview has **zero** card-level actions. Every menu would be empty or fake. |
| **Circular per-row progress rings** | Exceptions have a **10-state status enum** (`exceptions.tsx:9`), not a linear percentage. A ring would fabricate a progress model that does not exist. |
| **Salary-range / applicant-count rows** | Content-specific to a job board. Meaningless here. |
| **Icon-based sidebar nav** | The current `01`–`08` ordinals (`page.tsx:17-19`) encode a **close sequence**. Icons encode nothing and would cost eight hand-drawn SVGs or a new dependency. |
| **Purple `#7c5cff` / lime `#c8f169` accents** | The token set at `globals.css:2-3` is coherent and semantic. Two saturated accents with no meaning attached break colour-as-signal. |
| **Date-range pill / "Today" / "Sort by" pills** | No endpoint accepts these parameters. Runs are already period-scoped via `period_start`/`period_end`. |
| **Rounded panel on a gradient backdrop** | Decoration that costs vertical space on a desktop-first tool. |
| **"Cloud Resources" light card in a dark UI** | An artefact of a dark theme we are not adopting. |
| **A charting dependency** | Section 5. |
| **Component-library migration / design-token overhaul** | `globals.css` is 38 lines and coherent. Extending it is cheaper and lower-risk than replacing it. |

**The governing principle for every REJECT above:** this project's own documentation already names
"rendering an action the user cannot perform, then failing at the API" as a failure mode — and
`review-workspace.tsx:160` (bug 6.4) proves it has already happened once. A badge, a search box, a
"…" menu and a "See All" link that lead nowhere would multiply that failure by ten.

---

## 9. Rules while you work

1. **Do not create a worktree, sandbox branch, or isolated copy.** Work directly on `main`.
2. **Read the files before you change them.** Every line number in this document was accurate when
   written; verify before editing.
3. **Do not refactor code that is not part of the task.** No cleanup passes, no renames, no
   reorganising imports.
4. **Do not create new files unless this document names them.** Prefer editing existing ones.
5. **Commit from `main`. Never force push. Never `--no-verify`.** Do not push unless asked.
6. One commit per bug (6.1, 6.3, 6.4) and one per phase (7.1–7.5).
7. If a phase turns out to be blocked, **finish every other phase in full** and say explicitly what
   you left out and why. Do not silently narrow scope.
8. **An unexpected result is not automatically a bug.** Investigate neutrally before "fixing".

---

## 10. Proof of execution — required before reporting any item done

A completion summary is a **claim**, not evidence. Paste actual terminal output.

```bash
# Backend — after every backend change
python -m pytest tests -q --basetemp=./.pytest-tmp | tail -3
python scripts/verify_build_checklist.py | tail -3

# Frontend — after every frontend change
cd apps/web
npx tsc --noEmit          # must exit 0
npm run lint              # must exit 0, zero warnings
npm test                  # must pass
npm run build             # must exit 0

# Repo state
git branch --show-current
git log --oneline | head -15
git status --short
```

**An item counts as done only if:**

- pytest shows **0 failures** (the count rises above 332 as you add tests — expected and correct)
- `verify_build_checklist.py` still shows **26/26**
- `npx tsc --noEmit`, `npm run lint`, `npm test` and `npm run build` all exit 0
- the work is committed on `main`

**Do not report `EXIT=$?` after a pipe** — that captures the exit code of `tail`, not the command.
Quote the output lines instead.

**Additionally, for any phase that renders data:** paste the actual live API response you built
against, and confirm the rendered figures match it. A component that type-checks but shows the wrong
number passes every gate above.

---

## 11. Open question left for the user

One question from the analysis could not be answered because it was blocked by bug 6.1:

**Does the real `/exceptions/aging` response emit any exposure value in scientific notation?**
`str(Decimal)` can produce `1E+3`, which the `money` regex at `schemas.ts:5` rejects. Fix 6.1 first,
then curl the endpoint and inspect the `exposure` and `total_exposure` fields against real data. If
scientific notation appears, Phase 3 gains a backend serialisation dependency — and the fix belongs
in `apps/api/app/services/exceptions.py:72,74,82`, **not** in a loosened regex.

---

## 12. The one thing to remember

The reference screenshot is a **growth dashboard** — telemetry a product manager skims for twenty
seconds. This is an **evidentiary tool** — data a controller reads for hours and defends to an
auditor. Borrow the reference's *hierarchy*, never its *confidence*.

The failure mode to avoid is not ugliness. It is **false authority**: a screen that looks more
decisive without being more correct. That is exactly what `dashboard.tsx:50` does today, and why
Phase 1 comes before every pixel of visual work.
