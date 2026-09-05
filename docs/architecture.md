# Architecture

The platform is organized around one rule: deterministic accounting logic owns financial truth. AI may classify, summarize, or suggest, but it cannot approve a match, close a run, or post an accounting action.

## System flow

The implemented architecture follows the layer diagram in specification section 107:

```text
Bank / ERP / payment processor / CSV
                  |
             connectors
                  |
          immutable raw records
                  |
       profile, map, normalize, validate
                  |
        canonical transactions
                  |
  eligibility -> exact -> rules -> grouping
       -> candidates -> score -> ambiguity -> policy
                  |
        +---------+----------+
        |                    |
 deterministic auto-match   review suggestion
        |                    |
        +---------+----------+
                  |
          exception workflow
                  |
       optional AI assistance
                  |
          controlled approval
                  |
         accounting proposal
                  |
            audit and close
```

The raw layer is represented by `source_files` and immutable `source_records`. Mapping and normalization in `packages/ingestion` produce `canonical_transactions` plus `transaction_lineage`. A reconciliation run freezes its input IDs, source checksums, rule versions, configuration version, and model version in `run_transaction_snapshots`. The matching engine reads that frozen input, not the live transaction tables. This makes replay meaningful and prevents a later import from silently changing a prior run.

## Code layers and dependency rule

`packages/domain` contains value objects, enums, and Pydantic models. `packages/ingestion`, `packages/matching`, `packages/exceptions`, `packages/controls`, `packages/audit`, `packages/ai`, and `packages/connectors` implement reusable business capabilities. `apps/api` owns HTTP, authentication dependencies, services, repositories, SQLAlchemy models, and storage adapters. `workers` invokes application workflows asynchronously through Celery.

The dependency direction is strict: code under `packages` never imports from `apps` or `workers`. Domain and engine code therefore remains usable without FastAPI, SQLAlchemy sessions, Celery, or a running database. Application and worker layers may import packages. Infrastructure adapters translate between portable domain objects and external systems.

```text
apps/api ----+
             +----> packages/*
workers -----+

packages/* -X-> apps/api
packages/* -X-> workers
```

The database is PostgreSQL in production. Tests use SQLite, with portable `GUID`, `JSONColumn`, and `Money` types in `apps/api/app/infrastructure/types.py`. `Money` becomes `NUMERIC(24, 8)` on PostgreSQL and an exact, order-preserving string encoding on SQLite. Object storage is selected through `build_storage`: S3-compatible storage in production and local storage in development.

## Why the matching engine is a pure function

`packages.matching.engine.MatchingEngine.run` takes side A transactions, side B transactions, a reconciliation configuration, a versioned rule set, and a `MatchingContext`. It returns a `MatchingResult`. It does not query a database, publish a queue message, call an AI model, or mutate source records. Wall-clock access is limited to enforcing bounded grouping-search budgets.

This boundary matters for accounting assurance. A frozen snapshot and the same versions can be replayed; golden cases run as ordinary tests; stage outputs can be inspected; and a failure cannot leave half-written match groups. The engine asserts match exclusivity and allocation limits at stage boundaries. Persistence occurs only after the service receives a coherent result.

## HTTP request path

The normal synchronous path is:

```text
HTTP request
  -> FastAPI router (`apps/api/app/api`)
  -> authentication and `RequestContext` dependencies
  -> application service (`apps/api/app/services`)
  -> tenant-scoped repository (`apps/api/app/infrastructure/repositories.py`)
  -> SQLAlchemy session and database
```

Bearer-token verification creates a principal with a tenant, actor type, user ID, and permissions. `RequestContext` couples that principal to the request's SQLAlchemy session, audit sink, settings, storage adapter, and correlation ID. Routers validate transport schemas and check permissions; services coordinate controls and domain operations; repositories apply `tenant_id` before ID or filter predicates. The session dependency commits only after a successful request and rolls back on error.

Longer jobs follow the same boundaries through `workers`: the API records or queues work, a Celery task reconstructs the required context, the service performs the workflow, and repositories persist the outcome. The matching package itself remains unaware of HTTP and Celery.

## Trust and audit boundaries

Source payloads and checksums are retained rather than overwritten. Normalized fields carry per-field lineage. Rules, configuration, model identity, and run inputs are versioned in the snapshot. Match groups store score, confidence, reason codes, warnings, competing-candidate count, actor, and approval state. Audit events are append-only and hash-chained per tenant. Closing a run requires all five close gates and creates a certificate bound to the stored result hash and snapshot hash.

This design intentionally favors a visible unmatched item over a plausible but unprovable match. Uncertainty is routed to review or the exception workflow; it is not hidden by automation.
