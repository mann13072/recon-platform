#!/usr/bin/env python
"""Seed a demo tenant and walk the whole workflow (spec section 102).

Creates a tenant, three users with different roles, ingests a bank and a ledger
export, runs a reconciliation, and prints a development token for each user so
the API and the frontend can be driven immediately.

    python scripts/seed_demo.py
    python scripts/seed_demo.py --database-url postgresql+psycopg://recon:recon@localhost:5432/recon

Refuses to run against a production configuration.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from uuid import UUID, uuid4, uuid5

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apps.api.app.config import Settings  # noqa: E402
from apps.api.app.infrastructure.auth import issue_dev_token  # noqa: E402
from apps.api.app.infrastructure.models import Base, TenantRow, UserRoleRow, UserRow  # noqa: E402
from apps.api.app.infrastructure.storage import LocalObjectStorage  # noqa: E402

DEMO_NAMESPACE = UUID("f4d1a2b3-6c7e-4a58-9b10-2e5c8d3f7a90")

BANK_CSV = b"""Date,Description,Reference,Counterparty,Amount,Currency
2026-08-02,BANK FEE MONTHLY,,DEUTSCHE BANK,-12.50,EUR
2026-08-05,SEPA CT INV-4930 ACME GMBH,INV-4930,ACME GMBH,1250.00,EUR
2026-08-09,SEPA CT INV-4931 BETA LTD,INV-4931,BETA LTD,875.50,EUR
2026-08-12,SEPA CT INV-4932 GAMMA BV,INV-4932,GAMMA BV,2410.00,EUR
2026-08-14,CARD PAYMENT UNKNOWN MERCHANT,,UNKNOWN,-64.20,EUR
2026-08-18,SEPA CT INV-4933 DELTA SARL,INV-4933,DELTA SARL,640.75,EUR
2026-08-22,CHARGEBACK CASE 4471,,STRIPE,-120.00,EUR
2026-08-26,REFUND TO CUSTOMER ORDER 991,,ACME GMBH,-250.00,EUR
2026-08-28,SEPA CT INV-4934 EPSILON AB,INV-4934,EPSILON AB,3300.00,EUR
2026-08-31,STRIPE PAYOUT 8F42,,STRIPE,982.45,EUR
"""

LEDGER_CSV = b"""PostingDate,Memo,Reference,InvoiceNo,Counterparty,Value,Ccy
2026-08-05,Customer payment,INV-4930,INV-4930,ACME GMBH,1250.00,EUR
2026-08-09,Customer payment,INV-4931,INV-4931,BETA LTD,875.50,EUR
2026-08-12,Customer payment,INV-4932,INV-4932,GAMMA BV,2410.00,EUR
2026-08-18,Customer payment,INV-4933,INV-4933,DELTA SARL,640.75,EUR
2026-08-20,Office supplies,,,OFFICE DEPOT,-210.00,EUR
2026-08-29,Customer payment,INV-4934,INV-4934,EPSILON AB,3300.00,EUR
2026-08-31,Stripe clearing,,,STRIPE,982.45,EUR
"""

USERS = [
    ("alice", "alice@acme.example", ["PREPARER"], "prepares and runs"),
    ("bob", "bob@acme.example", ["CONTROLLER"], "reviews, approves and closes"),
    ("carol", "carol@acme.example", ["AUDITOR"], "reads everything, changes nothing"),
]


def build_settings(database_url: str, storage_path: Path) -> Settings:
    return Settings(
        ENVIRONMENT="development",
        DATABASE_URL=database_url,
        AUTH_DEV_SECRET="local-development-secret-at-least-32-characters",
        AUTH_AUDIENCE="",
        AI_ENABLED=False,
        LOCAL_STORAGE_PATH=str(storage_path),
        OBJECT_STORAGE_ENDPOINT="",
        CREDENTIAL_ENCRYPTION_KEY="local-development-credential-key-32-chars",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default="sqlite+pysqlite:///./recon-demo.sqlite3",
        help="target database (default: a local SQLite file)",
    )
    parser.add_argument("--storage", type=Path, default=Path("./var/storage"))
    parser.add_argument("--tenant-slug", default="acme-demo")
    args = parser.parse_args()

    settings = build_settings(args.database_url, args.storage)
    if settings.is_production:
        print("Refusing to seed demo data into a production environment.", file=sys.stderr)
        return 1

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(settings.database_url, future=True)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    storage = LocalObjectStorage(root=args.storage)
    session = session_factory()

    try:
        tenant = session.query(TenantRow).filter(TenantRow.slug == args.tenant_slug).one_or_none()
        if tenant is None:
            tenant = TenantRow(
                id=uuid5(DEMO_NAMESPACE, args.tenant_slug),
                name="Acme GmbH (demo)",
                slug=args.tenant_slug,
            )
            session.add(tenant)
            session.flush()
            print(f"created tenant {tenant.slug} ({tenant.id})")
        else:
            print(f"reusing tenant {tenant.slug} ({tenant.id})")

        tokens: dict[str, str] = {}
        principals: dict[str, object] = {}

        for subject, email, roles, _ in USERS:
            user = (
                session.query(UserRow)
                .filter(UserRow.tenant_id == tenant.id, UserRow.external_subject == subject)
                .one_or_none()
            )
            if user is None:
                user = UserRow(
                    id=uuid5(DEMO_NAMESPACE, f"{args.tenant_slug}:{subject}"),
                    tenant_id=tenant.id,
                    external_subject=subject,
                    email=email,
                    display_name=subject.title(),
                )
                session.add(user)
                session.flush()
                for role in roles:
                    session.add(
                        UserRoleRow(id=uuid4(), tenant_id=tenant.id, user_id=user.id, role=role)
                    )
            tokens[subject] = issue_dev_token(
                settings,
                subject=subject,
                tenant_id=tenant.id,
                roles=roles,
                email=email,
                ttl_seconds=7 * 24 * 3600,
            )
            principals[subject] = user

        session.commit()

        _run_workflow(session, settings, storage, tenant.id, principals["bob"])
        session.commit()

        print("\nDevelopment tokens (valid for 7 days):\n")
        for subject, _, roles, description in USERS:
            print(f"  {subject:<6} {','.join(roles):<12} {description}")
            print(f"         {tokens[subject]}\n")

        print("Try it:\n")
        print(
            f'  curl -H "Authorization: Bearer {tokens["bob"][:24]}..." '
            "http://localhost:8000/api/v1/reconciliations\n"
        )
        print("For the frontend, put a token in apps/web/.env.local as")
        print("NEXT_PUBLIC_DEV_TOKEN=<token>")
        return 0
    finally:
        session.close()
        engine.dispose()


def _run_workflow(session, settings, storage, tenant_id, user) -> None:  # type: ignore[no-untyped-def]
    """Ingest both sides and run one reconciliation."""
    from apps.api.app.dependencies import RequestContext
    from apps.api.app.infrastructure.audit_sink import DatabaseAuditLog
    from apps.api.app.services.ingestion import IngestionService
    from apps.api.app.services.reconciliation import (
        ReconciliationError,
        ReconciliationService,
    )
    from packages.ai.privacy import TenantAISettings
    from packages.ai.provider import NullProvider
    from packages.controls.permissions import Principal
    from packages.domain.enums import ActorType, AIPolicy, Role

    principal = Principal(
        tenant_id=tenant_id,
        actor_type=ActorType.USER,
        user_id=user.id,
        email=user.email,
        roles=frozenset({Role.CONTROLLER}),
    )
    context = RequestContext(
        session=session,
        principal=principal,
        settings=settings,
        storage=storage,
        audit=DatabaseAuditLog(session),
        ai=NullProvider(),
        ai_settings=TenantAISettings(policy=AIPolicy.AI_DISABLED),
        correlation_id=uuid4(),
    )

    ingestion = IngestionService(context)
    for filename, data, source_system in (
        ("bank_august.csv", BANK_CSV, "bank"),
        ("ledger_august.csv", LEDGER_CSV, "ledger"),
    ):
        row = ingestion.upload(filename=filename, data=data, mime_type="text/csv")
        if row.status == "INGESTED":
            print(f"  {filename}: already ingested")
            continue

        from apps.api.app.services.ingestion import _mapping_from_json

        mapping = _mapping_from_json(row.mapping or {})
        mapping.source_system = source_system
        ingestion.set_mapping(row.id, mapping)
        result = ingestion.ingest(row.id)
        print(
            f"  {filename}: {result.transactions_created} transactions, "
            f"quality {result.quality.level.value}"
        )

    service = ReconciliationService(context)
    definition = service.definitions.by_slug("bank-gl-august")
    if definition is None:
        definition = service.create(
            slug="bank-gl-august",
            name="Main EUR Bank vs GL - August 2026",
            template="bank_gl",
        )
        print(f"  created reconciliation {definition.slug}")

    try:
        outcome = service.start_run(definition.id, idempotency_key="seed-demo-august-2026")
    except ReconciliationError as exc:
        print(f"  run skipped: {exc}")
        return

    summary = outcome.summary
    print(
        f"\n  run {outcome.run.id}\n"
        f"    status              {outcome.run.status}\n"
        f"    bank balance        {summary.side_a_balance}\n"
        f"    ledger balance      {summary.side_b_balance}\n"
        f"    difference          {summary.difference}\n"
        f"    automatic matches   {summary.auto_matched_count}\n"
        f"    suggested           {summary.suggested_count}\n"
        f"    exceptions          {summary.exception_count}\n"
        f"    result hash         {outcome.result_hash[:16]}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
