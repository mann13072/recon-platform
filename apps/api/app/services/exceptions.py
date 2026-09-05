"""Exception workflow service (spec sections 31, 32, 92).

Wraps :class:`packages.exceptions.workflow.ExceptionWorkflow` with persistence,
evidence handling, audit and the AI advisory path. The workflow object keeps the
transition rules; this layer never bypasses it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from apps.api.app.dependencies import RequestContext
from apps.api.app.infrastructure.mappers import apply_exception_updates
from apps.api.app.infrastructure.models import (
    AICallRow,
    EvidenceRow,
    ExceptionCommentRow,
)
from apps.api.app.infrastructure.repositories import (
    ExceptionRepository,
    TransactionRepository,
)
from apps.api.app.infrastructure.storage import safe_filename
from packages.ai.exception_classification import classify_with_ai
from packages.ai.privacy import AIDisabledError, DataRegionViolation
from packages.ai.schemas import AICallRecord
from packages.audit.evidence import EvidenceMetadata
from packages.controls.permissions import Permission
from packages.domain.dates import utc_now
from packages.domain.enums import AuditAction, ExceptionStatus
from packages.domain.models.exceptions import Evidence, ExceptionRecord
from packages.exceptions.aging import build_aging_report
from packages.exceptions.classifier import Classification, classify_unmatched
from packages.exceptions.escalation import EscalationPolicy, evaluate_escalation
from packages.exceptions.workflow import ExceptionWorkflow, IllegalTransition

__all__ = ["ExceptionService", "ExceptionServiceError"]


class ExceptionServiceError(ValueError):
    def __init__(self, message: str, code: str = "exception_failed") -> None:
        super().__init__(message)
        self.code = code


@dataclass(slots=True)
class ExceptionService:
    context: RequestContext

    @property
    def repository(self) -> ExceptionRepository:
        return ExceptionRepository(
            session=self.context.session, tenant_id=self.context.tenant_id
        )

    @property
    def transactions(self) -> TransactionRepository:
        return TransactionRepository(
            session=self.context.session, tenant_id=self.context.tenant_id
        )

    # -- reads -------------------------------------------------------------
    def get(self, exception_id: UUID) -> ExceptionRecord:
        record = self.repository.get(exception_id)
        if record is None:
            raise ExceptionServiceError("No such exception.", "not_found")
        return record

    def aging(self, run_id: UUID | None = None) -> dict[str, Any]:
        records = self.repository.list(run_id=run_id, limit=10_000)
        report = build_aging_report(records)
        return {
            "total_open": report.total_open,
            "total_exposure": str(report.total_exposure),
            "overdue_count": report.overdue_count,
            "overdue_exposure": str(report.overdue_exposure),
            "oldest_age_days": report.oldest_age_days,
            "by_category": report.by_category,
            "by_severity": report.by_severity,
            "buckets": [
                {
                    "label": bucket.label,
                    "count": bucket.count,
                    "exposure": str(bucket.exposure),
                    "high_risk_count": bucket.high_risk_count,
                }
                for bucket in report.buckets
            ],
        }

    def escalations(self, run_id: UUID | None = None) -> list[dict[str, Any]]:
        policy = EscalationPolicy()
        result: list[dict[str, Any]] = []
        for record in self.repository.list(run_id=run_id, limit=10_000):
            triggers = evaluate_escalation(record, policy)
            if triggers:
                result.append(
                    {
                        "exception_id": str(record.id),
                        "title": record.title,
                        "severity": record.severity.value,
                        "triggers": [
                            {"code": t.code, "reason": t.reason, "escalate_to": t.escalate_to.value}
                            for t in triggers
                        ],
                    }
                )
        return result

    # -- transitions -------------------------------------------------------
    def transition(
        self,
        exception_id: UUID,
        target: ExceptionStatus,
        *,
        expected_version: int | None = None,
        reason: str | None = None,
        owner_user_id: UUID | None = None,
        resolution_code: str | None = None,
        proposed_resolution: str | None = None,
    ) -> ExceptionRecord:
        row = self.repository.get_row(exception_id)
        if row is None:
            raise ExceptionServiceError("No such exception.", "not_found")

        record = self.repository.get(exception_id)
        assert record is not None

        try:
            updated = ExceptionWorkflow().transition(
                record,
                target,
                self.context.principal,
                reason=reason,
                owner_user_id=owner_user_id,
                resolution_code=resolution_code,
                proposed_resolution=proposed_resolution,
                expected_version=expected_version,
            )
        except IllegalTransition as exc:
            raise ExceptionServiceError(str(exc), exc.code) from exc

        apply_exception_updates(row, updated)
        self.context.session.flush()

        self.context.audit.record(
            self.context.audit_context(),
            _ACTION_FOR_STATUS.get(target, AuditAction.EXCEPTION_ASSIGNED),
            "EXCEPTION",
            exception_id,
            before={"status": record.status.value},
            after={"status": updated.status.value},
            reason=reason,
            metadata={"resolution_code": resolution_code or ""},
        )
        return updated

    def comment(self, exception_id: UUID, body: str) -> ExceptionCommentRow:
        self.context.principal.require(Permission.COMMENT_EXCEPTION)
        if not body.strip():
            raise ExceptionServiceError("A comment cannot be empty.", "empty_comment")
        if self.repository.get(exception_id) is None:
            raise ExceptionServiceError("No such exception.", "not_found")

        row = ExceptionCommentRow(
            id=uuid4(),
            tenant_id=self.context.tenant_id,
            exception_id=exception_id,
            author_id=self.context.principal.user_id,  # type: ignore[arg-type]
            body=body.strip()[:8000],
        )
        self.context.session.add(row)
        self.context.session.flush()

        self.context.audit.record(
            self.context.audit_context(),
            AuditAction.EXCEPTION_COMMENTED,
            "EXCEPTION",
            exception_id,
            metadata={"comment_id": str(row.id)},
        )
        return row

    def comments(self, exception_id: UUID) -> list[ExceptionCommentRow]:
        return (
            self.context.session.query(ExceptionCommentRow)
            .filter(
                ExceptionCommentRow.tenant_id == self.context.tenant_id,
                ExceptionCommentRow.exception_id == exception_id,
            )
            .order_by(ExceptionCommentRow.created_at)
            .all()
        )

    # -- evidence ----------------------------------------------------------
    def add_evidence(
        self,
        exception_id: UUID,
        *,
        filename: str,
        mime_type: str,
        data: bytes,
        kind: str = "document",
    ) -> Evidence:
        self.context.principal.require(Permission.ADD_EVIDENCE)
        if self.repository.get(exception_id) is None:
            raise ExceptionServiceError("No such exception.", "not_found")

        try:
            metadata = EvidenceMetadata.inspect(safe_filename(filename), mime_type, data)
        except ValueError as exc:
            raise ExceptionServiceError(str(exc), "invalid_evidence") from exc

        key, digest = self.context.storage.put_content(
            self.context.tenant_id,
            "evidence",
            data,
            extension=filename.rsplit(".", 1)[-1] if "." in filename else "",
            content_type=mime_type,
        )

        row = EvidenceRow(
            id=uuid4(),
            tenant_id=self.context.tenant_id,
            exception_id=exception_id,
            filename=metadata.filename,
            mime_type=metadata.mime_type,
            byte_size=metadata.byte_size,
            sha256=digest,
            storage_key=key,
            kind=kind,
            uploaded_by=self.context.principal.user_id,  # type: ignore[arg-type]
            uploaded_at=utc_now(),
        )
        self.context.session.add(row)
        self.context.session.flush()

        self.context.audit.record(
            self.context.audit_context(),
            AuditAction.EXCEPTION_EVIDENCE_ADDED,
            "EXCEPTION",
            exception_id,
            after={"sha256": digest},
            metadata={"filename": metadata.filename, "byte_size": metadata.byte_size},
        )
        return Evidence(
            id=row.id,
            tenant_id=row.tenant_id,
            exception_id=exception_id,
            filename=row.filename,
            mime_type=row.mime_type,
            byte_size=row.byte_size,
            sha256=row.sha256,
            storage_key=row.storage_key,
            uploaded_by=row.uploaded_by,
            uploaded_at=row.uploaded_at,
            kind=row.kind,
        )

    def evidence_for(self, exception_id: UUID) -> list[EvidenceRow]:
        return (
            self.context.session.query(EvidenceRow)
            .filter(
                EvidenceRow.tenant_id == self.context.tenant_id,
                EvidenceRow.exception_id == exception_id,
            )
            .order_by(EvidenceRow.uploaded_at)
            .all()
        )

    # -- AI advisory -------------------------------------------------------
    def request_ai_classification(self, exception_id: UUID) -> dict[str, Any]:
        """Ask the model for an opinion. It changes no state (spec section 31).

        Whatever comes back, the exception's category and status are untouched.
        Only ``proposed_resolution`` is populated, tagged as AI-authored, so a
        human can accept or reject it.
        """
        self.context.principal.require(Permission.USE_AI_SUGGESTIONS)
        record = self.get(exception_id)
        row = self.repository.get_row(exception_id)
        assert row is not None

        transactions = self.transactions.get_many(list(record.transaction_ids))
        primary = transactions[0] if transactions else None
        partner = transactions[1] if len(transactions) > 1 else None

        deterministic = Classification(
            category=record.category,
            confidence=0.5,
            reason_codes=record.reason_codes,
            detail=record.detail,
        )

        try:
            outcome = classify_with_ai(
                deterministic,
                tenant_id=self.context.tenant_id,
                provider=self.context.ai,
                settings=self.context.ai_settings,
                transaction=primary,
                candidate=partner,
            )
        except (AIDisabledError, DataRegionViolation) as exc:
            return {
                "available": False,
                "reason": str(exc),
                "category": record.category.value,
            }

        if outcome.ai is not None:
            self._record_ai_call(outcome.ai.call, "EXCEPTION", exception_id)

        suggestion = outcome.ai_suggestion
        if suggestion is None:
            return {
                "available": False,
                "reason": (
                    outcome.ai.call.failure_reason
                    if outcome.ai
                    else outcome.ai_skipped_reason
                ),
                "category": record.category.value,
            }

        row.proposed_resolution = suggestion.suggested_resolution or None
        row.proposed_by_actor_type = "AI_ASSISTANT"
        row.ai_suggestion_id = outcome.ai.call.id if outcome.ai else None
        self.context.session.flush()

        self.context.audit.record(
            self.context.audit_context(),
            AuditAction.AI_SUGGESTION_CREATED,
            "EXCEPTION",
            exception_id,
            metadata={
                "suggested_category": suggestion.category.value,
                "confidence": f"{suggestion.confidence:.4f}",
                "model": self.context.ai.model_name,
                # The stored category is unchanged; this is advisory only.
                "applied": "false",
            },
        )

        return {
            "available": True,
            "category": record.category.value,
            "suggested_category": suggestion.category.value,
            "confidence": suggestion.confidence,
            "reason_codes": list(suggestion.reason_codes),
            "explanation": suggestion.human_explanation,
            "cited_fields": list(suggestion.cited_fields),
            "suggested_resolution": suggestion.suggested_resolution,
            "advisory_only": True,
        }

    def record_ai_decision(self, exception_id: UUID, *, accepted: bool) -> None:
        """Record whether the human took the suggestion (spec section 46)."""
        record = self.get(exception_id)
        if record.ai_suggestion_id is None:
            raise ExceptionServiceError(
                "This exception has no AI suggestion to decide on.", "no_suggestion"
            )
        call = self.context.session.get(AICallRow, record.ai_suggestion_id)
        if call is not None and call.tenant_id == self.context.tenant_id:
            call.user_decision = "accepted" if accepted else "rejected"
            self.context.session.flush()

        self.context.audit.record(
            self.context.audit_context(),
            AuditAction.AI_SUGGESTION_CREATED
            if accepted
            else AuditAction.AI_SUGGESTION_REJECTED,
            "EXCEPTION",
            exception_id,
            metadata={"decision": "accepted" if accepted else "rejected"},
        )

    def _record_ai_call(self, call: AICallRecord, subject_type: str, subject_id: UUID) -> None:
        self.context.session.add(
            AICallRow(
                id=call.id,
                tenant_id=call.tenant_id,
                task=call.task,
                provider=call.provider,
                model_name=call.model_name,
                model_version=call.model_version,
                prompt_version=call.prompt_version,
                input_hash=call.input_hash,
                policy=call.policy,
                redacted_field_count=call.redacted_field_count,
                latency_ms=call.latency_ms,
                cost_usd=call.cost_usd,
                schema_valid=call.schema_valid,
                failure_reason=call.failure_reason,
                output_summary=call.output_summary,
                user_decision=call.user_decision,
                subject_type=subject_type,
                subject_id=subject_id,
                occurred_at=call.occurred_at,
            )
        )
        self.context.session.flush()

    def reclassify_deterministically(self, exception_id: UUID) -> Classification:
        """Re-run the deterministic classifier on current data."""
        record = self.get(exception_id)
        transactions = self.transactions.get_many(list(record.transaction_ids))
        if not transactions:
            raise ExceptionServiceError(
                "This exception references no transactions.", "no_transactions"
            )
        from packages.domain.models.reconciliation import ReconciliationConfig

        config = ReconciliationConfig(
            name="ad-hoc",
            side_a={"type": "bank"},  # type: ignore[arg-type]
            side_b={"type": "ledger"},  # type: ignore[arg-type]
        )
        return classify_unmatched(transactions[0], "A", config)


_ACTION_FOR_STATUS: dict[ExceptionStatus, AuditAction] = {
    ExceptionStatus.ASSIGNED: AuditAction.EXCEPTION_ASSIGNED,
    ExceptionStatus.PROPOSED_RESOLUTION: AuditAction.EXCEPTION_RESOLUTION_PROPOSED,
    ExceptionStatus.RESOLVED: AuditAction.EXCEPTION_RESOLVED,
    ExceptionStatus.CLOSED: AuditAction.EXCEPTION_CLOSED,
}
