# recon-platform

An AI-assisted financial reconciliation platform.

**Design principle: deterministic accounting logic first. AI assists ambiguity; it does not own financial truth.**

The platform automatically reconciles what can be proven, intelligently suggests
what is likely, and gives finance teams a controlled workflow for everything else.

See `docs/architecture.md` for the full picture and `docs/matching-engine.md` for
how a match is decided.

## Quick start

```bash
make install      # python -m pip install -e ".[dev]" && npm install in apps/web
make dev          # docker compose up: postgres, redis, minio, api, worker, web
make migrate      # alembic upgrade head
make seed         # load the demo tenant and golden fixture data
make test         # pytest + frontend tests
make checklist    # walk the pre-production build checklist
```

## Layout

| Path | Contents |
| --- | --- |
| `apps/api` | FastAPI application, `/api/v1` |
| `apps/web` | Next.js + TypeScript frontend |
| `packages/domain` | Money, dates, enums, canonical models |
| `packages/ingestion` | Parsers, profiler, mapping, normalisation, quality gate |
| `packages/connectors` | Connector contract and implementations |
| `packages/matching` | The staged matching engine |
| `packages/exceptions` | Exception classification and workflow |
| `packages/ai` | Provider abstraction; advisory only |
| `packages/controls` | RBAC, approvals, materiality, period lock, SoD |
| `packages/audit` | Append-only audit events, lineage, evidence |
| `workers` | Celery jobs |
| `tests` | Unit, integration, golden accounting cases, security, performance |
