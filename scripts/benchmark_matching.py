#!/usr/bin/env python
"""Matching benchmark (spec sections 52 and 99).

Measures what the spec asks for: ingestion and normalisation throughput,
candidates generated per row, exact-match and grouped-match runtime, and peak
memory. Synthetic rows are generated rather than shipped, so the 100k case in
pilot criterion 1 costs nothing in the repository.

    python scripts/benchmark_matching.py              # 10k and 100k
    python scripts/benchmark_matching.py --sizes 1000
    python scripts/benchmark_matching.py --sizes 1000000 --json out.json
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
import tracemalloc
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid5

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from packages.domain.dates import utc_now  # noqa: E402
from packages.domain.models.reconciliation import GroupingConfig  # noqa: E402
from packages.domain.models.transaction import (  # noqa: E402
    CanonicalTransaction,
    compute_checksum,
)
from packages.ingestion.normalization import (  # noqa: E402
    normalize_bank_description,
    normalize_invoice_number,
    normalize_name,
    normalize_reference,
)
from packages.matching.candidate_generation import (  # noqa: E402
    BlockingIndex,
    CandidateGenerator,
)
from packages.matching.engine import MatchingContext, MatchingEngine  # noqa: E402
from packages.matching.templates import bank_gl_template  # noqa: E402

NAMESPACE = UUID("b7e1c6d2-9a83-4f57-9e21-3d8f0a1b2c45")
TENANT = uuid5(NAMESPACE, "tenant")
CONNECTION_A = uuid5(NAMESPACE, "bank")
CONNECTION_B = uuid5(NAMESPACE, "ledger")

COUNTERPARTIES = [
    "ACME GMBH", "BETA LTD", "GAMMA BV", "DELTA SARL", "EPSILON AB",
    "ZETA OY", "ETA AS", "THETA SPA", "IOTA PLC", "KAPPA NV",
]


@dataclass
class BenchmarkRow:
    size: int
    generate_seconds: float
    normalize_rows_per_second: float
    index_seconds: float
    candidates_generated: int
    candidates_per_row: float
    engine_seconds: float
    transactions_per_second: float
    matches: int
    auto_matched: int
    suggested: int
    unmatched_a: int
    peak_memory_mb: float


def make_transaction(
    index: int,
    *,
    side: str,
    seed_date: date,
    rng: random.Random,
    matched: bool,
) -> CanonicalTransaction:
    """Build one synthetic transaction.

    ``matched`` rows share an invoice number and amount across sides, so the
    benchmark exercises the real matching path rather than the empty one.
    """
    amount = Decimal(rng.randrange(100, 5_000_000)) / 100
    invoice = f"INV-{index:08d}" if matched else None
    counterparty = COUNTERPARTIES[index % len(COUNTERPARTIES)]
    when = seed_date + timedelta(days=index % 28)

    if side == "bank":
        description = f"SEPA CT {invoice or 'MISC'} {counterparty}"
        connection = CONNECTION_A
        source_system = "bank"
    else:
        description = f"Customer payment {counterparty}"
        connection = CONNECTION_B
        source_system = "ledger"
        # A realistic ledger lags the bank by a day or two.
        when = when + timedelta(days=index % 2)

    payload = {"i": index, "side": side, "amount": str(amount)}
    return CanonicalTransaction(
        id=uuid5(NAMESPACE, f"{side}:{index}"),
        tenant_id=TENANT,
        source_system=source_system,
        source_connection_id=connection,
        source_record_id=f"{side.upper()}-{index}",
        transaction_date=when,
        amount=amount,
        currency="EUR",
        description=description,
        normalized_description=normalize_bank_description(description),
        reference=invoice,
        normalized_reference=normalize_reference(invoice),
        invoice_number=invoice,
        normalized_invoice_number=normalize_invoice_number(invoice),
        counterparty_name=counterparty,
        normalized_counterparty=normalize_name(counterparty),
        raw_payload=payload,
        source_checksum=compute_checksum(payload),
        imported_at=utc_now(),
    )


def generate(size: int, *, match_rate: float, seed: int = 7) -> tuple[list, list]:
    """Two sides of ``size`` rows each, with ``match_rate`` genuinely matchable."""
    rng = random.Random(seed)
    seed_date = date(2026, 8, 1)
    matched_count = int(size * match_rate)

    side_a = [
        make_transaction(i, side="bank", seed_date=seed_date, rng=random.Random(seed + i),
                         matched=i < matched_count)
        for i in range(size)
    ]
    side_b = [
        make_transaction(i, side="ledger", seed_date=seed_date,
                         rng=random.Random(seed + i), matched=i < matched_count)
        for i in range(size)
    ]
    # Give the matched pairs identical amounts so the exact rules can fire.
    for i in range(matched_count):
        side_b[i] = side_b[i].model_copy(update={"amount": side_a[i].amount})
    del rng
    return side_a, side_b


def run_size(size: int, *, match_rate: float, grouping: bool) -> BenchmarkRow:
    tracemalloc.start()

    started = time.perf_counter()
    side_a, side_b = generate(size, match_rate=match_rate)
    generate_seconds = time.perf_counter() - started
    normalize_rate = (size * 2) / generate_seconds if generate_seconds else 0.0

    config, rule_set = bank_gl_template()
    config = config.model_copy(
        update={
            "grouping": GroupingConfig(
                enabled=grouping, max_group_size=4, max_candidate_pool=40
            )
        }
    )

    started = time.perf_counter()
    index = BlockingIndex.build(side_b)
    index_seconds = time.perf_counter() - started

    generator = CandidateGenerator.for_config(config)
    started = time.perf_counter()
    candidates = generator.generate(
        side_a[: min(size, 5_000)],
        side_b,
        run_id=uuid5(NAMESPACE, "run"),
        tenant_id=TENANT,
    )
    candidate_seconds = time.perf_counter() - started
    sampled = min(size, 5_000)
    candidates_per_row = len(candidates) / sampled if sampled else 0.0
    del candidate_seconds

    engine = MatchingEngine(config, rule_set)
    context = MatchingContext(TENANT, uuid5(NAMESPACE, "recon"), uuid5(NAMESPACE, "run"), "v1")

    started = time.perf_counter()
    result = engine.run(side_a, side_b, context)
    engine_seconds = time.perf_counter() - started

    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    return BenchmarkRow(
        size=size,
        generate_seconds=round(generate_seconds, 3),
        normalize_rows_per_second=round(normalize_rate, 1),
        index_seconds=round(index_seconds, 3),
        candidates_generated=len(candidates),
        candidates_per_row=round(candidates_per_row, 3),
        engine_seconds=round(engine_seconds, 3),
        transactions_per_second=round((size * 2) / engine_seconds, 1) if engine_seconds else 0.0,
        matches=len(result.matches),
        auto_matched=len(result.auto_matched),
        suggested=len(result.suggested),
        unmatched_a=len(result.unmatched_a),
        peak_memory_mb=round(peak / (1024 * 1024), 1),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sizes",
        type=int,
        nargs="+",
        default=[10_000, 100_000],
        help="rows per side (spec section 52 benchmarks 10k, 100k, 1m)",
    )
    parser.add_argument("--match-rate", type=float, default=0.8)
    parser.add_argument("--grouping", action="store_true")
    parser.add_argument("--json", type=Path, help="write results to a JSON file")
    args = parser.parse_args()

    print("Matching benchmark - spec sections 52 and 99")
    print(f"match rate {args.match_rate:.0%}, grouping {'on' if args.grouping else 'off'}\n")

    header = (
        f"{'rows/side':>10} {'engine s':>9} {'tx/s':>10} {'cand/row':>9} "
        f"{'matched':>8} {'auto':>7} {'peak MB':>8}"
    )
    print(header)
    print("-" * len(header))

    results: list[BenchmarkRow] = []
    for size in args.sizes:
        row = run_size(size, match_rate=args.match_rate, grouping=args.grouping)
        results.append(row)
        print(
            f"{row.size:>10,} {row.engine_seconds:>9.3f} "
            f"{row.transactions_per_second:>10,.0f} {row.candidates_per_row:>9.2f} "
            f"{row.matches:>8,} {row.auto_matched:>7,} {row.peak_memory_mb:>8.1f}"
        )

    print(
        "\nCandidates per row is the number that matters: it must stay flat as "
        "the dataset grows.\nA rising value means blocking has stopped working "
        "and the engine is drifting toward O(n*m)."
    )

    if args.json:
        args.json.write_text(
            json.dumps([asdict(row) for row in results], indent=2), encoding="utf-8"
        )
        print(f"\nwrote {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
