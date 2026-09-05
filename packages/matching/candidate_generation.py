"""Stage 0 eligibility filters and Stage 4 candidate generation.

Spec sections 12 and 39. The single most important performance rule:

    Do not compare every transaction to every other transaction.

Candidates are found through blocking keys - exact identifiers first, then
amount/date/currency buckets. Only transactions sharing at least one key are
ever scored, so the cost is proportional to real collisions rather than to
``n * m``.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid5

from packages.domain.dates import DateField
from packages.domain.enums import MatchCardinality
from packages.domain.models.matching import CandidateMatch
from packages.domain.models.reconciliation import ReconciliationConfig
from packages.domain.models.transaction import CanonicalTransaction
from packages.domain.money import minor_units
from packages.ingestion.normalization import description_contains

__all__ = [
    "BlockingIndex",
    "CandidateGenerator",
    "EligibilityConfig",
    "candidate_namespace",
    "eligible",
]

# A fixed namespace so candidate IDs are reproducible across runs of the same
# snapshot. uuid4 here would break the "same snapshot, same result" invariant.
candidate_namespace = UUID("8c5e5a1e-3a1f-4e0b-9b7a-2f1d6c0e4a91")


@dataclass(frozen=True, slots=True)
class EligibilityConfig:
    """Stage 0 configuration (spec section 12)."""

    allow_fx: bool = False
    max_amount_difference: Decimal = Decimal("0.01")
    max_amount_difference_pct: Decimal | None = None
    max_date_window_days: int = 5
    date_field: DateField = DateField.TRANSACTION
    require_opposite_direction: bool = False
    require_same_account: bool = False
    incompatible_statuses: frozenset[str] = frozenset({"VOID", "CANCELLED", "FAILED"})

    @classmethod
    def from_reconciliation(cls, config: ReconciliationConfig) -> EligibilityConfig:
        tolerances = config.tolerances
        return cls(
            allow_fx=config.allow_fx,
            max_amount_difference=tolerances.max_amount_difference,
            max_amount_difference_pct=tolerances.amount_percentage,
            max_date_window_days=tolerances.date_days,
            date_field=tolerances.date_field,
        )


def eligible(
    a: CanonicalTransaction,
    b: CanonicalTransaction,
    cfg: EligibilityConfig,
) -> bool:
    """Whether two transactions could possibly be related.

    Cheap, total and side-effect free. Anything that fails here is never scored.
    """
    if not cfg.allow_fx and a.currency != b.currency:
        return False

    if a.status and a.status.upper() in cfg.incompatible_statuses:
        return False
    if b.status and b.status.upper() in cfg.incompatible_statuses:
        return False

    if cfg.require_same_account and a.source_account_id != b.source_account_id:
        return False

    difference = abs(a.amount - b.amount)
    within_absolute = difference <= cfg.max_amount_difference
    within_relative = False
    if cfg.max_amount_difference_pct is not None and b.amount != 0:
        within_relative = (difference / abs(b.amount)) <= cfg.max_amount_difference_pct
    if not (within_absolute or within_relative):
        return False

    if cfg.require_opposite_direction and (a.amount > 0) == (b.amount > 0):
        return False

    date_a = a.date_for(cfg.date_field) or a.best_date
    date_b = b.date_for(cfg.date_field) or b.best_date
    if date_a and date_b:
        days = abs((date_a - date_b).days)
        if days > cfg.max_date_window_days:
            return False

    return True


@dataclass(slots=True)
class BlockingIndex:
    """Inverted indexes over side B, used to look candidates up in O(1).

    Each index is a blocking key. Two transactions are only ever compared when
    they collide on at least one key, which is what keeps the engine away from
    the quadratic path.
    """

    by_external_id: dict[str, list[CanonicalTransaction]] = field(default_factory=lambda: defaultdict(list))
    by_settlement: dict[str, list[CanonicalTransaction]] = field(default_factory=lambda: defaultdict(list))
    by_invoice: dict[str, list[CanonicalTransaction]] = field(default_factory=lambda: defaultdict(list))
    by_reference: dict[str, list[CanonicalTransaction]] = field(default_factory=lambda: defaultdict(list))
    by_amount_bucket: dict[tuple[str, int], list[CanonicalTransaction]] = field(
        default_factory=lambda: defaultdict(list)
    )
    by_counterparty: dict[str, list[CanonicalTransaction]] = field(default_factory=lambda: defaultdict(list))
    by_check: dict[str, list[CanonicalTransaction]] = field(default_factory=lambda: defaultdict(list))
    all_transactions: list[CanonicalTransaction] = field(default_factory=list)

    @classmethod
    def build(cls, transactions: list[CanonicalTransaction]) -> BlockingIndex:
        index = cls()
        index.all_transactions = list(transactions)
        for tx in transactions:
            if tx.external_transaction_id:
                index.by_external_id[tx.external_transaction_id.upper()].append(tx)
            for settlement in (tx.settlement_id, tx.payout_id, tx.batch_id):
                if settlement:
                    index.by_settlement[settlement.upper()].append(tx)
            if tx.normalized_invoice_number:
                index.by_invoice[tx.normalized_invoice_number].append(tx)
            if tx.normalized_reference:
                index.by_reference[tx.normalized_reference].append(tx)
            if tx.normalized_counterparty:
                index.by_counterparty[tx.normalized_counterparty].append(tx)
            if tx.check_number:
                index.by_check[tx.check_number.upper()].append(tx)
            index.by_amount_bucket[_amount_key(tx)].append(tx)
            if tx.net_amount is not None and tx.net_amount != tx.amount:
                index.by_amount_bucket[
                    (tx.currency, minor_units(tx.net_amount, tx.currency))
                ].append(tx)
        return index

    def size(self) -> int:
        return len(self.all_transactions)


def _amount_key(tx: CanonicalTransaction) -> tuple[str, int]:
    return (tx.currency, minor_units(tx.amount, tx.currency))


@dataclass(slots=True)
class CandidateGenerator:
    """Generates 1:1 candidates for a run (Stage 4)."""

    config: ReconciliationConfig
    eligibility: EligibilityConfig
    max_candidates_per_transaction: int = 50

    @classmethod
    def for_config(cls, config: ReconciliationConfig) -> CandidateGenerator:
        return cls(
            config=config,
            eligibility=EligibilityConfig.from_reconciliation(config),
            max_candidates_per_transaction=config.max_candidates_per_transaction,
        )

    def generate(
        self,
        side_a: list[CanonicalTransaction],
        side_b: list[CanonicalTransaction],
        *,
        run_id: UUID,
        tenant_id: UUID,
    ) -> list[CandidateMatch]:
        """Generate deduplicated 1:1 candidates.

        Output order is deterministic: side A in input order, then candidates by
        transaction id.
        """
        index = BlockingIndex.build(side_b)
        candidates: list[CandidateMatch] = []
        seen: set[tuple[UUID, UUID]] = set()

        for a in side_a:
            partners = self.lookup(a, index)
            eligible_partners = sorted(
                (b for b in partners if eligible(a, b, self.eligibility)),
                key=lambda b: str(b.id),
            )
            for b in eligible_partners[: self.max_candidates_per_transaction]:
                key = (a.id, b.id)
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(
                    CandidateMatch(
                        id=uuid5(candidate_namespace, f"{run_id}:{a.id}:{b.id}"),
                        tenant_id=tenant_id,
                        run_id=run_id,
                        side_a_ids=(a.id,),
                        side_b_ids=(b.id,),
                        cardinality=MatchCardinality.ONE_TO_ONE,
                        generated_by="blocking_index_v1",
                    )
                )

        return candidates

    def lookup(
        self,
        a: CanonicalTransaction,
        index: BlockingIndex,
    ) -> list[CanonicalTransaction]:
        """Collect side-B transactions sharing any blocking key with ``a``."""
        found: dict[UUID, CanonicalTransaction] = {}

        def add(items: list[CanonicalTransaction]) -> None:
            for item in items:
                found[item.id] = item

        if a.external_transaction_id:
            add(index.by_external_id.get(a.external_transaction_id.upper(), []))
        for settlement in (a.settlement_id, a.payout_id, a.batch_id):
            if settlement:
                add(index.by_settlement.get(settlement.upper(), []))
        if a.normalized_invoice_number:
            add(index.by_invoice.get(a.normalized_invoice_number, []))
        if a.normalized_reference:
            add(index.by_reference.get(a.normalized_reference, []))
            # A reference may itself be an invoice number on the other side.
            add(index.by_invoice.get(a.normalized_reference, []))
        if a.check_number:
            add(index.by_check.get(a.check_number.upper(), []))

        add(self._amount_neighbourhood(a, index))

        # Identifiers buried in a bank narrative: only worth scanning when the
        # description exists and the opposite side is small enough that a scan
        # is cheaper than missing the match.
        if a.description and index.size() <= 5_000:
            for b in index.all_transactions:
                if b.id in found:
                    continue
                identifier = b.settlement_id or b.payout_id or b.external_transaction_id
                if identifier and description_contains(a.description, identifier):
                    found[b.id] = b

        return list(found.values())

    def _amount_neighbourhood(
        self,
        a: CanonicalTransaction,
        index: BlockingIndex,
    ) -> list[CanonicalTransaction]:
        """Side-B transactions whose amount is within the absolute tolerance.

        The bucket is keyed on integer minor units, so the scan is over the
        exact tolerance band and nothing else.
        """
        currency = a.currency
        centre = minor_units(a.amount, currency)
        span = minor_units(self.eligibility.max_amount_difference, currency)

        # A wide tolerance band would turn this into a scan. Beyond a few
        # hundred minor units it is cheaper and safer to require another key.
        if span > 500:
            span = 500

        result: list[CanonicalTransaction] = []
        for offset in range(-span, span + 1):
            result.extend(index.by_amount_bucket.get((currency, centre + offset), []))

        if self.eligibility.allow_fx:
            for other_currency, bucket_value in list(index.by_amount_bucket):
                if other_currency == currency:
                    continue
                del bucket_value  # keys only; FX pairs need explicit rate rules
        return result


def date_bucket(tx: CanonicalTransaction, field: DateField, days: int) -> tuple[str, ...]:
    """Coarse date buckets, used when an index must be persisted in SQL."""
    anchor = tx.date_for(field) or tx.best_date
    if anchor is None:
        return ()
    return tuple(
        (anchor + timedelta(days=offset)).isoformat()
        for offset in range(-days, days + 1)
    )
