"""Accounting controls: RBAC, SoD, materiality, period lock, journals.

Spec sections 33, 34, 45, 57 and 58.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from packages.controls import (
    ApprovalSubject,
    MatchAssessment,
    MaterialityPolicy,
    Permission,
    PermissionDenied,
    PeriodLocked,
    PeriodLockRegistry,
    Principal,
    SoDViolation,
    UnbalancedJournalError,
    assert_can_approve_match,
    assert_can_change_rules,
    assert_can_close_run,
    assert_can_modify_run,
    assert_can_reopen_run,
    assess,
    requires_manual_approval,
    severity_for,
    validate_journal,
)
from packages.domain.enums import (
    ActorType,
    ExceptionSeverity,
    ReconciliationStatus,
    Role,
)
from packages.domain.models.exceptions import JournalLine

TENANT = uuid4()
ALICE = uuid4()
BOB = uuid4()


def user(*roles: Role, user_id: object = None, limit: int | None = None) -> Principal:
    return Principal(
        tenant_id=TENANT,
        actor_type=ActorType.USER,
        user_id=user_id or ALICE,  # type: ignore[arg-type]
        email="alice@example.com",
        roles=frozenset(roles),
        approval_limit=limit,
    )


class TestRolePermissions:
    def test_viewer_cannot_change_anything(self) -> None:
        viewer = user(Role.VIEWER)
        for permission in (
            Permission.APPROVE_MATCH,
            Permission.UPLOAD_FILE,
            Permission.CLOSE_RUN,
            Permission.EDIT_RULES,
        ):
            assert not viewer.has(permission)

    def test_preparer_cannot_approve(self) -> None:
        preparer = user(Role.PREPARER)
        assert preparer.has(Permission.CREATE_MANUAL_MATCH)
        assert not preparer.has(Permission.APPROVE_MATCH)

    def test_auditor_reads_everything_and_changes_nothing(self) -> None:
        auditor = user(Role.AUDITOR)
        assert auditor.has(Permission.VIEW_AUDIT)
        assert auditor.has(Permission.EXPORT_AUDIT_PACKAGE)
        assert not auditor.has(Permission.APPROVE_MATCH)
        assert not auditor.has(Permission.UPLOAD_FILE)

    def test_administrator_does_not_approve_financial_actions(self) -> None:
        """Platform administration and financial approval are different duties."""
        admin = user(Role.ADMINISTRATOR)
        assert admin.has(Permission.MANAGE_USERS)
        assert not admin.has(Permission.APPROVE_MATCH)
        assert not admin.has(Permission.APPROVE_JOURNAL)
        assert not admin.has(Permission.CLOSE_RUN)

    def test_integration_admin_cannot_touch_financial_state(self) -> None:
        integrator = user(Role.INTEGRATION_ADMINISTRATOR)
        assert integrator.has(Permission.MANAGE_CONNECTIONS)
        assert not integrator.has(Permission.APPROVE_MATCH)
        assert not integrator.has(Permission.CLOSE_RUN)

    def test_require_raises_with_a_useful_message(self) -> None:
        with pytest.raises(PermissionDenied, match="match:approve"):
            user(Role.PREPARER).require(Permission.APPROVE_MATCH)


class TestMakerChecker:
    def _subject(self, created_by: object, **overrides: object) -> ApprovalSubject:
        defaults: dict[str, object] = {
            "subject_type": "match_group",
            "subject_id": uuid4(),
            "created_by": created_by,
            "created_by_actor_type": ActorType.USER,
            "amount": Decimal("100000.00"),
            "currency": "EUR",
        }
        defaults.update(overrides)
        return ApprovalSubject(**defaults)  # type: ignore[arg-type]

    def test_preparer_cannot_approve_their_own_high_value_match(self) -> None:
        alice = user(Role.CONTROLLER, user_id=ALICE)
        with pytest.raises(SoDViolation) as exc:
            assert_can_approve_match(
                alice,
                self._subject(ALICE),
                materiality_threshold=Decimal("50000.00"),
            )
        assert exc.value.code == "SELF_APPROVAL"

    def test_a_second_person_can_approve_it(self) -> None:
        bob = user(Role.CONTROLLER, user_id=BOB)
        assert_can_approve_match(
            bob, self._subject(ALICE), materiality_threshold=Decimal("50000.00")
        )

    def test_self_approval_below_materiality_is_allowed(self) -> None:
        alice = user(Role.REVIEWER, user_id=ALICE)
        assert_can_approve_match(
            alice,
            self._subject(ALICE, amount=Decimal("12.00")),
            materiality_threshold=Decimal("50000.00"),
        )

    def test_self_approval_of_a_manual_match_is_blocked_at_any_amount(self) -> None:
        alice = user(Role.CONTROLLER, user_id=ALICE)
        with pytest.raises(SoDViolation):
            assert_can_approve_match(
                alice,
                self._subject(ALICE, amount=Decimal("1.00"), is_manual=True),
                materiality_threshold=Decimal("50000.00"),
            )

    def test_approval_limit_is_enforced(self) -> None:
        bob = user(Role.CONTROLLER, user_id=BOB, limit=10_000)
        with pytest.raises(SoDViolation) as exc:
            assert_can_approve_match(
                bob, self._subject(ALICE), materiality_threshold=Decimal("50000.00")
            )
        assert exc.value.code == "APPROVAL_LIMIT_EXCEEDED"

    def test_a_non_human_actor_can_never_approve(self) -> None:
        engine = Principal(
            tenant_id=TENANT,
            actor_type=ActorType.MATCH_ENGINE,
            roles=frozenset({Role.CONTROLLER}),
        )
        with pytest.raises(SoDViolation) as exc:
            assert_can_approve_match(
                engine, self._subject(ALICE), materiality_threshold=Decimal("1.00")
            )
        assert exc.value.code == "NON_HUMAN_APPROVAL"


class TestMateriality:
    def test_amount_at_threshold_requires_approval(self) -> None:
        policy = MaterialityPolicy(materiality_threshold=Decimal("50000.00"))
        assert requires_manual_approval(
            MatchAssessment(total_amount=Decimal("50000.00")), policy
        )
        assert not requires_manual_approval(
            MatchAssessment(total_amount=Decimal("49999.99")), policy
        )

    def test_manual_adjustment_always_requires_approval(self) -> None:
        policy = MaterialityPolicy(materiality_threshold=Decimal("50000.00"))
        decision = assess(
            MatchAssessment(total_amount=Decimal("2.00"), contains_manual_adjustment=True),
            policy,
        )
        assert decision.requires_approval
        assert "manual adjustment" in decision.explain()

    def test_percentage_of_balance_can_tighten_the_threshold(self) -> None:
        policy = MaterialityPolicy(
            materiality_threshold=Decimal("50000.00"),
            percentage_of_balance=Decimal("0.01"),
            account_balance=Decimal("1000000.00"),
        )
        assert policy.effective_threshold() == Decimal("10000.00")
        assert requires_manual_approval(
            MatchAssessment(total_amount=Decimal("15000.00")), policy
        )

    def test_two_dollars_and_two_million_get_different_severities(self) -> None:
        """The spec's own framing of why materiality is not confidence."""
        policy = MaterialityPolicy(materiality_threshold=Decimal("50000.00"))
        assert severity_for(Decimal("2.00"), policy) is ExceptionSeverity.LOW
        assert severity_for(Decimal("2000000.00"), policy) is ExceptionSeverity.CRITICAL

    def test_closed_period_forces_approval(self) -> None:
        policy = MaterialityPolicy(
            materiality_threshold=Decimal("50000.00"), period_is_closed=True
        )
        assert requires_manual_approval(MatchAssessment(total_amount=Decimal("1.00")), policy)


