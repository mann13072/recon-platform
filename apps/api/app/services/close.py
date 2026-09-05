"""Closing and reopening a reconciliation, and the audit export.

Spec sections 57, 58 and 91. A run closes only when every gate passes, and
closing produces a signed certificate whose hash covers the run's whole result.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from apps.api.app.dependencies import RequestContext
from apps.api.app.infrastructure.models import (
    EvidenceRow,
    ReconciliationRow,
    RunRow,
)
from apps.api.app.infrastructure.repositories import (
    ExceptionRepository,
    MatchRepository,
    ReconciliationRepository,
    RunRepository,
    TransactionRepository,
)
from packages.audit.evidence import AuditPackage, build_audit_package
from packages.controls.segregation_of_duties import (
    assert_can_close_run,
    assert_can_reopen_run,
)
from packages.domain.dates import utc_now
from packages.domain.enums import AuditAction, ExceptionStatus, ReconciliationStatus
from packages.domain.models.exceptions import Evidence
from packages.domain.models.reconciliation import (
    CloseCertificate,
    ReconciliationConfig,
    RunSnapshot,
    RunSummary,
)

__all__ = ["CloseError", "CloseService"]

# Statuses that still count as unresolved when deciding whether a run may close.
_UNRESOLVED = frozenset(
    {
        ExceptionStatus.OPEN,
        ExceptionStatus.TRIAGED,
        ExceptionStatus.ASSIGNED,
        ExceptionStatus.INVESTIGATING,
        ExceptionStatus.PROPOSED_RESOLUTION,
        ExceptionStatus.AWAITING_APPROVAL,
        ExceptionStatus.BLOCKED,
        ExceptionStatus.ESCALATED,
        ExceptionStatus.REOPENED,
    }
)


class CloseError(ValueError):
    def __init__(self, message: str, code: str = "close_failed") -> None:
        super().__init__(message)
        self.code = code


@dataclass(slots=True)
class CloseService:
    context: RequestContext

    @property
    def runs(self) -> RunRepository:
        return RunRepository(session=self.context.session, tenant_id=self.context.tenant_id)

    @property
    def matches(self) -> MatchRepository:
        return MatchRepository(session=self.context.session, tenant_id=self.context.tenant_id)

    @property
    def exceptions(self) -> ExceptionRepository:
        return ExceptionRepository(session=self.context.session, tenant_id=self.context.tenant_id)

    @property
    def definitions(self) -> ReconciliationRepository:
        return ReconciliationRepository(
            session=self.context.session, tenant_id=self.context.tenant_id
        )

    # -- close -------------------------------------------------------------
    def preflight(self, run_id: UUID) -> dict[str, object]:
        """What still stands between this run and being closed.

        Exposed on its own so the UI can show the checklist before the user
        clicks, rather than only reporting a refusal afterwards.
        """
        run, definition, config = self._load(run_id)
        blockers = self._blockers(run, config)
        return {
            "run_id": str(run.id),
            "status": run.status,
            "can_close": not any(blockers.values()),
            "unresolved_required_exceptions": blockers["unresolved_required_exceptions"],
            "pending_approvals": blockers["pending_approvals"],
            "unexplained_difference": str(self._difference(run)),
            "max_unexplained_difference": str(config.controls.max_unexplained_difference),
            "missing_evidence": blockers["missing_evidence"],
            "blocking_quality_errors": blockers["blocking_quality_errors"],
            "entity": definition.entity,
        }

    def close(self, run_id: UUID, *, expected_version: int) -> CloseCertificate:
        run, definition, config = self._load(run_id)

        if run.status == ReconciliationStatus.CLOSED.value:
            raise CloseError("This reconciliation is already closed.", "already_closed")

        blockers = self._blockers(run, config)
        difference = self._difference(run)

        assert_can_close_run(
            self.context.principal,
            unresolved_required_exceptions=blockers["unresolved_required_exceptions"],
            pending_approvals=blockers["pending_approvals"],
            unexplained_difference=difference,
            max_unexplained_difference=config.controls.max_unexplained_difference,
            missing_evidence=blockers["missing_evidence"],
            blocking_quality_errors=blockers["blocking_quality_errors"],
        )

        summary = RunSummary.model_validate(run.summary or {})
        snapshot = self.runs.snapshot(run.id)
        period = run.period_end.strftime("%Y-%m") if run.period_end else utc_now().strftime("%Y-%m")

        certificate = CloseCertificate(
            run_id=run.id,
            period=period,
            status=ReconciliationStatus.CLOSED,
            book_balance=summary.side_b_balance,
            external_balance=summary.side_a_balance,
            difference=difference,
            unresolved_items=blockers["unresolved_required_exceptions"],
            closed_by=self.context.principal.user_id,  # type: ignore[arg-type]
            closed_at=utc_now(),
            config_version=definition.config_version,
            snapshot_hash=snapshot.snapshot_hash if snapshot else "",
        )
        certificate = certificate.model_copy(
            update={"certificate_hash": _sign(certificate, run.result_hash or "")}
        )

        self.runs.update_status(
            run.id,
            expected_version=expected_version,
            status=ReconciliationStatus.CLOSED.value,
            closed_by=self.context.principal.user_id,
            closed_at=certificate.closed_at,
            close_certificate=certificate.model_dump(mode="json"),
        )

        self.context.audit.record(
            self.context.audit_context(),
            AuditAction.RUN_CLOSED,
            "RUN",
            run.id,
            before={"status": run.status},
            after=certificate.model_dump(mode="json"),
            metadata={
                "certificate_hash": certificate.certificate_hash,
                "difference": str(difference),
            },
        )
        return certificate

    def reopen(self, run_id: UUID, *, expected_version: int, reason: str) -> RunRow:
        run, _, _ = self._load(run_id)
        assert_can_reopen_run(self.context.principal, reason)

        if run.status != ReconciliationStatus.CLOSED.value:
            raise CloseError("Only a closed reconciliation can be reopened.", "not_closed")

        updated = self.runs.update_status(
            run.id,
            expected_version=expected_version,
            status=ReconciliationStatus.REOPENED.value,
            reopen_reason=reason,
        )

        self.context.audit.record(
            self.context.audit_context(),
            AuditAction.RUN_REOPENED,
            "RUN",
            run.id,
            before={"status": ReconciliationStatus.CLOSED.value},
            after={"status": updated.status},
            reason=reason,
        )
        return updated

    # -- audit export ------------------------------------------------------
    def audit_package(self, run_id: UUID) -> AuditPackage:
        """Assemble the export from stored state only (spec section 91)."""
        run, definition, config = self._load(run_id)

        summary = RunSummary.model_validate(run.summary or {})
        snapshot_row = self.runs.snapshot(run.id)
        if snapshot_row is None:
            raise CloseError("This run has no snapshot to export.", "no_snapshot")

        snapshot = RunSnapshot(
            run_id=run.id,
            tenant_id=self.context.tenant_id,
            reconciliation_id=definition.id,
            side_a_transaction_ids=tuple(UUID(str(i)) for i in snapshot_row.side_a_transaction_ids),
            side_b_transaction_ids=tuple(UUID(str(i)) for i in snapshot_row.side_b_transaction_ids),
            source_checksums=dict(snapshot_row.source_checksums or {}),
            rule_set_version=snapshot_row.rule_set_version,
            rule_versions=dict(snapshot_row.rule_versions or {}),
            config_version=snapshot_row.config_version,
            config_hash=snapshot_row.config_hash,
            model_version=snapshot_row.model_version,
            snapshot_hash=snapshot_row.snapshot_hash,
            period_start=run.period_start,
            period_end=run.period_end,
            initiated_by=run.initiated_by,
        )

        transactions_repo = TransactionRepository(
            session=self.context.session, tenant_id=self.context.tenant_id
        )
        transaction_ids = [
            *snapshot.side_a_transaction_ids,
            *snapshot.side_b_transaction_ids,
        ]
        transactions = {t.id: t for t in transactions_repo.get_many(transaction_ids)}

        matches = self.matches.list_for_run(run.id, limit=100_000)
        exceptions = self.exceptions.list(run_id=run.id, limit=100_000)
        events = self.context.audit.events_for(self.context.tenant_id)
        evidence = [
            Evidence(
                id=row.id,
                tenant_id=row.tenant_id,
                exception_id=row.exception_id,
                match_group_id=row.match_group_id,
                filename=row.filename,
                mime_type=row.mime_type,
                byte_size=row.byte_size,
                sha256=row.sha256,
                storage_key=row.storage_key,
                uploaded_by=row.uploaded_by,
                uploaded_at=row.uploaded_at,
                retention_policy=row.retention_policy,
                kind=row.kind,
            )
            for row in self._evidence_rows(run.id)
        ]

        certificate = (
            CloseCertificate.model_validate(run.close_certificate)
            if run.close_certificate
            else None
        )

        package = build_audit_package(
            summary=summary,
            snapshot=snapshot,
            config=config,
            matches=matches,
            exceptions=exceptions,
            transactions=transactions,
            audit_events=events,
            evidence=evidence,
            certificate=certificate,
        )

        self.context.audit.record(
            self.context.audit_context(),
            AuditAction.AUDIT_EXPORT_CREATED,
            "RUN",
            run.id,
            metadata={"files": len(package.files)},
        )
        return package

    # -- internals ---------------------------------------------------------
    def _load(self, run_id: UUID) -> tuple[RunRow, ReconciliationRow, ReconciliationConfig]:
        run = self.runs.get(run_id)
        if run is None:
            raise CloseError("No such run.", "not_found")
        definition = self.definitions.get(run.reconciliation_id)
        if definition is None:
            raise CloseError("The reconciliation no longer exists.", "not_found")
        return run, definition, ReconciliationConfig.model_validate(definition.config)

    def _blockers(self, run: RunRow, config: ReconciliationConfig) -> dict[str, int]:
        records = self.exceptions.list(run_id=run.id, limit=100_000)
        unresolved = sum(1 for r in records if r.requires_approval and r.status in _UNRESOLVED)

        match_counts = self.matches.counts_by_status(run.id)
        pending = match_counts.get("SUGGESTED", 0) + match_counts.get("PROPOSED", 0)

        required_categories = set(config.controls.require_evidence_for_categories)
        missing_evidence = 0
        if required_categories:
            with_evidence = {
                row.exception_id for row in self._evidence_rows(run.id) if row.exception_id
            }
            missing_evidence = sum(
                1
                for r in records
                if r.category in required_categories and r.id not in with_evidence
            )

        quality_errors = self._blocking_quality_errors()

        return {
            "unresolved_required_exceptions": unresolved,
            "pending_approvals": pending,
            "missing_evidence": missing_evidence,
            "blocking_quality_errors": quality_errors,
        }

    def _blocking_quality_errors(self) -> int:
        from apps.api.app.infrastructure.models import SourceFileRow

        return int(
            self.context.session.query(SourceFileRow)
            .filter(
                SourceFileRow.tenant_id == self.context.tenant_id,
                SourceFileRow.status == "QUALITY_FAILED",
            )
            .count()
        )

    def _difference(self, run: RunRow) -> Decimal:
        """The part of the balance difference nobody has accounted for yet.

        The raw difference between the two sides is not the same thing. A
        resolved exception *is* an explanation: a timing difference carried
        forward, a fee posted, a duplicate written off. Once a human has
        resolved it, its amount stops being unexplained, and the close gate in
        spec section 58 is about the unexplained remainder.

        Each explained exception contributes with the sign of the side it sits
        on: a side-A record widens the difference, a side-B record narrows it.
        """
        summary = run.summary or {}
        difference = Decimal(str(summary.get("difference", "0")))

        snapshot = self.runs.snapshot(run.id)
        if snapshot is None:
            return difference
        side_a = {str(i) for i in (snapshot.side_a_transaction_ids or [])}

        transactions = TransactionRepository(
            session=self.context.session, tenant_id=self.context.tenant_id
        )
        explained = Decimal("0")

        for record in self.exceptions.list(run_id=run.id, limit=100_000):
            if record.status not in {ExceptionStatus.RESOLVED, ExceptionStatus.CLOSED}:
                continue
            for transaction in transactions.get_many(list(record.transaction_ids)):
                if str(transaction.id) in side_a:
                    explained += transaction.amount
                else:
                    explained -= transaction.amount

        return difference - explained

    def _evidence_rows(self, run_id: UUID) -> list[EvidenceRow]:
        from apps.api.app.infrastructure.models import ExceptionRow

        return (
            self.context.session.query(EvidenceRow)
            .join(ExceptionRow, ExceptionRow.id == EvidenceRow.exception_id)
            .filter(
                EvidenceRow.tenant_id == self.context.tenant_id,
                ExceptionRow.reconciliation_run_id == run_id,
            )
            .all()
        )


def _sign(certificate: CloseCertificate, result_hash: str) -> str:
    """Hash the certificate together with the run's result hash.

    Anything that changed the result would change this signature, so the
    certificate cannot be detached from the result it attests to.
    """
    payload = certificate.model_dump(mode="json")
    payload.pop("certificate_hash", None)
    payload["result_hash"] = result_hash
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
