"""Match review: approve, reject, manual match, unmatch (spec sections 37, 57).

Every write here passes three gates before it touches a row:

    1. the period is open,
    2. the run is not closed,
    3. segregation of duties permits *this* principal to do *this* thing.

Then it takes the optimistic lock, and only then does it write.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID, uuid4

from apps.api.app.dependencies import RequestContext
from apps.api.app.infrastructure.models import (
    MatchGroupRow,
    PeriodLockRow,
    ReconciliationRow,
    RunRow,
)
from apps.api.app.infrastructure.repositories import MatchRepository, RunRepository, TransactionRepository
from packages.controls.materiality import MaterialityPolicy
from packages.controls.period_lock import PeriodLock, PeriodLockRegistry
from packages.controls.permissions import Permission
from packages.controls.segregation_of_duties import (
    ApprovalSubject,
    SoDViolation,
    assert_can_approve_match,
    assert_can_modify_run,
)
from packages.domain.dates import utc_now
from packages.domain.enums import (
    ActorType,
    AuditAction,
    DecisionOutcome,
    MatchCardinality,
    MatchGroupStatus,
    ReconciliationStatus,
    Side,
)
from packages.domain.models.matching import MatchGroup, MatchGroupMember
from packages.domain.models.reconciliation import ReconciliationConfig
from packages.matching.engine import assert_invariants
from packages.matching.scoring import Scorer, extract_features

__all__ = ["ReviewError", "ReviewService"]


class ReviewError(ValueError):
    def __init__(self, message: str, code: str = "review_failed") -> None:
        super().__init__(message)
        self.code = code


@dataclass(slots=True)
class ReviewService:
    context: RequestContext

    @property
    def matches(self) -> MatchRepository:
        return MatchRepository(session=self.context.session, tenant_id=self.context.tenant_id)

    @property
    def runs(self) -> RunRepository:
        return RunRepository(session=self.context.session, tenant_id=self.context.tenant_id)

    @property
    def transactions(self) -> TransactionRepository:
        return TransactionRepository(
            session=self.context.session, tenant_id=self.context.tenant_id
        )

    # -- gates -------------------------------------------------------------
    def _guard(self, run: RunRow) -> tuple[ReconciliationRow, ReconciliationConfig]:
        assert_can_modify_run(
            self.context.principal, ReconciliationStatus(run.status)
        )
        definition = self.context.session.get(ReconciliationRow, run.reconciliation_id)
        if definition is None or definition.tenant_id != self.context.tenant_id:
            raise ReviewError("The reconciliation no longer exists.", "not_found")

        self._period_registry(definition.entity).assert_range_open(
            definition.entity, run.period_start, run.period_end
        )
        return definition, ReconciliationConfig.model_validate(definition.config)

    def _period_registry(self, entity: str) -> PeriodLockRegistry:
        rows = (
            self.context.session.query(PeriodLockRow)
            .filter(
                PeriodLockRow.tenant_id == self.context.tenant_id,
                PeriodLockRow.entity == entity,
                PeriodLockRow.released_at.is_(None),
            )
            .all()
        )
        return PeriodLockRegistry(
            locks=[
                PeriodLock(
                    tenant_id=row.tenant_id,
                    entity=row.entity,
                    period_start=row.period_start,
                    period_end=row.period_end,
                    locked_by=row.locked_by,
                    locked_at=row.locked_at,
                    reason=row.reason,
                )
                for row in rows
            ]
        )

    def _require_row(self, match_id: UUID) -> tuple[MatchGroupRow, RunRow]:
        row = self.matches.get_row(match_id)
        if row is None:
            raise ReviewError("No such match.", "not_found")
        run = self.runs.get(row.run_id)
        if run is None:
            raise ReviewError("The run for this match no longer exists.", "not_found")
        return row, run

    # -- actions -----------------------------------------------------------
    def approve(
        self, match_id: UUID, *, expected_version: int, reason: str | None = None
    ) -> MatchGroup:
        row, run = self._require_row(match_id)
        _, config = self._guard(run)

        if row.status in {MatchGroupStatus.APPROVED.value, MatchGroupStatus.REJECTED.value}:
            raise ReviewError(
                f"This match is already {row.status.lower()}.", "already_decided"
            )

        assert_can_approve_match(
            self.context.principal,
            ApprovalSubject(
                subject_type="match_group",
                subject_id=row.id,
                created_by=row.created_by,
                created_by_actor_type=ActorType(row.created_by_actor_type),
                amount=row.total_amount,
                currency=row.currency,
                is_manual=row.engine_stage == "manual",
                is_override=bool(row.override_reason),
            ),
            materiality_threshold=config.controls.materiality_threshold,
            maker_checker_required=config.controls.maker_checker_required,
        )

        before = {"status": row.status}
        updated = self.matches.update(
            match_id,
            expected_version=expected_version,
            status=MatchGroupStatus.APPROVED.value,
            approved_by=self.context.principal.user_id,
            approved_at=utc_now(),
            override_reason=reason or row.override_reason,
        )

        self.context.audit.record(
            self.context.audit_context(),
            AuditAction.MATCH_APPROVED,
            "MATCH_GROUP",
            match_id,
            before=before,
            after={"status": updated.status},
            reason=reason,
            metadata={
                "confidence": f"{updated.confidence:.6f}",
                "rule": f"{updated.rule_id}:{updated.rule_version}",
                "amount": str(updated.total_amount),
            },
        )
        return self.matches.get(match_id)  # type: ignore[return-value]

    def reject(
        self, match_id: UUID, *, expected_version: int, reason: str
    ) -> MatchGroup:
        """Reject a proposed match. A reason is mandatory (spec section 109)."""
        if not reason or not reason.strip():
            raise ReviewError(
                "Rejecting a match requires a written reason.", "reason_required"
            )
        row, run = self._require_row(match_id)
        self._guard(run)
        self.context.principal.require(Permission.REJECT_MATCH)

        before = {"status": row.status}
        updated = self.matches.update(
            match_id,
            expected_version=expected_version,
            status=MatchGroupStatus.REJECTED.value,
            rejected_by=self.context.principal.user_id,
            rejected_at=utc_now(),
            override_reason=reason,
        )

        self.context.audit.record(
            self.context.audit_context(),
            AuditAction.MATCH_REJECTED,
            "MATCH_GROUP",
            match_id,
            before=before,
            after={"status": updated.status},
            reason=reason,
        )
        return self.matches.get(match_id)  # type: ignore[return-value]

    def unmatch(self, match_id: UUID, *, expected_version: int, reason: str) -> MatchGroup:
        """Undo an approved match.

        The group is not deleted - nothing in this platform deletes financial
        history. It moves to UNMATCHED, which releases its transactions because
        only active statuses hold a claim.
        """
        if not reason.strip():
            raise ReviewError(
                "Unmatching requires a written reason.", "reason_required"
            )
        row, run = self._require_row(match_id)
        self._guard(run)
        self.context.principal.require(Permission.UNMATCH)

        before = {"status": row.status}
        self.matches.update(
            match_id,
            expected_version=expected_version,
            status=MatchGroupStatus.UNMATCHED.value,
            override_reason=reason,
        )
        self.context.audit.record(
            self.context.audit_context(),
            AuditAction.MATCH_UNMATCHED,
            "MATCH_GROUP",
            match_id,
            before=before,
            after={"status": MatchGroupStatus.UNMATCHED.value},
            reason=reason,
        )
        return self.matches.get(match_id)  # type: ignore[return-value]

    def create_manual(
        self,
        *,
        run_id: UUID,
        side_a_ids: list[UUID],
        side_b_ids: list[UUID],
        reason: str,
    ) -> MatchGroup:
        """Create a match by hand.

        A manual match is always PROPOSED, never approved on creation: the
        person who makes it is not the person who confirms it (spec section 34).
        """
        if not reason.strip():
            raise ReviewError(
                "A manual match requires a written reason.", "reason_required"
            )
        if not side_a_ids or not side_b_ids:
            raise ReviewError(
                "A manual match needs at least one transaction on each side.",
                "invalid_members",
            )

        run = self.runs.get(run_id)
        if run is None:
            raise ReviewError("No such run.", "not_found")
        definition, config = self._guard(run)
        self.context.principal.require(Permission.CREATE_MANUAL_MATCH)

        side_a = self.transactions.get_many(side_a_ids)
        side_b = self.transactions.get_many(side_b_ids)
        if len(side_a) != len(side_a_ids) or len(side_b) != len(side_b_ids):
            raise ReviewError(
                "One or more transactions do not exist in this tenant.", "not_found"
            )

        currencies = {t.currency for t in (*side_a, *side_b)}
        if len(currencies) > 1 and not config.allow_fx:
            raise ReviewError(
                "The selected transactions are in different currencies and this "
                "reconciliation does not have FX enabled.",
                "currency_mismatch",
            )

        # Exclusivity across the whole tenant, not only within this run.
        for transaction in (*side_a, *side_b):
            claimed = self.matches.active_group_for_transaction(transaction.id)
            if claimed is not None:
                raise ReviewError(
                    f"Transaction {transaction.source_record_id} already belongs to "
                    f"an active match ({claimed.id}). Unmatch it first.",
                    "already_matched",
                )

        cardinality = _cardinality(len(side_a), len(side_b))
        group = MatchGroup(
            id=uuid4(),
            tenant_id=self.context.tenant_id,
            run_id=run.id,
            reconciliation_id=definition.id,
            members=tuple(
                [
                    MatchGroupMember(
                        transaction_id=t.id, side=Side.A, allocated_amount=t.amount
                    )
                    for t in side_a
                ]
                + [
                    MatchGroupMember(
                        transaction_id=t.id, side=Side.B, allocated_amount=t.amount
                    )
                    for t in side_b
                ]
            ),
            cardinality=cardinality,
            status=MatchGroupStatus.PROPOSED,
            decision=DecisionOutcome.SUGGEST,
            confidence=0.0,
            score=0.0,
            currency=side_a[0].currency,
            rule_id="manual",
            rule_version="v1",
            engine_stage="manual",
            created_by_actor_type=ActorType.USER.value,
            override_reason=reason,
            metadata={"created_by": str(self.context.principal.user_id)},
        )

        # A manual pairing still records its evidence, so a reviewer sees what
        # the machine would have said about it.
        if len(side_a) == 1 and len(side_b) == 1:
            features = extract_features(side_a[0], side_b[0])
            scorer = Scorer(config=config)
            group = group.model_copy(
                update={
                    "score": scorer._score_with_features(features, side_a[0], side_b[0]),
                }
            )

        by_id = {t.id: t for t in (*side_a, *side_b)}
        assert_invariants([group], by_id)

        row = self.matches.bulk_insert([group])
        del row
        self.matches.update(
            group.id, expected_version=1, created_by=self.context.principal.user_id
        )

        self.context.audit.record(
            self.context.audit_context(),
            AuditAction.MATCH_CREATED,
            "MATCH_GROUP",
            group.id,
            after={"status": group.status.value, "cardinality": cardinality.value},
            reason=reason,
            metadata={
                "side_a": ",".join(str(i) for i in side_a_ids),
                "side_b": ",".join(str(i) for i in side_b_ids),
            },
        )
        return self.matches.get(group.id)  # type: ignore[return-value]

    def materiality_policy(self, config: ReconciliationConfig) -> MaterialityPolicy:
        return MaterialityPolicy(
            materiality_threshold=config.controls.materiality_threshold,
            manual_approval_above_materiality=config.controls.manual_approval_above_materiality,
            always_review_types=frozenset(config.controls.always_review_types),
            approval_limit=(
                Decimal(self.context.principal.approval_limit)
                if self.context.principal.approval_limit is not None
                else None
            ),
        )


def _cardinality(a_count: int, b_count: int) -> MatchCardinality:
    if a_count == 1 and b_count == 1:
        return MatchCardinality.ONE_TO_ONE
    if a_count == 1:
        return MatchCardinality.ONE_TO_MANY
    if b_count == 1:
        return MatchCardinality.MANY_TO_ONE
    return MatchCardinality.MANY_TO_MANY


__all__ += ["SoDViolation"]