class TestPeriodLock:
    def test_locking_requires_permission(self) -> None:
        registry = PeriodLockRegistry()
        with pytest.raises(PermissionDenied):
            registry.lock(
                user(Role.PREPARER),
                tenant_id=TENANT,
                entity="acme-gmbh",
                period_start=date(2026, 8, 1),
                period_end=date(2026, 8, 31),
            )

    def test_a_date_inside_a_locked_period_is_refused(self) -> None:
        registry = PeriodLockRegistry()
        registry.lock(
            user(Role.CONTROLLER),
            tenant_id=TENANT,
            entity="acme-gmbh",
            period_start=date(2026, 8, 1),
            period_end=date(2026, 8, 31),
        )
        with pytest.raises(PeriodLocked):
            registry.assert_open("acme-gmbh", date(2026, 8, 15))
        registry.assert_open("acme-gmbh", date(2026, 9, 1))

    def test_a_record_with_no_date_fails_closed(self) -> None:
        """A control that cannot prove safety must refuse, not assume."""
        registry = PeriodLockRegistry()
        registry.lock(
            user(Role.CONTROLLER),
            tenant_id=TENANT,
            entity="acme-gmbh",
            period_start=date(2026, 8, 1),
            period_end=date(2026, 8, 31),
        )
        with pytest.raises(PeriodLocked):
            registry.assert_open("acme-gmbh", None)

    def test_unlocking_requires_a_reason(self) -> None:
        registry = PeriodLockRegistry()
        controller = user(Role.CONTROLLER)
        lock = registry.lock(
            controller,
            tenant_id=TENANT,
            entity="acme-gmbh",
            period_start=date(2026, 8, 1),
            period_end=date(2026, 8, 31),
        )
        with pytest.raises(SoDViolation, match="reason"):
            registry.unlock(controller, lock, "   ")
        registry.unlock(controller, lock, "Audit adjustment approved by the CFO.")
        registry.assert_open("acme-gmbh", date(2026, 8, 15))


