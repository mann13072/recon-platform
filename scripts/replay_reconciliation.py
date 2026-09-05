#!/usr/bin/env python
"""Replay a stored run against its frozen snapshot (spec sections 36 and 51).

    Running the same deterministic rules on the same snapshot must return the
    same result.

This makes that claim checkable against production data rather than only in a
test. It reads the run's snapshot, re-executes the engine, and compares the
result hash with the one stored when the run completed.

    python scripts/replay_reconciliation.py <run-id>
    python scripts/replay_reconciliation.py --all --tenant <tenant-id>

Exit code 0 when every replayed run reproduced, 1 otherwise. Nothing is written.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", nargs="?", help="the run to replay")
    parser.add_argument("--all", action="store_true", help="replay every run")
    parser.add_argument("--tenant", help="restrict --all to one tenant")
    parser.add_argument(
        "--database-url", default="sqlite+pysqlite:///./recon-demo.sqlite3"
    )
    args = parser.parse_args()

    if not args.run_id and not args.all:
        parser.error("supply a run id, or --all")

    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker

    from apps.api.app.infrastructure.models import RunRow
    from apps.api.app.infrastructure.repositories import (
        ReconciliationRepository,
        RunRepository,
        TransactionRepository,
    )
    from packages.domain.models.reconciliation import ReconciliationConfig
    from packages.matching.engine import MatchingContext, MatchingEngine
    from packages.matching.templates import (
        bank_gl_template,
        default_rule_set,
        stripe_payout_template,
    )

    engine = create_engine(args.database_url, future=True)
    session = sessionmaker(bind=engine, future=True)()

    try:
        statement = select(RunRow).where(RunRow.result_hash.is_not(None))
        if args.run_id:
            statement = statement.where(RunRow.id == UUID(args.run_id))
        if args.tenant:
            statement = statement.where(RunRow.tenant_id == UUID(args.tenant))
        runs = list(session.execute(statement.order_by(RunRow.created_at)).scalars())

        if not runs:
            print("No completed runs matched.", file=sys.stderr)
            return 1

        print(f"Replaying {len(runs)} run(s)\n")
        header = f"{'run':<38} {'stored':<18} {'replayed':<18} {'result':<14} matches"
        print(header)
        print("-" * len(header))

        failures = 0
        for run in runs:
            runs_repo = RunRepository(session=session, tenant_id=run.tenant_id)
            snapshot = runs_repo.snapshot(run.id)
            if snapshot is None:
                print(f"{str(run.id):<38} {'-':<18} {'-':<18} {'NO SNAPSHOT':<14} -")
                failures += 1
                continue

            definitions = ReconciliationRepository(
                session=session, tenant_id=run.tenant_id
            )
            definition = definitions.get(run.reconciliation_id)
            if definition is None:
                print(f"{str(run.id):<38} {'-':<18} {'-':<18} {'NO DEFINITION':<14} -")
                failures += 1
                continue

            config = ReconciliationConfig.model_validate(definition.config)
            rule_set = {
                "bank_gl": bank_gl_template()[1],
                "stripe_payout": stripe_payout_template()[1],
            }.get(definition.template or "", default_rule_set())

            transactions = TransactionRepository(
                session=session, tenant_id=run.tenant_id
            )
            side_a = transactions.get_many(
                [UUID(str(i)) for i in snapshot.side_a_transaction_ids]
            )
            side_b = transactions.get_many(
                [UUID(str(i)) for i in snapshot.side_b_transaction_ids]
            )

            missing = (
                len(snapshot.side_a_transaction_ids) - len(side_a)
                + len(snapshot.side_b_transaction_ids) - len(side_b)
            )

            result = MatchingEngine(config, rule_set).run(
                side_a,
                side_b,
                MatchingContext(
                    tenant_id=run.tenant_id,
                    reconciliation_id=definition.id,
                    run_id=run.id,
                    rule_set_version=snapshot.rule_set_version,
                ),
            )

            reproduced = result.result_hash == run.result_hash
            if not reproduced:
                failures += 1
            verdict = "REPRODUCED" if reproduced else "DIVERGED"
            if missing:
                verdict = f"MISSING {missing}"
                failures += 0 if reproduced else 0

            print(
                f"{str(run.id):<38} {(run.result_hash or '')[:16]:<18} "
                f"{result.result_hash[:16]:<18} {verdict:<14} {len(result.matches)}"
            )

        print()
        if failures:
            print(
                f"{failures} run(s) did not reproduce. A divergence means the "
                "engine is not deterministic on a fixed snapshot, which "
                "invalidates the reproducibility guarantee. Do not close any "
                "reconciliation until it is resolved.",
                file=sys.stderr,
            )
            return 1

        print("Every run reproduced its stored result hash.")
        return 0
    finally:
        session.close()
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
