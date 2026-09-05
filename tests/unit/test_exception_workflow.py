"""Exception state machine and audit chain (spec sections 31, 32, 35)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from packages.audit import (
    AuditContext,
    InMemoryAuditLog,
    verify_chain,
)
from packages.controls import Principal
from packages.domain.dates import utc_now
from packages.domain.enums import (
    ActorType,
    AuditAction,
    ExceptionCategory,
    ExceptionSeverity,
    ExceptionStatus,
    Role,
)
from packages.domain.models.exceptions import ExceptionRecord
from packages.exceptions import (
    ExceptionWorkflow,
    IllegalTransition,
    build_aging_report,
    can_transition,
    evaluate_escalation,
)
from packages.exceptions.escalation import EscalationPolicy

TENANT = uuid4()
RUN = uuid4()
ALICE = uuid4()


def exception(
    status: ExceptionStatus = ExceptionStatus.OPEN,
    *,
    severity: ExceptionSeverity = ExceptionSeverity.MEDIUM,
    age_days: int = 0,
    exposure: str = "100.00",
    owner: object = None,
) -> ExceptionRecord:
    detected = utc_now() - timedelta(days=age_days)
    return ExceptionRecord(
        id=uuid4(),
        tenant_id=TENANT,
        reconciliation_run_id=RUN,
        transaction_ids=(uuid4(),),
        category=ExceptionCategory.PROCESSOR_FEE,
        severity=severity,
        amount_exposure=Decimal(exposure),
        currency="EUR",
        status=status,
        owner_user_id=owner,  # type: ignore[arg-type]
        first_detected_at=detected,
        due_at=detected + timedelta(days=7),
    )


def user(*roles: Role) -> Principal:
    return Principal(
        tenant_id=TENANT,
        actor_type=ActorType.USER,
        user_id=ALICE,
        email="alice@example.com",
        roles=frozenset(roles),
    )


def ai_actor() -> Principal:
    return Principal(
        tenant_id=TENANT,
        actor_type=ActorType.AI_ASSISTANT,
        roles=frozenset({Role.CONTROLLER}),
    )


class TestStateMachine:
    def test_the_happy_path_walks_the_whole_machine(self) -> None:
        workflow = ExceptionWorkflow()
        controller = user(Role.CONTROLLER)
        record = exception()

        record = workflow.transition(record, ExceptionStatus.TRIAGED, controller)
        record = workflow.transition(
            record, ExceptionStatus.ASSIGNED, controller, owner_user_id=ALICE
        )
        record = workflow.transition(record, ExceptionStatus.INVESTIGATING, controller)
        record = workflow.transition(
            record,
            ExceptionStatus.PROPOSED_RESOLUTION,
            controller,
            proposed_resolution="Post the processor fee.",
        )
        record = workflow.transition(record, ExceptionStatus.AWAITING_APPROVAL, controller)
        record = workflow.transition(
            record, ExceptionStatus.RESOLVED, controller, resolution_code="POST_FEE"
        )
        record = workflow.transition(record, ExceptionStatus.CLOSED, controller)

        assert record.status is ExceptionStatus.CLOSED
        assert record.closed_by == ALICE
        assert record.closed_at is not None
        assert record.version == 8

    def test_illegal_jumps_are_refused(self) -> None:
        workflow = ExceptionWorkflow()
        with pytest.raises(IllegalTransition, match="Legal next states"):
            workflow.transition(exception(), ExceptionStatus.CLOSED, user(Role.CONTROLLER))

    def test_ai_cannot_close_an_exception(self) -> None:
        """Spec section 31: AI may not transition an exception to CLOSED."""
        workflow = ExceptionWorkflow()
        resolved = exception(ExceptionStatus.RESOLVED)
        with pytest.raises(IllegalTransition) as exc:
            workflow.transition(resolved, ExceptionStatus.CLOSED, ai_actor())
        assert exc.value.code == "HUMAN_REQUIRED"

    def test_ai_cannot_resolve_an_exception(self) -> None:
        workflow = ExceptionWorkflow()
        proposed = exception(ExceptionStatus.PROPOSED_RESOLUTION)
        with pytest.raises(IllegalTransition) as exc:
            workflow.transition(proposed, ExceptionStatus.RESOLVED, ai_actor(), resolution_code="X")
        assert exc.value.code == "HUMAN_REQUIRED"

    def test_can_transition_agrees_with_the_workflow(self) -> None:
        assert can_transition(ExceptionStatus.RESOLVED, ExceptionStatus.CLOSED, ActorType.USER)
        assert not can_transition(
            ExceptionStatus.RESOLVED, ExceptionStatus.CLOSED, ActorType.AI_ASSISTANT
        )

    def test_ai_suggestion_does_not_move_the_workflow(self) -> None:
        workflow = ExceptionWorkflow()
        record = exception()
        updated = workflow.apply_ai_suggestion(
            record,
            category="PROCESSOR_FEE",
            proposed_resolution="Post the fee.",
            suggestion_id=uuid4(),
        )
        assert updated.status is ExceptionStatus.OPEN
        assert updated.proposed_by_actor_type == "AI_ASSISTANT"
        assert updated.ai_suggestion_id is not None

    def test_resolution_requires_a_code(self) -> None:
        workflow = ExceptionWorkflow()
        record = exception(ExceptionStatus.PROPOSED_RESOLUTION)
        with pytest.raises(IllegalTransition) as exc:
            workflow.transition(record, ExceptionStatus.RESOLVED, user(Role.CONTROLLER))
        assert exc.value.code == "RESOLUTION_CODE_REQUIRED"

    def test_reopening_requires_a_reason(self) -> None:
        workflow = ExceptionWorkflow()
        closed = exception(ExceptionStatus.CLOSED)
        with pytest.raises(IllegalTransition) as exc:
            workflow.transition(closed, ExceptionStatus.REOPENED, user(Role.CONTROLLER))
        assert exc.value.code == "REASON_REQUIRED"

        reopened = workflow.transition(
            closed,
            ExceptionStatus.REOPENED,
            user(Role.CONTROLLER),
            reason="The bank confirmed the fee was posted twice.",
        )
        assert reopened.status is ExceptionStatus.REOPENED
        assert reopened.closed_at is None

    def test_optimistic_locking_detects_a_concurrent_change(self) -> None:
        """Spec section 64: a stale write is reported, never silently applied."""
        workflow = ExceptionWorkflow()
        record = exception()
        moved = workflow.transition(record, ExceptionStatus.TRIAGED, user(Role.CONTROLLER))

        with pytest.raises(IllegalTransition) as exc:
            workflow.transition(
                moved,
                ExceptionStatus.ASSIGNED,
                user(Role.CONTROLLER),
                owner_user_id=ALICE,
                expected_version=1,
            )
        assert exc.value.code == "VERSION_CONFLICT"

    def test_assigning_requires_an_owner(self) -> None:
        workflow = ExceptionWorkflow()
        with pytest.raises(IllegalTransition) as exc:
            workflow.transition(
                exception(ExceptionStatus.TRIAGED),
                ExceptionStatus.ASSIGNED,
                user(Role.CONTROLLER),
            )
        assert exc.value.code == "OWNER_REQUIRED"

    def test_a_viewer_cannot_move_anything(self) -> None:
        from packages.controls import PermissionDenied

        with pytest.raises(PermissionDenied):
            ExceptionWorkflow().transition(exception(), ExceptionStatus.TRIAGED, user(Role.VIEWER))


class TestAging:
    def test_buckets_and_oldest_age(self) -> None:
        report = build_aging_report(
            [
                exception(age_days=2),
                exception(age_days=20),
                exception(age_days=45, severity=ExceptionSeverity.CRITICAL),
                exception(age_days=120, exposure="9000.00"),
                exception(ExceptionStatus.CLOSED, age_days=200),
            ]
        )
        assert report.total_open == 4
        assert report.oldest_age_days == 120
        assert report.bucket("0-7").count == 1  # type: ignore[union-attr]
        assert report.bucket("8-30").count == 1  # type: ignore[union-attr]
        assert report.bucket("31-60").count == 1  # type: ignore[union-attr]
        assert report.bucket("90+").count == 1  # type: ignore[union-attr]
        assert report.bucket("31-60").high_risk_count == 1  # type: ignore[union-attr]

    def test_closed_exceptions_are_excluded(self) -> None:
        report = build_aging_report([exception(ExceptionStatus.CLOSED, age_days=90)])
        assert report.total_open == 0
        assert report.oldest_age_days is None

    def test_overdue_items_are_counted(self) -> None:
        report = build_aging_report([exception(age_days=30)])
        assert report.overdue_count == 1


class TestEscalation:
    def test_a_stale_critical_exception_escalates(self) -> None:
        triggers = evaluate_escalation(
            exception(severity=ExceptionSeverity.CRITICAL, age_days=5),
            EscalationPolicy(),
        )
        codes = {t.code for t in triggers}
        assert "CRITICAL_AGE" in codes
        assert "OVERDUE" not in codes  # due date is 7 days out

    def test_high_exposure_escalates_regardless_of_age(self) -> None:
        triggers = evaluate_escalation(exception(exposure="250000.00"), EscalationPolicy())
        assert any(t.code == "HIGH_EXPOSURE" for t in triggers)

    def test_a_closed_exception_never_escalates(self) -> None:
        assert (
            evaluate_escalation(exception(ExceptionStatus.CLOSED, age_days=400), EscalationPolicy())
            == []
        )


class TestAuditChain:
    def test_events_are_hash_chained(self) -> None:
        log = InMemoryAuditLog()
        context = AuditContext(tenant_id=TENANT, actor_type=ActorType.USER, actor_id=ALICE)

        first = log.record(context, AuditAction.MATCH_APPROVED, "MATCH_GROUP", uuid4())
        second = log.record(context, AuditAction.RUN_CLOSED, "RUN", uuid4())

        assert first.previous_event_hash == ""
        assert second.previous_event_hash == first.event_hash
        ok, index = log.verify(TENANT)
        assert ok and index is None

    def test_tampering_breaks_the_chain_at_the_edited_event(self) -> None:
        log = InMemoryAuditLog()
        context = AuditContext(tenant_id=TENANT, actor_type=ActorType.USER, actor_id=ALICE)
        for _ in range(3):
            log.record(context, AuditAction.MATCH_APPROVED, "MATCH_GROUP", uuid4())

        events = log.events_for(TENANT)
        events[1] = events[1].model_copy(update={"reason": "quietly changed"})

        ok, index = verify_chain(events)
        assert not ok
        assert index == 1

    def test_secrets_never_reach_the_audit_log(self) -> None:
        log = InMemoryAuditLog()
        event = log.record(
            AuditContext(tenant_id=TENANT, actor_type=ActorType.CONNECTOR),
            AuditAction.SOURCE_CONNECTED,
            "CONNECTION",
            uuid4(),
            metadata={
                "provider": "stripe",
                "refresh_token": "rt_live_supersecret",
                "nested": {"client_secret": "sk_live_x"},
            },
        )
        assert event.metadata["refresh_token"] == "[REDACTED]"
        assert event.metadata["nested"]["client_secret"] == "[REDACTED]"
        assert event.metadata["provider"] == "stripe"

    def test_before_and_after_hashes_differ_when_state_changes(self) -> None:
        log = InMemoryAuditLog()
        before = exception()
        after = before.model_copy(update={"status": ExceptionStatus.TRIAGED})
        event = log.record(
            AuditContext(tenant_id=TENANT, actor_type=ActorType.USER, actor_id=ALICE),
            AuditAction.EXCEPTION_ASSIGNED,
            "EXCEPTION",
            before.id,
            before=before,
            after=after,
        )
        assert event.before_hash and event.after_hash
        assert event.before_hash != event.after_hash
