"""The AI privacy guard (spec sections 55 and 56).

Every AI call passes through :func:`prepare_payload`, which runs the eight steps
the spec lists, in order:

    1. classify the requested AI task
    2. minimise fields
    3. redact unnecessary PII
    4. check the tenant AI policy
    5. check the data-region policy
    6. send the request
    7. validate structured output
    8. store audit metadata

Steps 1-5 happen here. Step 6 belongs to the provider, and 7-8 to the service
layer. There is no path to a provider that bypasses this module.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from packages.domain.enums import AIPolicy

__all__ = [
    "AIDisabledError",
    "DataRegionViolation",
    "MinimizedPayload",
    "TASK_FIELD_ALLOWLIST",
    "TenantAISettings",
    "prepare_payload",
    "redact_value",
]


class AIDisabledError(RuntimeError):
    """Raised when AI is called for a tenant that has it switched off."""


class DataRegionViolation(RuntimeError):
    """Raised when a provider is not permitted to process a tenant's data."""


# Exactly which canonical fields each task may see. A field not listed here is
# never sent, whatever the caller passes (spec section 28: "AI should receive
# only the minimum data needed").
TASK_FIELD_ALLOWLIST: dict[str, frozenset[str]] = {
    "classify_exception": frozenset(
        {
            "amount",
            "currency",
            "description",
            "normalized_description",
            "transaction_date",
            "gross_amount",
            "fee_amount",
            "tax_amount",
            "net_amount",
            "settlement_id",
            "payout_id",
            "status",
            "transaction_type",
            "reference",
        }
    ),
    "extract_references": frozenset({"description", "normalized_description"}),
    "resolve_entity": frozenset(
        {"counterparty_name", "normalized_counterparty", "candidate_entities"}
    ),
    "summarize_evidence": frozenset(
        {
            "difference",
            "currency",
            "largest_open_exceptions",
            "aged_items",
            "possible_fee_items",
            "unmatched_groups",
            "question",
        }
    ),
    "propose_resolution": frozenset(
        {
            "category",
            "amount",
            "currency",
            "description",
            "reason_codes",
            "detail",
            "transaction_date",
        }
    ),
}

# Values matching these are replaced wherever they appear, even inside a
# description that a task is otherwise allowed to see.
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b")
_LONG_DIGITS_RE = re.compile(r"\b\d{9,}\b")
_EMAIL_RE = re.compile(r"\b[\w.%-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")


def redact_value(value: str) -> tuple[str, int]:
    """Mask account numbers, cards, IBANs and e-mail addresses in free text.

    Returns the masked text and how many redactions were made, so the audit
    record can show that minimisation actually did something.
    """
    count = 0

    def sub(pattern: re.Pattern[str], replacement: str, text: str) -> str:
        nonlocal count
        text, n = pattern.subn(replacement, text)
        count += n
        return text

    masked = sub(_IBAN_RE, "[IBAN]", value)
    masked = sub(_CARD_RE, "[CARD]", masked)
    masked = sub(_EMAIL_RE, "[EMAIL]", masked)
    masked = sub(_LONG_DIGITS_RE, "[ACCOUNT]", masked)
    return masked, count


@dataclass(frozen=True, slots=True)
class TenantAISettings:
    """Per-tenant AI configuration (spec section 56)."""

    policy: AIPolicy = AIPolicy.AI_DISABLED
    allowed_regions: frozenset[str] = frozenset({"eu"})
    provider_region: str = "eu"
    allowed_tasks: frozenset[str] = frozenset(TASK_FIELD_ALLOWLIST)
    max_cost_usd_per_call: float = 0.05
    latency_budget_ms: int = 8_000

    @property
    def enabled(self) -> bool:
        return self.policy is not AIPolicy.AI_DISABLED


@dataclass(slots=True)
class MinimizedPayload:
    payload: dict[str, Any]
    input_hash: str
    redacted_field_count: int = 0
    dropped_fields: list[str] = field(default_factory=list)
    policy: AIPolicy = AIPolicy.AI_DISABLED

    def as_json(self) -> str:
        return json.dumps(self.payload, sort_keys=True, separators=(",", ":"), default=str)


def prepare_payload(
    task: str,
    raw: dict[str, Any],
    settings: TenantAISettings,
) -> MinimizedPayload:
    """Steps 1-5 of the guard. Raises rather than sending anything questionable."""
    # 1. classify the task
    if task not in TASK_FIELD_ALLOWLIST:
        raise ValueError(f"unknown AI task: {task}")

    # 4. tenant policy
    if not settings.enabled:
        raise AIDisabledError(
            "AI is disabled for this tenant. The deterministic result stands."
        )
    if task not in settings.allowed_tasks:
        raise AIDisabledError(f"AI task '{task}' is not enabled for this tenant.")

    # 5. data region
    if settings.provider_region not in settings.allowed_regions:
        raise DataRegionViolation(
            f"The configured AI provider runs in '{settings.provider_region}' but "
            f"this tenant only permits {sorted(settings.allowed_regions)}."
        )

    allowed = TASK_FIELD_ALLOWLIST[task]
    minimized: dict[str, Any] = {}
    dropped: list[str] = []
    redactions = 0

    # 2. minimise, 3. redact
    for key, value in sorted(raw.items()):
        if key not in allowed:
            dropped.append(key)
            continue
        if settings.policy is AIPolicy.AI_METADATA_ONLY and isinstance(value, str):
            # Metadata-only tenants send shapes, never content.
            minimized[key] = f"<{len(value)} chars>"
            redactions += 1
            continue
        if isinstance(value, str):
            masked, count = redact_value(value)
            minimized[key] = masked
            redactions += count
        elif isinstance(value, dict):
            nested = prepare_payload(task, value, settings)
            minimized[key] = nested.payload
            redactions += nested.redacted_field_count
        else:
            minimized[key] = value

    encoded = json.dumps(minimized, sort_keys=True, separators=(",", ":"), default=str)
    return MinimizedPayload(
        payload=minimized,
        input_hash=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        redacted_field_count=redactions,
        dropped_fields=dropped,
        policy=settings.policy,
    )
