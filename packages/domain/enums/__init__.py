"""Platform-wide enumerations.

Everything the spec names as a taxonomy lives here so that the API, the engine,
the workflow layer and the audit log cannot drift apart.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "ActorType",
    "AIPolicy",
    "AuditAction",
    "ConnectorState",
    "DecisionOutcome",
    "DebitCredit",
    "EventName",
    "ExceptionCategory",
    "ExceptionSeverity",
    "ExceptionStatus",
    "MatchCardinality",
    "MatchGroupStatus",
    "QualityLevel",
    "ReconciliationStatus",
    "Role",
    "Side",
]


class Side(StrEnum):
    """Which dataset a transaction belongs to within one reconciliation."""

    A = "A"
    B = "B"


class DebitCredit(StrEnum):
    DEBIT = "debit"
    CREDIT = "credit"
    UNKNOWN = "unknown"


class MatchCardinality(StrEnum):
    ONE_TO_ONE = "1:1"
    ONE_TO_MANY = "1:N"
    MANY_TO_ONE = "N:1"
    MANY_TO_MANY = "N:N"


class DecisionOutcome(StrEnum):
    """Output of the decision policy (spec section 18)."""

    AUTO_MATCH = "AUTO_MATCH"
    SUGGEST = "SUGGEST"
    EXCEPTION = "EXCEPTION"


class MatchGroupStatus(StrEnum):
    PROPOSED = "PROPOSED"
    SUGGESTED = "SUGGESTED"
    AUTO_APPROVED = "AUTO_APPROVED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    UNMATCHED = "UNMATCHED"

    @property
    def is_active(self) -> bool:
        """Active groups hold an exclusive claim on their transactions."""
        return self in {
            MatchGroupStatus.PROPOSED,
            MatchGroupStatus.SUGGESTED,
            MatchGroupStatus.AUTO_APPROVED,
            MatchGroupStatus.APPROVED,
        }


class ReconciliationStatus(StrEnum):
    """Spec section 90. A run is not complete merely because matching finished."""

    DRAFT = "DRAFT"
    READY = "READY"
    RUNNING = "RUNNING"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    READY_TO_CLOSE = "READY_TO_CLOSE"
    CLOSED = "CLOSED"
    REOPENED = "REOPENED"
    FAILED = "FAILED"


class ExceptionCategory(StrEnum):
    """Spec section 30. Configurable per tenant; this is the shipped default."""

    TIMING_DIFFERENCE = "TIMING_DIFFERENCE"
    MISSING_BANK_RECORD = "MISSING_BANK_RECORD"
    MISSING_LEDGER_RECORD = "MISSING_LEDGER_RECORD"
    DUPLICATE_RECORD = "DUPLICATE_RECORD"
    PARTIAL_PAYMENT = "PARTIAL_PAYMENT"
    SPLIT_PAYMENT = "SPLIT_PAYMENT"
    OVERPAYMENT = "OVERPAYMENT"
    UNDERPAYMENT = "UNDERPAYMENT"
    PROCESSOR_FEE = "PROCESSOR_FEE"
    BANK_FEE = "BANK_FEE"
    FX_DIFFERENCE = "FX_DIFFERENCE"
    ROUNDING_DIFFERENCE = "ROUNDING_DIFFERENCE"
    REFUND = "REFUND"
    REVERSAL = "REVERSAL"
    CHARGEBACK = "CHARGEBACK"
    WITHHOLDING_TAX = "WITHHOLDING_TAX"
    WRONG_REFERENCE = "WRONG_REFERENCE"
    WRONG_DATE = "WRONG_DATE"
    WRONG_AMOUNT = "WRONG_AMOUNT"
    UNRECOGNIZED_COUNTERPARTY = "UNRECOGNIZED_COUNTERPARTY"
    UNSUPPORTED_GROUPING = "UNSUPPORTED_GROUPING"
    DATA_QUALITY = "DATA_QUALITY"
    POSSIBLE_FRAUD = "POSSIBLE_FRAUD"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    OTHER = "OTHER"


class ExceptionSeverity(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ExceptionStatus(StrEnum):
    """Spec section 31. AI may never transition an exception to CLOSED."""

    OPEN = "OPEN"
    TRIAGED = "TRIAGED"
    ASSIGNED = "ASSIGNED"
    INVESTIGATING = "INVESTIGATING"
    PROPOSED_RESOLUTION = "PROPOSED_RESOLUTION"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"
    BLOCKED = "BLOCKED"
    ESCALATED = "ESCALATED"
    REOPENED = "REOPENED"


class Role(StrEnum):
    """Spec section 34."""

    VIEWER = "VIEWER"
    PREPARER = "PREPARER"
    REVIEWER = "REVIEWER"
    APPROVER = "APPROVER"
    CONTROLLER = "CONTROLLER"
    ADMINISTRATOR = "ADMINISTRATOR"
    AUDITOR = "AUDITOR"
    INTEGRATION_ADMINISTRATOR = "INTEGRATION_ADMINISTRATOR"


class ActorType(StrEnum):
    """Spec section 35."""

    USER = "USER"
    SYSTEM = "SYSTEM"
    CONNECTOR = "CONNECTOR"
    MATCH_ENGINE = "MATCH_ENGINE"
    AI_ASSISTANT = "AI_ASSISTANT"
    ADMIN = "ADMIN"


class AuditAction(StrEnum):
    """Actions recorded in the append-only audit log."""

    TENANT_CREATED = "TENANT_CREATED"
    SOURCE_CONNECTED = "SOURCE_CONNECTED"
    SOURCE_SYNC_STARTED = "SOURCE_SYNC_STARTED"
    SOURCE_SYNC_COMPLETED = "SOURCE_SYNC_COMPLETED"
    SOURCE_SYNC_FAILED = "SOURCE_SYNC_FAILED"
    FILE_UPLOADED = "FILE_UPLOADED"
    FILE_PROFILED = "FILE_PROFILED"
    FILE_MAPPED = "FILE_MAPPED"
    FILE_INGESTED = "FILE_INGESTED"
    RECONCILIATION_CREATED = "RECONCILIATION_CREATED"
    RECONCILIATION_UPDATED = "RECONCILIATION_UPDATED"
    RECONCILIATION_RUN_STARTED = "RECONCILIATION_RUN_STARTED"
    RECONCILIATION_RUN_COMPLETED = "RECONCILIATION_RUN_COMPLETED"
    RECONCILIATION_RUN_FAILED = "RECONCILIATION_RUN_FAILED"
    MATCH_CREATED = "MATCH_CREATED"
    MATCH_SUGGESTED = "MATCH_SUGGESTED"
    MATCH_AUTO_APPROVED = "MATCH_AUTO_APPROVED"
    MATCH_APPROVED = "MATCH_APPROVED"
    MATCH_REJECTED = "MATCH_REJECTED"
    MATCH_OVERRIDDEN = "MATCH_OVERRIDDEN"
    MATCH_UNMATCHED = "MATCH_UNMATCHED"
    EXCEPTION_CREATED = "EXCEPTION_CREATED"
    EXCEPTION_ASSIGNED = "EXCEPTION_ASSIGNED"
    EXCEPTION_COMMENTED = "EXCEPTION_COMMENTED"
    EXCEPTION_EVIDENCE_ADDED = "EXCEPTION_EVIDENCE_ADDED"
    EXCEPTION_RESOLUTION_PROPOSED = "EXCEPTION_RESOLUTION_PROPOSED"
    EXCEPTION_RESOLVED = "EXCEPTION_RESOLVED"
    EXCEPTION_CLOSED = "EXCEPTION_CLOSED"
    JOURNAL_PROPOSED = "JOURNAL_PROPOSED"
    JOURNAL_APPROVED = "JOURNAL_APPROVED"
    JOURNAL_REJECTED = "JOURNAL_REJECTED"
    RUN_CLOSED = "RUN_CLOSED"
    RUN_REOPENED = "RUN_REOPENED"
    RULE_CHANGED = "RULE_CHANGED"
    RULE_SIMULATED = "RULE_SIMULATED"
    THRESHOLD_CHANGED = "THRESHOLD_CHANGED"
    AI_SUGGESTION_CREATED = "AI_SUGGESTION_CREATED"
    AI_SUGGESTION_REJECTED = "AI_SUGGESTION_REJECTED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    AUDIT_EXPORT_CREATED = "AUDIT_EXPORT_CREATED"


# Spec section 61 lists the product event names. They intentionally overlap with
# AuditAction: audit is the legal record, events drive notifications/analytics.
EventName = AuditAction


class ConnectorState(StrEnum):
    """Spec section 89."""

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    AUTH_EXPIRED = "AUTH_EXPIRED"
    RATE_LIMITED = "RATE_LIMITED"
    FAILED = "FAILED"
    DISABLED = "DISABLED"


class QualityLevel(StrEnum):
    """Spec section 85 data-quality gate."""

    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


class AIPolicy(StrEnum):
    """Per-tenant AI privacy setting (spec section 56)."""

    AI_DISABLED = "AI_DISABLED"
    AI_METADATA_ONLY = "AI_METADATA_ONLY"
    AI_REDACTED_DATA = "AI_REDACTED_DATA"
    AI_PRIVATE_PROVIDER = "AI_PRIVATE_PROVIDER"
