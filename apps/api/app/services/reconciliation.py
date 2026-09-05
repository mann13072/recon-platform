"""Reconciliation definitions and runs (spec sections 36, 41, 58, 90).

The important structural choice: a run reads its transactions from its own
**snapshot**, not from the live tables. The snapshot is written when the run
starts and frozen. A later import therefore cannot change what an old run
reconciled, and re-running an old snapshot reproduces its result exactly.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from apps.api.app.dependencies import RequestContext
from apps.api.app.infrastructure.models import (
    ReconciliationRow,
    RunRow,
    RunSnapshotRow,
)
from apps.api.app.infrastructure.repositories import (
    ExceptionRepository,
    MatchRepository,
    ReconciliationRepository,
    RunRepository,
    TransactionRepository,
)
from packages.audit.logger import AuditContext
from packages.controls.materiality import MaterialityPolicy
from packages.domain.dates import utc_now
from packages.domain.enums import (
    ActorType,
    AuditAction,
    DecisionOutcome,
    MatchGroupStatus,
    ReconciliationStatus,
)
from packages.domain.models.exceptions import ExceptionRecord
from packages.domain.models.reconciliation import (
    ReconciliationConfig,
    RunSnapshot,
    RunSummary,
)
from packages.domain.models.transaction import CanonicalTransaction
from packages.exceptions.classifier import (
    ExceptionClassifier,
    classify_pair,
    classify_unmatched,
)
from packages.matching.engine import MatchingContext, MatchingEngine
from packages.matching.rules import RuleSet, load_rule_set
from packages.matching.templates import (
    bank_gl_template,
    default_rule_set,
    stripe_payout_template,
)

__all__ = ["ReconciliationError", "ReconciliationService", "RunOutcome"]

TEMPLATES = {"bank_gl": bank_gl_template, "stripe_payout": stripe_payout_template}


class ReconciliationError(ValueError):
    def __init__(self, message: str, code: str = "reconciliation_failed") -> None:
        super().__init__(message)
        self.code = code


@dataclass(slots=True)
class RunOutcome:
    run: RunRow
    summary: RunSummary
    matches_created: int
    exceptions_created: int
    result_hash: str
    replayed: bool = False


@dataclass(slots=True)
class ReconciliationService:
    context: RequestContext

    # -- repositories ------------------------------------------------------
    @property
    def definitions(self) -> ReconciliationRepository:
        return ReconciliationRepository(
            session=self.context.session, tenant_id=self.context.tenant_id
        )

    @property
    def runs(self) -> RunRepository:
        return RunRepository(session=self.context.session, tenant_id=self.context.tenant_id)

    @property
    def transactions(self) -> TransactionRepository:
        return TransactionRepository(session=self.context.session, tenant_id=self.context.tenant_id)

    @property
    def matches(self) -> MatchRepository:
        return MatchRepository(session=self.context.session, tenant_id=self.context.tenant_id)

    @property
    def exceptions(self) -> ExceptionRepository:
        return ExceptionRepository(session=self.context.session, tenant_id=self.context.tenant_id)

    # -- definitions -------------------------------------------------------
    def create(
        self,
        *,
        slug: str,
        name: str,
        template: str | None = None,
        config: dict[str, Any] | None = None,
        side_a_connection_id: UUID | None = None,
        side_b_connection_id: UUID | None = None,
        entity: str = "default",
    ) -> ReconciliationRow:
        if self.definitions.by_slug(slug) is not None:
            raise ReconciliationError(
                f"A reconciliation with the slug '{slug}' already exists.", "duplicate"
            )

        if template:
            builder = TEMPLATES.get(template)
            if builder is None:
                raise ReconciliationError(
                    f"Unknown template '{template}'. Available: " + ", ".join(sorted(TEMPLATES)),
                    "unknown_template",
                )
            built, _ = builder(name=name)
            resolved = built.model_dump(mode="json")
            if config:
                resolved.update(config)
        elif config:
            resolved = ReconciliationConfig.from_yaml_dict(config).model_dump(mode="json")
        else:
            raise ReconciliationError(
                "Supply either a template or an explicit configuration.", "config_required"
            )

        row = self.definitions.add(
            ReconciliationRow(
                id=uuid4(),
                tenant_id=self.context.tenant_id,
                slug=slug,
                name=name,
                template=template,
                config=resolved,
                config_version=str(resolved.get("config_version", "v1")),
                config_hash=_hash(resolved),
                side_a_connection_id=side_a_connection_id,
                side_b_connection_id=side_b_connection_id,
                entity=entity,
                created_by=self.context.principal.user_id,
            )
        )

        self.context.audit.record(
            self.context.audit_context(),
            AuditAction.RECONCILIATION_CREATED,
            "RECONCILIATION",
            row.id,
            after=resolved,
            metadata={"template": template or "custom", "slug": slug},
        )
        return row

    def config_for(self, row: ReconciliationRow) -> ReconciliationConfig:
        return ReconciliationConfig.model_validate(row.config)

    def rule_set_for(self, row: ReconciliationRow) -> RuleSet:
        """The rule set a definition runs under.

        A tenant-specific stored rule set wins; otherwise the template's rules;
        otherwise everything shipped.
        """
        from apps.api.app.infrastructure.models import RuleSetRow

        if row.rule_set_id is not None:
            stored = self.context.session.get(RuleSetRow, row.rule_set_id)
            if stored is not None and stored.tenant_id == self.context.tenant_id:
                return load_rule_set(stored.definition)

        if row.template == "bank_gl":
            return bank_gl_template()[1]
        if row.template == "stripe_payout":
            return stripe_payout_template()[1]
        return default_rule_set()

    # -- runs --------------------------------------------------------------
    def start_run(
        self,
        reconciliation_id: UUID,
        *,
        period_start: date | None = None,
        period_end: date | None = None,
        idempotency_key: str | None = None,
    ) -> RunOutcome:
        """Snapshot, match, persist. The whole Stage 1-8 pipeline."""
        definition = self.definitions.get(reconciliation_id)
        if definition is None:
            raise ReconciliationError("No such reconciliation.", "not_found")

        if idempotency_key:
            existing = self.runs.by_idempotency_key(idempotency_key)
            if existing is not None:
                # Spec section 63: a replayed request returns the original run.
                return RunOutcome(
                    run=existing,
                    summary=RunSummary.model_validate(existing.summary or {}),
                    matches_created=0,
                    exceptions_created=0,
                    result_hash=existing.result_hash or "",
                    replayed=True,
                )

        config = self.config_for(definition)
        rule_set = self.rule_set_for(definition)

        side_a, side_b = self._select_transactions(definition, config, period_start, period_end)
        if not side_a and not side_b:
            raise ReconciliationError(
                "There are no transactions in the selected period for either side.",
                "no_data",
            )

        run = self.runs.add(
            RunRow(
                id=uuid4(),
                tenant_id=self.context.tenant_id,
                reconciliation_id=definition.id,
                status=ReconciliationStatus.RUNNING.value,
                period_start=period_start,
                period_end=period_end,
                started_at=utc_now(),
                initiated_by=self.context.principal.user_id,
                idempotency_key=idempotency_key,
            )
        )

        snapshot = self._write_snapshot(
            run, definition, config, rule_set, side_a, side_b, period_start, period_end
        )

        self.context.audit.record(
            self.context.audit_context(),
            AuditAction.RECONCILIATION_RUN_STARTED,
            "RUN",
            run.id,
            after={"snapshot_hash": snapshot.snapshot_hash},
            metadata={
                "side_a_count": len(side_a),
                "side_b_count": len(side_b),
                "rule_set_version": rule_set.version,
            },
        )

        return self._execute(run, definition, config, rule_set, side_a, side_b)

    def rerun(self, run_id: UUID) -> RunOutcome:
        """Re-execute an existing run against its frozen snapshot.

        This is what makes 'running the same deterministic rules on the same
        snapshot returns the same result' checkable in production and not only
        in a test (spec sections 36 and 51).
        """
        run = self.runs.get(run_id)
        if run is None:
            raise ReconciliationError("No such run.", "not_found")
        snapshot_row = self.runs.snapshot(run_id)
        if snapshot_row is None:
            raise ReconciliationError("This run has no snapshot to replay.", "no_snapshot")

        definition = self.definitions.get(run.reconciliation_id)
        if definition is None:
            raise ReconciliationError("The reconciliation no longer exists.", "not_found")

        config = self.config_for(definition)
        rule_set = self.rule_set_for(definition)
        side_a = self.transactions.get_many(
            [UUID(str(i)) for i in snapshot_row.side_a_transaction_ids]
        )
        side_b = self.transactions.get_many(
            [UUID(str(i)) for i in snapshot_row.side_b_transaction_ids]
        )

        engine = MatchingEngine(config, rule_set)
        result = engine.run(
            side_a,
            side_b,
            MatchingContext(
                tenant_id=self.context.tenant_id,
                reconciliation_id=definition.id,
                run_id=run.id,
                rule_set_version=rule_set.version,
            ),
        )
        return RunOutcome(
            run=run,
            summary=RunSummary.model_validate(run.summary or {}),
            matches_created=len(result.matches),
            exceptions_created=0,
            result_hash=result.result_hash,
            replayed=True,
        )

    # -- internals ---------------------------------------------------------
    def _select_transactions(
        self,
        definition: ReconciliationRow,
        config: ReconciliationConfig,
        period_start: date | None,
        period_end: date | None,
    ) -> tuple[list[CanonicalTransaction], list[CanonicalTransaction]]:
        """Choose each side's population, by connection when configured.

        Selection is by connection first because that is unambiguous. Falling
        back to source system keeps a file-only tenant working before any
        connector is configured.
        """
        common: dict[str, Any] = {
            "date_from": period_start,
            "date_to": period_end,
            "limit": 100_000,
        }

        if definition.side_a_connection_id:
            side_a = self.transactions.list(connection_id=definition.side_a_connection_id, **common)
        else:
            side_a = self.transactions.list(source_system=config.side_a.type, **common)

        if definition.side_b_connection_id:
            side_b = self.transactions.list(connection_id=definition.side_b_connection_id, **common)
        else:
            side_b = self.transactions.list(source_system=config.side_b.type, **common)

        return side_a, side_b

    def _write_snapshot(
        self,
        run: RunRow,
        definition: ReconciliationRow,
        config: ReconciliationConfig,
        rule_set: RuleSet,
        side_a: list[CanonicalTransaction],
        side_b: list[CanonicalTransaction],
        period_start: date | None,
        period_end: date | None,
    ) -> RunSnapshot:
        checksums = {str(t.id): t.source_checksum for t in (*side_a, *side_b)}
        snapshot = RunSnapshot(
            run_id=run.id,
            tenant_id=self.context.tenant_id,
            reconciliation_id=definition.id,
            side_a_transaction_ids=tuple(t.id for t in side_a),
            side_b_transaction_ids=tuple(t.id for t in side_b),
            source_checksums=checksums,
            rule_set_version=rule_set.version,
            rule_versions=rule_set.versions(),
            config_version=definition.config_version,
            config_hash=definition.config_hash,
            period_start=period_start,
            period_end=period_end,
            initiated_by=self.context.principal.user_id,
            created_at=utc_now(),
        )
        digest = _hash(
            {
                "a": sorted(str(i) for i in snapshot.side_a_transaction_ids),
                "b": sorted(str(i) for i in snapshot.side_b_transaction_ids),
                "checksums": checksums,
                "rules": snapshot.rule_versions,
                "config": definition.config_hash,
            }
        )
        snapshot = snapshot.model_copy(update={"snapshot_hash": digest})

        self.runs.add_snapshot(
            RunSnapshotRow(
                id=uuid4(),
                tenant_id=self.context.tenant_id,
                run_id=run.id,
                side_a_transaction_ids=[str(i) for i in snapshot.side_a_transaction_ids],
                side_b_transaction_ids=[str(i) for i in snapshot.side_b_transaction_ids],
                source_checksums=checksums,
                rule_set_version=snapshot.rule_set_version,
                rule_versions=snapshot.rule_versions,
                config_version=snapshot.config_version,
                config_hash=snapshot.config_hash,
                snapshot_hash=digest,
            )
        )
        return snapshot

    def _execute(
        self,
        run: RunRow,
        definition: ReconciliationRow,
        config: ReconciliationConfig,
        rule_set: RuleSet,
        side_a: list[CanonicalTransaction],
        side_b: list[CanonicalTransaction],
    ) -> RunOutcome:
        engine = MatchingEngine(config, rule_set)
        matching_context = MatchingContext(
            tenant_id=self.context.tenant_id,
            reconciliation_id=definition.id,
            run_id=run.id,
            rule_set_version=rule_set.version,
        )

        try:
            result = engine.run(side_a, side_b, matching_context)
        except AssertionError as exc:
            # An invariant violation means the engine produced a financially
            # incoherent result. Fail the run rather than persist it.
            self.runs.update_status(
                run.id,
                expected_version=run.version,
                status=ReconciliationStatus.FAILED.value,
                finished_at=utc_now(),
                failure_reason=str(exc),
            )
            self.context.audit.record(
                self.context.audit_context(ActorType.MATCH_ENGINE),
                AuditAction.RECONCILIATION_RUN_FAILED,
                "RUN",
                run.id,
                reason=str(exc),
            )
            raise ReconciliationError(
                f"The run failed an accounting integrity check and was not saved: {exc}",
                "invariant_violation",
            ) from exc

        self.matches.bulk_insert(result.matches)
        exceptions = self._build_exceptions(run, config, result, side_a, side_b)
        self.exceptions.bulk_insert(exceptions)

        summary = self._build_summary(run, definition, config, result, exceptions, side_a, side_b)

        needs_review = bool(result.suggested) or bool(exceptions)
        status = (
            ReconciliationStatus.REVIEW_REQUIRED
            if needs_review
            else ReconciliationStatus.READY_TO_CLOSE
        )

        updated = self.runs.update_status(
            run.id,
            expected_version=run.version,
            status=status.value,
            finished_at=utc_now(),
            result_hash=result.result_hash,
            summary=summary.model_dump(mode="json"),
        )

        engine_context = AuditContext(
            tenant_id=self.context.tenant_id,
            actor_type=ActorType.MATCH_ENGINE,
            actor_label="matching-engine",
            correlation_id=self.context.correlation_id,
        )
        for group in result.matches:
            self.context.audit.record(
                engine_context,
                (
                    AuditAction.MATCH_AUTO_APPROVED
                    if group.decision is DecisionOutcome.AUTO_MATCH
                    else AuditAction.MATCH_SUGGESTED
                ),
                "MATCH_GROUP",
                group.id,
                after={"status": group.status.value, "score": group.score},
                metadata={
                    "rule": f"{group.rule_id}:{group.rule_version}",
                    "stage": group.engine_stage,
                    "confidence": f"{group.confidence:.6f}",
                    "competing_candidates": group.competing_candidate_count,
                },
            )
        for record in exceptions:
            self.context.audit.record(
                engine_context,
                AuditAction.EXCEPTION_CREATED,
                "EXCEPTION",
                record.id,
                after={"category": record.category.value, "severity": record.severity.value},
            )

        self.context.audit.record(
            self.context.audit_context(),
            AuditAction.RECONCILIATION_RUN_COMPLETED,
            "RUN",
            run.id,
            after={"result_hash": result.result_hash, "status": status.value},
            metadata=dict(result.counts()),
        )

        return RunOutcome(
            run=updated,
            summary=summary,
            matches_created=len(result.matches),
            exceptions_created=len(exceptions),
            result_hash=result.result_hash,
        )

    def _build_exceptions(
        self,
        run: RunRow,
        config: ReconciliationConfig,
        result: Any,
        side_a: list[CanonicalTransaction],
        side_b: list[CanonicalTransaction],
    ) -> list[ExceptionRecord]:
        """Turn everything the engine could not match into exceptions.

        Where a best candidate exists, the pair is classified together, which
        gives a far more useful category than 'unmatched': a fee difference, a
        timing difference, a partial payment.
        """
        policy = MaterialityPolicy(
            materiality_threshold=config.controls.materiality_threshold,
            manual_approval_above_materiality=config.controls.manual_approval_above_materiality,
            always_review_types=frozenset(config.controls.always_review_types),
        )
        classifier = ExceptionClassifier(config=config, materiality=policy)
        by_id = {t.id: t for t in (*side_a, *side_b)}
        records: list[ExceptionRecord] = []

        for transaction in result.unmatched_a:
            decision = result.decisions.get(transaction.id)
            top = decision.top if decision else None
            if top is not None and top.candidate.side_b_ids:
                partner = by_id.get(top.candidate.side_b_ids[0])
                if partner is not None:
                    classification = classify_pair(transaction, partner, config)
                    records.append(
                        classifier.build(
                            tenant_id=self.context.tenant_id,
                            run_id=run.id,
                            transactions=[transaction, partner],
                            classification=classification,
                        )
                    )
                    continue
            records.append(
                classifier.build(
                    tenant_id=self.context.tenant_id,
                    run_id=run.id,
                    transactions=[transaction],
                    classification=classify_unmatched(transaction, "A", config),
                )
            )

        paired = {tid for record in records for tid in record.transaction_ids}
        for transaction in result.unmatched_b:
            if transaction.id in paired:
                continue
            records.append(
                classifier.build(
                    tenant_id=self.context.tenant_id,
                    run_id=run.id,
                    transactions=[transaction],
                    classification=classify_unmatched(transaction, "B", config),
                )
            )

        return records

    def _build_summary(
        self,
        run: RunRow,
        definition: ReconciliationRow,
        config: ReconciliationConfig,
        result: Any,
        exceptions: list[ExceptionRecord],
        side_a: list[CanonicalTransaction],
        side_b: list[CanonicalTransaction],
    ) -> RunSummary:
        """The dashboard numbers (spec section 41).

        Automation rate is never reported without the manual-review rate beside
        it (spec section 48).
        """
        currency = config.tolerances.date_field and (
            side_a[0].currency if side_a else (side_b[0].currency if side_b else "EUR")
        )
        balance_a = sum((t.amount for t in side_a), Decimal("0"))
        balance_b = sum((t.amount for t in side_b), Decimal("0"))

        auto = [m for m in result.matches if m.decision is DecisionOutcome.AUTO_MATCH]
        suggested = [m for m in result.matches if m.decision is DecisionOutcome.SUGGEST]
        approved = [m for m in result.matches if m.status is MatchGroupStatus.APPROVED]

        matched_amount = sum((abs(m.total_amount) for m in result.matches), Decimal("0"))
        matched_transactions = sum(len(m.members) for m in result.matches)
        total_transactions = len(side_a) + len(side_b)

        high_risk = sum(1 for e in exceptions if e.severity.value in {"HIGH", "CRITICAL"})
        oldest = max((e.age_days for e in exceptions), default=None)

        completion = (matched_transactions / total_transactions) if total_transactions else 0.0
        auto_rate = (len(auto) / len(result.matches)) if result.matches else 0.0
        review_rate = (
            (len(suggested) + len(exceptions)) / total_transactions if total_transactions else 0.0
        )

        return RunSummary(
            run_id=run.id,
            reconciliation_id=definition.id,
            status=ReconciliationStatus(run.status),
            period_start=run.period_start,
            period_end=run.period_end,
            currency=currency or "EUR",
            side_a_balance=balance_a,
            side_b_balance=balance_b,
            difference=balance_a - balance_b,
            matched_amount=matched_amount,
            matched_transaction_count=matched_transactions,
            auto_matched_count=len(auto),
            human_approved_count=len(approved),
            suggested_count=len(suggested),
            exception_count=len(exceptions),
            high_risk_exception_count=high_risk,
            unmatched_a_count=len(result.unmatched_a),
            unmatched_b_count=len(result.unmatched_b),
            oldest_exception_age_days=oldest,
            completion_pct=round(completion * 100, 2),
            auto_match_rate=round(auto_rate, 4),
            # No false matches have been confirmed yet for this run. The value
            # stays None rather than 0.0 so the dashboard shows "not yet
            # measured" instead of implying a proven zero.
            false_match_rate=None,
            manual_review_rate=round(review_rate, 4),
        )


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
