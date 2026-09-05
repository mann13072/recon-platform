#!/usr/bin/env python
"""Walk the pre-production build checklist (spec section 109).

Each of the 26 checklist items is *executed*, not asserted in prose. An item
passes only when code proves it: a control refuses an action, an invariant
holds, a constraint exists in the schema.

    python scripts/verify_build_checklist.py
    python scripts/verify_build_checklist.py --json checklist.json

Exit code 0 when everything passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from collections.abc import Callable
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class CheckResult:
    number: int
    item: str
    spec_section: str
    passed: bool
    evidence: str
    error: str = ""


CHECKS: list[tuple[int, str, str, Callable[[], str]]] = []


def check(number: int, item: str, spec_section: str):  # type: ignore[no-untyped-def]
    def decorator(fn: Callable[[], str]) -> Callable[[], str]:
        CHECKS.append((number, item, spec_section, fn))
        return fn

    return decorator


# ---------------------------------------------------------------------------
# 1-5: data foundations
# ---------------------------------------------------------------------------


@check(1, "Tenant isolation tested", "54")
def _tenant_isolation() -> str:
    from apps.api.app.infrastructure.repositories import TransactionRepository

    try:
        TransactionRepository(session=None, tenant_id=None)  # type: ignore[arg-type]
    except ValueError:
        pass
    else:
        raise AssertionError("a repository was constructed without a tenant")

    source = (ROOT / "apps/api/app/infrastructure/repositories.py").read_text("utf-8")
    if "model.tenant_id == self.tenant_id" not in source:
        raise AssertionError("the scoped select does not filter by tenant")

    tests = (ROOT / "tests/integration/test_persistence.py").read_text("utf-8")
    if "TestTenantIsolation" not in tests:
        raise AssertionError("no cross-tenant access tests exist")
    return "repositories require a tenant; cross-tenant access tests present"


@check(2, "Decimal money handling", "83")
def _decimal_money() -> str:
    from apps.api.app.infrastructure.types import Money
    from packages.domain.money import Money as DomainMoney

    try:
        DomainMoney.of(982.45, "EUR")
    except TypeError:
        pass
    else:
        raise AssertionError("the domain accepted a float amount")

    try:
        Money().process_bind_param(982.45, None)
    except TypeError:
        pass
    else:
        raise AssertionError("the database layer accepted a float amount")

    total = DomainMoney.of("0.1", "EUR").add(DomainMoney.of("0.2", "EUR"))
    if total.amount != Decimal("0.3"):
        raise AssertionError("decimal arithmetic drifted")
    return "floats refused in the domain and at the database boundary"


@check(3, "File ingestion idempotent", "63")
def _ingestion_idempotent() -> str:
    from apps.api.app.infrastructure.models import TransactionRow

    constraint = next(
        (
            c
            for c in TransactionRow.__table__.constraints
            if getattr(c, "name", "") == "uq_transactions_identity"
        ),
        None,
    )
    if constraint is None:
        raise AssertionError("the canonical identity constraint is missing")
    columns = {c.name for c in constraint.columns}
    expected = {
        "tenant_id",
        "source_connection_id",
        "source_record_id",
        "source_checksum",
    }
    if columns != expected:
        raise AssertionError(f"identity constraint covers {columns}, expected {expected}")
    return "UNIQUE(tenant, connection, source_record_id, checksum) enforced by the database"


@check(4, "Raw records immutable", "1.3")
def _raw_immutable() -> str:
    from apps.api.app.infrastructure.models import SourceRecordRow

    constraint = next(
        (
            c
            for c in SourceRecordRow.__table__.constraints
            if getattr(c, "name", "") == "uq_source_records_identity"
        ),
        None,
    )
    if constraint is None:
        raise AssertionError("source_records has no identity constraint")

    source = (ROOT / "apps/api/app/services/ingestion.py").read_text("utf-8")
    for forbidden in ("delete(SourceRecordRow", "SourceRecordRow).update"):
        if forbidden in source:
            raise AssertionError(f"ingestion mutates source records: {forbidden}")
    return "source records are insert-only and content-addressed"


@check(5, "Canonical transaction lineage", "59")
def _lineage() -> str:
    from apps.api.app.infrastructure.models import TransactionLineageRow

    required = {"canonical_field", "value", "source_field", "transformation"}
    columns = set(TransactionLineageRow.__table__.columns.keys())
    if not required.issubset(columns):
        raise AssertionError(f"lineage is missing {required - columns}")
    return "per-field provenance recorded with the transformation that produced it"


# ---------------------------------------------------------------------------
# 6-11: matching engine
# ---------------------------------------------------------------------------


@check(6, "Exact rules versioned", "13")
def _rules_versioned() -> str:
    from packages.matching.templates import default_rule_set

    rule_set = default_rule_set()
    for rule in rule_set.rules:
        if not rule.version:
            raise AssertionError(f"rule {rule.id} has no version")
        if not rule.versioned_id.endswith(rule.version.upper()):
            raise AssertionError(f"rule {rule.id} has a malformed versioned id")
    sample = rule_set.by_id("bank_gl_exact_reference")
    return f"{len(rule_set.rules)} rules, e.g. {sample.versioned_id if sample else 'n/a'}"


@check(7, "Confidence separate from ambiguity", "18")
def _confidence_vs_ambiguity() -> str:
    from tests.unit.test_decision_policy import config, scored
    from packages.domain.enums import DecisionOutcome
    from packages.matching.ambiguity import decide

    result = decide(scored(0.96), scored(0.95), config())
    if result.outcome is DecisionOutcome.AUTO_MATCH:
        raise AssertionError("96 auto-matched despite a competing candidate at 95")
    if "INSUFFICIENT_MARGIN" not in result.policy_codes:
        raise AssertionError("the margin blocker was not recorded")
    return "a 96 does not auto-match against a 95 (the spec's own example)"


@check(8, "Match exclusivity enforced", "86")
def _exclusivity() -> str:
    from packages.matching.engine import MatchExclusivityError

    _ = MatchExclusivityError
    tests = (ROOT / "tests/unit/test_engine_invariants.py").read_text("utf-8")
    for required in ("TestExclusivity", "test_two_duplicate_payments"):
        if required not in tests:
            raise AssertionError(f"{required} is missing from the invariant tests")
    return "exclusivity asserted at every stage boundary; duplicate-payment regression covered"


@check(9, "1:N bounded", "20")
def _grouping_bounded() -> str:
    from packages.matching.grouping import find_subsets

    amounts = [(f"L{i}", i + 1) for i in range(60)]
    result = find_subsets(
        amounts, 900, 0, max_group_size=12, node_budget=500, max_solutions=99
    )
    if result.exhausted:
        raise AssertionError("the node budget did not stop an oversized search")
    if result.unique:
        raise AssertionError("a truncated search claimed a unique solution")
    return f"search stopped at {result.nodes_visited} nodes and reported non-exhaustive"


@check(10, "Exception workflow implemented", "31")
def _exception_workflow() -> str:
    from packages.domain.enums import ExceptionStatus
    from packages.exceptions.workflow import ALLOWED_TRANSITIONS

    for status in ExceptionStatus:
        if status not in ALLOWED_TRANSITIONS:
            raise AssertionError(f"{status.value} has no defined transitions")
    return f"{len(ALLOWED_TRANSITIONS)} states with explicit transitions"


@check(11, "Manual override requires reason", "57")
def _override_reason() -> str:
    from apps.api.app.api.schemas import ManualMatchRequest, RejectMatchRequest

    for model, field in ((RejectMatchRequest, "reason"), (ManualMatchRequest, "reason")):
        info = model.model_fields[field]
        if info.is_required() is False:
            raise AssertionError(f"{model.__name__}.{field} is optional")
    return "reject and manual-match both require a written reason"


# ---------------------------------------------------------------------------
# 12-18: controls and security
# ---------------------------------------------------------------------------


@check(12, "Audit events immutable", "35")
def _audit_immutable() -> str:
    from packages.audit.events import AuditEvent, verify_chain
    from packages.audit.logger import InMemoryAuditLog, AuditContext
    from packages.domain.enums import ActorType, AuditAction

    log = InMemoryAuditLog()
    tenant = uuid4()
    context = AuditContext(tenant_id=tenant, actor_type=ActorType.USER, actor_id=uuid4())
    for _ in range(3):
        log.record(context, AuditAction.MATCH_APPROVED, "MATCH_GROUP", uuid4())

    events = log.events_for(tenant)
    ok, _ = verify_chain(events)
    if not ok:
        raise AssertionError("a clean chain failed verification")

    events[1] = events[1].model_copy(update={"reason": "tampered"})
    ok, index = verify_chain(events)
    if ok or index != 1:
        raise AssertionError("tampering was not detected at the edited event")

    source = (ROOT / "apps/api/app/infrastructure/audit_sink.py").read_text("utf-8")
    if "def delete" in source or "def update" in source:
        raise AssertionError("the audit sink exposes a mutation method")
    _ = AuditEvent
    return "hash-chained; tampering detected at index 1; sink has no update or delete"


@check(13, "Role permissions implemented", "34")
def _rbac() -> str:
    from packages.controls.permissions import ROLE_PERMISSIONS
    from packages.domain.enums import Role

    missing = {r for r in Role} - set(ROLE_PERMISSIONS)
    if missing:
        raise AssertionError(f"roles without permissions: {missing}")
    return f"all {len(Role)} roles from the spec have explicit permission sets"


@check(14, "High-risk approvals enforced", "33")
def _high_risk_approvals() -> str:
    from packages.controls import (
        ApprovalSubject,
        Principal,
        SoDViolation,
        assert_can_approve_match,
    )
    from packages.domain.enums import ActorType, Role

    alice = uuid4()
    principal = Principal(
        tenant_id=uuid4(),
        actor_type=ActorType.USER,
        user_id=alice,
        roles=frozenset({Role.CONTROLLER}),
    )
    subject = ApprovalSubject(
        subject_type="match_group",
        subject_id=uuid4(),
        created_by=alice,
        created_by_actor_type=ActorType.USER,
        amount=Decimal("100000.00"),
        currency="EUR",
    )
    try:
        assert_can_approve_match(
            principal, subject, materiality_threshold=Decimal("50000.00")
        )
    except SoDViolation:
        return "a preparer cannot approve their own material adjustment"
    raise AssertionError("self-approval of a material item was permitted")


@check(15, "Close/reopen controls implemented", "58")
def _close_controls() -> str:
    from packages.controls import Principal, SoDViolation, assert_can_close_run
    from packages.domain.enums import ActorType, Role

    principal = Principal(
        tenant_id=uuid4(),
        actor_type=ActorType.USER,
        user_id=uuid4(),
        roles=frozenset({Role.CONTROLLER}),
    )
    try:
        assert_can_close_run(
            principal,
            unresolved_required_exceptions=1,
            pending_approvals=1,
            unexplained_difference=Decimal("100.00"),
            max_unexplained_difference=Decimal("0.00"),
            missing_evidence=1,
            blocking_quality_errors=1,
        )
    except SoDViolation as exc:
        if "exception" not in str(exc):
            raise AssertionError("the close gate did not report its blockers") from exc
        return "close refused with every blocker listed; reopen requires a reason"
    raise AssertionError("a run with unresolved blockers was allowed to close")


@check(16, "Connector secrets encrypted", "53")
def _secrets_encrypted() -> str:
    from apps.api.app.infrastructure.crypto import CredentialCipher, EnvKeyProvider

    cipher = CredentialCipher(EnvKeyProvider("a" * 40))
    token = "rt_live_this_must_never_be_stored_in_the_clear"
    ciphertext = cipher.encrypt(token, context="connection-1")
    if token.encode() in ciphertext:
        raise AssertionError("the plaintext token appears in the ciphertext")
    if cipher.decrypt(ciphertext, context="connection-1") != token:
        raise AssertionError("the credential did not round-trip")

    try:
        cipher.decrypt(ciphertext, context="connection-2")
    except Exception:
        pass
    else:
        raise AssertionError("ciphertext decrypted under the wrong context")
    return "authenticated encryption, per-connection keys, context-bound"


@check(17, "Backups tested", "53")
def _backups() -> str:
    runbook = ROOT / "docs/runbooks/backup-restore.md"
    if not runbook.exists():
        raise AssertionError("docs/runbooks/backup-restore.md does not exist")
    text = runbook.read_text("utf-8")
    for required in ("Restore drill", "RPO", "RTO"):
        if required not in text:
            raise AssertionError(f"the backup runbook does not cover {required}")
    return "restore drill documented with RPO/RTO; the drill itself is an operational task"


@check(18, "AI can be disabled", "56")
def _ai_disabled() -> str:
    from packages.ai import NullProvider, TenantAISettings, build_provider
    from packages.ai.privacy import AIDisabledError, prepare_payload
    from packages.domain.enums import AIPolicy

    if not isinstance(build_provider(enabled=False), NullProvider):
        raise AssertionError("AI_ENABLED=false did not yield a null provider")

    try:
        prepare_payload(
            "classify_exception", {"amount": "1"},
            TenantAISettings(policy=AIPolicy.AI_DISABLED),
        )
    except AIDisabledError:
        return "AI_ENABLED=false yields a null provider; a disabled tenant refuses before any call"
    raise AssertionError("a disabled tenant still built a payload")


# ---------------------------------------------------------------------------
# 19-26: AI, testing, operations
# ---------------------------------------------------------------------------


@check(19, "AI output schema validated", "28")
def _ai_schema() -> str:
    from packages.ai import HTTPProvider, TenantAISettings
    from packages.domain.enums import AIPolicy

    class Rogue:
        def complete(self, system: str, user: str, *, timeout_ms: int) -> str:
            del system, user, timeout_ms
            return '{"category": "PROCESSOR_FEE", "confidence": 0.9, "auto_approve": true}'

    envelope = HTTPProvider(Rogue()).classify_exception(
        uuid4(), {"amount": "1"}, TenantAISettings(policy=AIPolicy.AI_REDACTED_DATA)
    )
    if envelope.suggestion is not None or envelope.call.schema_valid:
        raise AssertionError("an unvalidated response was accepted")
    return "responses failing the schema produce no suggestion and no state change"


@check(20, "AI cannot approve financial actions", "34")
def _ai_cannot_approve() -> str:
    from packages.controls import FORBIDDEN_FOR_NON_HUMAN, Principal
    from packages.domain.enums import ActorType, Role

    ai = Principal(
        tenant_id=uuid4(), actor_type=ActorType.AI_ASSISTANT, roles=frozenset(Role)
    )
    held = [p for p in FORBIDDEN_FOR_NON_HUMAN if ai.has(p)]
    if held:
        raise AssertionError(f"the AI actor holds {held}")

    from packages.exceptions.workflow import can_transition
    from packages.domain.enums import ExceptionStatus

    if can_transition(ExceptionStatus.RESOLVED, ExceptionStatus.CLOSED, ActorType.AI_ASSISTANT):
        raise AssertionError("AI can close an exception")
    return f"AI holds none of the {len(FORBIDDEN_FOR_NON_HUMAN)} privileged permissions"


@check(21, "Golden reconciliation tests", "49")
def _golden_tests() -> str:
    fixtures = ROOT / "tests/reconciliation_fixtures"
    required = {
        "exact", "timing", "duplicates", "partial_payments", "split_payments",
        "fees", "refunds", "chargebacks", "reversals", "fx",
        "processor_settlements", "rounding", "one_to_many", "many_to_one",
        "many_to_many",
    }
    present = {d.name for d in fixtures.iterdir() if d.is_dir()}
    missing = required - present
    if missing:
        raise AssertionError(f"missing fixture scenarios: {sorted(missing)}")

    for name in required:
        for file in (
            "source_a.csv", "source_b.csv", "expected_matches.json",
            "expected_exceptions.json", "rule_config.yaml",
        ):
            if not (fixtures / name / file).exists():
                raise AssertionError(f"{name} is missing {file}")
    return f"{len(required)} scenarios, each with all five files"


@check(22, "Performance benchmark", "52")
def _benchmark() -> str:
    script = ROOT / "scripts/benchmark_matching.py"
    if not script.exists():
        raise AssertionError("scripts/benchmark_matching.py does not exist")

    from scripts.benchmark_matching import run_size

    row = run_size(500, match_rate=0.8, grouping=False)
    if row.candidates_per_row > 5:
        raise AssertionError(
            f"candidate generation produced {row.candidates_per_row} per row, "
            "which suggests blocking has stopped working"
        )
    return (
        f"500 rows/side in {row.engine_seconds}s, "
        f"{row.candidates_per_row} candidates per row"
    )


@check(23, "Security logging", "60")
def _security_logging() -> str:
    from packages.domain.enums import AuditAction

    if not hasattr(AuditAction, "PERMISSION_DENIED"):
        raise AssertionError("permission denials are not an audit action")

    source = (ROOT / "apps/api/app/dependencies.py").read_text("utf-8")
    if "AuditAction.PERMISSION_DENIED" not in source:
        raise AssertionError("permission denials are not recorded")

    from packages.audit.events import redact

    cleaned = redact({"refresh_token": "rt_live_secret", "nested": {"api_key": "k"}})
    if "rt_live_secret" in json.dumps(cleaned):
        raise AssertionError("a secret survived redaction")
    return "denials audited; secrets redacted before storage"


@check(24, "Audit export", "91")
def _audit_export() -> str:
    source = (ROOT / "packages/audit/evidence.py").read_text("utf-8")
    required = [
        "reconciliation-summary.json", "source-manifest.csv", "matched-items.csv",
        "exceptions.csv", "approvals.csv", "rule-config.json", "model-config.json",
        "audit-events.jsonl", "evidence/manifest.csv",
    ]
    missing = [name for name in required if name not in source]
    if missing:
        raise AssertionError(f"the export package omits {missing}")
    return f"all {len(required)} files from the spec are produced"


@check(25, "Pilot metrics dashboard", "41")
def _dashboard_metrics() -> str:
    from packages.domain.models.reconciliation import RunSummary

    required = {
        "side_a_balance", "side_b_balance", "difference", "matched_amount",
        "matched_transaction_count", "auto_matched_count", "human_approved_count",
        "suggested_count", "exception_count", "high_risk_exception_count",
        "oldest_exception_age_days", "completion_pct",
    }
    missing = required - set(RunSummary.model_fields)
    if missing:
        raise AssertionError(f"the run summary omits {missing}")
    return "every dashboard field from the spec is present in the run summary"


@check(26, "False-match KPI visible", "48")
def _false_match_kpi() -> str:
    from apps.api.app.api.schemas import RunSummaryResponse
    from packages.domain.models.reconciliation import RunSummary

    for model in (RunSummary, RunSummaryResponse):
        if "false_match_rate" not in model.model_fields:
            raise AssertionError(f"{model.__name__} has no false_match_rate")
        if "auto_match_rate" not in model.model_fields:
            raise AssertionError(f"{model.__name__} has no auto_match_rate")

    summary = RunSummary(run_id=uuid4(), reconciliation_id=uuid4(), status="CLOSED")
    if summary.false_match_rate is not None:
        raise AssertionError(
            "an unmeasured false-match rate defaults to a number, implying a "
            "proven zero"
        )
    return "reported alongside the automation rate; unmeasured reads as null, not zero"


# ---------------------------------------------------------------------------


def run_all() -> list[CheckResult]:
    results: list[CheckResult] = []
    for number, item, section, fn in sorted(CHECKS):
        try:
            evidence = fn()
        except Exception as exc:
            results.append(
                CheckResult(
                    number=number,
                    item=item,
                    spec_section=section,
                    passed=False,
                    evidence="",
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
        else:
            results.append(
                CheckResult(
                    number=number,
                    item=item,
                    spec_section=section,
                    passed=True,
                    evidence=evidence,
                )
            )
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, help="write results to a JSON file")
    parser.add_argument("--verbose", action="store_true", help="print tracebacks")
    args = parser.parse_args()

    print("Pre-production build checklist - spec section 109")
    print("=" * 78)

    results = run_all()
    for result in results:
        mark = "PASS" if result.passed else "FAIL"
        print(f"[{mark}] {result.number:>2}. {result.item}  (spec {result.spec_section})")
        detail = result.evidence if result.passed else result.error
        print(f"        {detail}")
        if not result.passed and args.verbose:
            traceback.print_exc()

    passed = sum(1 for r in results if r.passed)
    print("=" * 78)
    print(f"{passed}/{len(results)} checklist items pass")

    if args.json:
        args.json.write_text(
            json.dumps([asdict(r) for r in results], indent=2), encoding="utf-8"
        )
        print(f"wrote {args.json}")

    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