class TestCloseControls:
    def test_a_closed_run_cannot_be_modified(self) -> None:
        with pytest.raises(SoDViolation, match="closed"):
            assert_can_modify_run(user(Role.CONTROLLER), ReconciliationStatus.CLOSED)

    def test_close_reports_every_blocker_at_once(self) -> None:
        with pytest.raises(SoDViolation) as exc:
            assert_can_close_run(
                user(Role.CONTROLLER),
                unresolved_required_exceptions=3,
                pending_approvals=1,
                unexplained_difference=Decimal("120.00"),
                max_unexplained_difference=Decimal("0.00"),
                missing_evidence=2,
                blocking_quality_errors=1,
            )
        message = str(exc.value)
        assert "3 exception(s)" in message
        assert "1 approval(s)" in message
        assert "120.00" in message
        assert "2 item(s)" in message
        assert "1 blocking data-quality" in message

    def test_a_clean_run_closes(self) -> None:
        assert_can_close_run(
            user(Role.CONTROLLER),
            unresolved_required_exceptions=0,
            pending_approvals=0,
            unexplained_difference=Decimal("0.00"),
            max_unexplained_difference=Decimal("0.00"),
            missing_evidence=0,
            blocking_quality_errors=0,
        )

    def test_reopening_requires_a_reason(self) -> None:
        with pytest.raises(SoDViolation, match="reason"):
            assert_can_reopen_run(user(Role.CONTROLLER), None)
        assert_can_reopen_run(user(Role.CONTROLLER), "Late bank statement received.")

    def test_weakening_a_control_needs_the_threshold_permission(self) -> None:
        admin = user(Role.ADMINISTRATOR)
        assert_can_change_rules(admin, weakens_controls=False)
        with pytest.raises(PermissionDenied, match="thresholds:edit"):
            assert_can_change_rules(admin, weakens_controls=True)
        assert_can_change_rules(user(Role.CONTROLLER), weakens_controls=True)


class TestJournalValidation:
    def test_balanced_entry_passes(self) -> None:
        validate_journal(
            [
                JournalLine(account="6100", side="debit", amount=Decimal("17.55"),
                            currency="EUR"),
                JournalLine(account="1100", side="credit", amount=Decimal("17.55"),
                            currency="EUR"),
            ]
        )

    def test_unbalanced_entry_raises(self) -> None:
        with pytest.raises(UnbalancedJournalError, match="not balanced"):
            validate_journal(
                [
                    JournalLine(account="6100", side="debit", amount=Decimal("17.55"),
                                currency="EUR"),
                    JournalLine(account="1100", side="credit", amount=Decimal("17.50"),
                                currency="EUR"),
                ]
            )

    def test_mixed_currencies_raise(self) -> None:
        with pytest.raises(UnbalancedJournalError, match="mixes currencies"):
            validate_journal(
                [
                    JournalLine(account="6100", side="debit", amount=Decimal("10.00"),
                                currency="EUR"),
                    JournalLine(account="1100", side="credit", amount=Decimal("10.00"),
                                currency="USD"),
                ]
            )

    def test_empty_and_zero_entries_raise(self) -> None:
        with pytest.raises(UnbalancedJournalError):
            validate_journal([])
        with pytest.raises(UnbalancedJournalError, match="zero total"):
            validate_journal(
                [
                    JournalLine(account="6100", side="debit", amount=Decimal("0"),
                                currency="EUR"),
                    JournalLine(account="1100", side="credit", amount=Decimal("0"),
                                currency="EUR"),
                ]
            )

    def test_negative_amounts_are_rejected_at_the_line(self) -> None:
        with pytest.raises(ValueError, match="unsigned"):
            JournalLine(account="6100", side="debit", amount=Decimal("-1.00"),
                        currency="EUR")
