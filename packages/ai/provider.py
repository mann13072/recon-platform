"""The AI provider abstraction (spec section 4).

    AIProvider
    |-- classify_exception()
    |-- extract_references()
    |-- resolve_entity()
    |-- summarize_evidence()
    `-- propose_resolution()

Business logic never depends on a single vendor. Two implementations ship:

* :class:`DeterministicProvider` - the default. Rule-based, no network, no keys.
  It exists so the whole platform runs, and every AI code path is exercised by
  the test suite, with ``AI_ENABLED=false``.
* :class:`HTTPProvider` - talks to a hosted model API. The HTTP client is
  injected, so it is tested against recorded responses rather than a live
  endpoint.

No vendor SDK is imported at module scope anywhere in this package.
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, ValidationError

from packages.ai.privacy import (
    AIDisabledError,
    MinimizedPayload,
    TenantAISettings,
    prepare_payload,
)
from packages.ai.prompts import PROMPT_VERSIONS, SYSTEM_PROMPT, build_prompt
from packages.ai.schemas import (
    AICallRecord,
    AIResponseEnvelope,
    EntityResolutionSuggestion,
    EvidenceSummary,
    ExceptionClassificationSuggestion,
    ExtractedReferenceSuggestion,
    ResolutionProposal,
    SchemaValidationFailure,
)
from packages.domain.enums import ExceptionCategory

__all__ = [
    "AIProvider",
    "DeterministicProvider",
    "HTTPProvider",
    "NullProvider",
    "TransportProtocol",
    "build_provider",
]

_SCHEMA_FOR_TASK: dict[str, type[BaseModel]] = {
    "classify_exception": ExceptionClassificationSuggestion,
    "extract_references": ExtractedReferenceSuggestion,
    "resolve_entity": EntityResolutionSuggestion,
    "summarize_evidence": EvidenceSummary,
    "propose_resolution": ResolutionProposal,
}


class TransportProtocol(Protocol):
    """The minimal contract a model transport must satisfy."""

    def complete(self, system: str, user: str, *, timeout_ms: int) -> str:
        """Return the model's raw text response."""
        ...


class AIProvider(ABC):
    """What the rest of the platform is allowed to ask a model for."""

    name: str = "abstract"
    model_name: str = "none"
    model_version: str = "none"
    cost_per_call_usd: Decimal = Decimal("0")

    @abstractmethod
    def _invoke(self, task: str, payload: MinimizedPayload) -> dict[str, Any]:
        """Produce a raw response dict for a task. Implementations may not raise
        for model-quality reasons; they raise only for transport failures."""

    # -- public API --------------------------------------------------------
    def classify_exception(
        self, tenant_id: UUID, data: dict[str, Any], settings: TenantAISettings
    ) -> AIResponseEnvelope:
        return self._run("classify_exception", tenant_id, data, settings)

    def extract_references(
        self, tenant_id: UUID, data: dict[str, Any], settings: TenantAISettings
    ) -> AIResponseEnvelope:
        return self._run("extract_references", tenant_id, data, settings)

    def resolve_entity(
        self, tenant_id: UUID, data: dict[str, Any], settings: TenantAISettings
    ) -> AIResponseEnvelope:
        return self._run("resolve_entity", tenant_id, data, settings)

    def summarize_evidence(
        self, tenant_id: UUID, data: dict[str, Any], settings: TenantAISettings
    ) -> AIResponseEnvelope:
        return self._run("summarize_evidence", tenant_id, data, settings)

    def propose_resolution(
        self, tenant_id: UUID, data: dict[str, Any], settings: TenantAISettings
    ) -> AIResponseEnvelope:
        return self._run("propose_resolution", tenant_id, data, settings)

    # -- the one path every task takes -------------------------------------
    def _run(
        self,
        task: str,
        tenant_id: UUID,
        data: dict[str, Any],
        settings: TenantAISettings,
    ) -> AIResponseEnvelope:
        """Guard, invoke, validate, record. In that order, every time."""
        payload = prepare_payload(task, data, settings)

        started = time.perf_counter()
        failure: str | None = None
        suggestion: Any = None
        raw: dict[str, Any] | None = None

        try:
            raw = self._invoke(task, payload)
        except Exception as exc:  # transport, timeout, rate limit
            failure = f"{type(exc).__name__}: {exc}"

        if raw is not None:
            try:
                suggestion = _SCHEMA_FOR_TASK[task].model_validate(raw)
            except ValidationError as exc:
                # A schema failure produces no state change at all. The caller
                # keeps whatever the deterministic layer decided.
                failure = f"schema validation failed: {exc.error_count()} error(s)"
                suggestion = None

        latency_ms = (time.perf_counter() - started) * 1000.0

        call = AICallRecord(
            tenant_id=tenant_id,
            task=task,
            provider=self.name,
            model_name=self.model_name,
            model_version=self.model_version,
            prompt_version=PROMPT_VERSIONS[task],
            input_hash=payload.input_hash,
            policy=payload.policy.value,
            redacted_field_count=payload.redacted_field_count,
            latency_ms=round(latency_ms, 3),
            cost_usd=self.cost_per_call_usd,
            schema_valid=failure is None,
            failure_reason=failure,
            output_summary=_summarize(suggestion),
        )
        return AIResponseEnvelope(suggestion=suggestion, call=call)


class NullProvider(AIProvider):
    """Answers nothing. Used when AI is disabled but a provider object is needed."""

    name = "null"

    def _invoke(self, task: str, payload: MinimizedPayload) -> dict[str, Any]:
        raise AIDisabledError("AI is disabled.")


class DeterministicProvider(AIProvider):
    """A rule-based stand-in for a model.

    Every answer is derived from the payload by explicit rules, so it is exactly
    reproducible and costs nothing. It exists for three reasons: the platform
    must run without a vendor key; the AI code paths must be testable; and a
    customer whose policy forbids external processing still gets the feature,
    just with weaker semantics.
    """

    name = "deterministic"
    model_name = "rule-based"
    model_version = "v1"

    def _invoke(self, task: str, payload: MinimizedPayload) -> dict[str, Any]:
        handler = {
            "classify_exception": self._classify,
            "extract_references": self._extract,
            "resolve_entity": self._resolve,
            "summarize_evidence": self._summarize,
            "propose_resolution": self._propose,
        }[task]
        return handler(payload.payload)

    def _classify(self, data: dict[str, Any]) -> dict[str, Any]:
        gross = _decimal(data.get("gross_amount"))
        fee = _decimal(data.get("fee_amount"))
        net = _decimal(data.get("net_amount"))
        amount = _decimal(data.get("amount"))

        if gross is not None and fee is not None and net is not None and gross - abs(fee) == net:
            reasons = ["NET_AMOUNT_MATCH"]
            if data.get("settlement_id"):
                reasons.append("SETTLEMENT_REFERENCE_MATCH")
            return {
                "category": ExceptionCategory.PROCESSOR_FEE.value,
                "confidence": 0.97,
                "reason_codes": reasons,
                "human_explanation": (
                    "The bank deposit equals the processor payout after the reported fee."
                ),
                "cited_fields": ["gross_amount", "fee_amount", "net_amount"],
                "suggested_resolution": ("Post the processor fee to the fee expense account."),
            }

        description = str(data.get("description") or "").upper()
        for token, category in (
            ("CHARGEBACK", ExceptionCategory.CHARGEBACK),
            ("REFUND", ExceptionCategory.REFUND),
            ("REVERSAL", ExceptionCategory.REVERSAL),
            ("FEE", ExceptionCategory.BANK_FEE),
        ):
            if token in description:
                return {
                    "category": category.value,
                    "confidence": 0.72,
                    "reason_codes": ["DESCRIPTION_KEYWORD"],
                    "human_explanation": (
                        f"The description contains '{token}', which indicates a "
                        f"{category.value.replace('_', ' ').lower()}."
                    ),
                    "cited_fields": ["description"],
                }

        if amount is not None and abs(amount) <= Decimal("0.05"):
            return {
                "category": ExceptionCategory.ROUNDING_DIFFERENCE.value,
                "confidence": 0.8,
                "reason_codes": ["SMALL_DIFFERENCE"],
                "human_explanation": "The amount is small enough to be a rounding difference.",
                "cited_fields": ["amount"],
            }

        # The honest answer when the evidence does not support a claim.
        return {
            "category": ExceptionCategory.INSUFFICIENT_EVIDENCE.value,
            "confidence": 0.0,
            "reason_codes": ["INSUFFICIENT_EVIDENCE"],
            "human_explanation": ("The supplied fields do not support any classification."),
            "cited_fields": [],
        }

    def _extract(self, data: dict[str, Any]) -> dict[str, Any]:
        from packages.ingestion.normalization import extract_references

        result = extract_references(str(data.get("description") or ""))
        if not result.references:
            return {
                "kind": "none",
                "value": "",
                "confidence": 0.0,
                "reason_codes": ["INSUFFICIENT_EVIDENCE"],
                "human_explanation": "No known identifier pattern matched.",
                "cited_fields": ["description"],
            }
        first = result.references[0]
        return {
            "kind": first.kind,
            "value": first.value,
            "proposed_pattern": first.pattern_id,
            "confidence": 0.9,
            "reason_codes": ["PATTERN_MATCH"],
            "human_explanation": (f"Pattern {first.pattern_id} matched '{first.value}'."),
            "cited_fields": ["description"],
        }

    def _resolve(self, data: dict[str, Any]) -> dict[str, Any]:
        from packages.matching.fuzzy import jaro_winkler_similarity

        observed = str(data.get("normalized_counterparty") or data.get("counterparty_name") or "")
        candidates = data.get("candidate_entities") or []
        best, score = "", 0.0
        for candidate in candidates:
            similarity = jaro_winkler_similarity(observed, str(candidate))
            if similarity > score:
                best, score = str(candidate), similarity

        return {
            "observed_name": observed,
            "canonical_entity": best,
            "is_same_entity": score >= 0.92,
            "confidence": round(score, 4),
            "reason_codes": ["NAME_SIMILARITY"],
            "human_explanation": (
                f"'{observed}' is {score:.0%} similar to '{best}'."
                if best
                else "No candidate entity was supplied."
            ),
            "cited_fields": ["counterparty_name"],
        }

    def _summarize(self, data: dict[str, Any]) -> dict[str, Any]:
        difference = data.get("difference")
        currency = data.get("currency", "")
        exceptions = data.get("largest_open_exceptions") or []
        fees = data.get("possible_fee_items") or []

        points = [
            f"{len(exceptions)} open exception(s) account for the largest part of the difference.",
            f"{len(fees)} item(s) look like fee or charge differences.",
        ]
        return {
            "summary": (
                f"The unexplained difference is {currency} {difference}. " + " ".join(points)
            ),
            "key_points": points,
            "confidence": 0.6,
            "reason_codes": ["EVIDENCE_SUMMARY"],
            "human_explanation": "Summarised from the deterministic evidence bundle.",
            "cited_fields": ["difference", "largest_open_exceptions"],
        }

    def _propose(self, data: dict[str, Any]) -> dict[str, Any]:
        category = str(data.get("category") or "")
        playbook: dict[str, tuple[str, tuple[str, ...], bool]] = {
            ExceptionCategory.PROCESSOR_FEE.value: (
                "POST_FEE",
                (
                    "Confirm the fee amount against the processor statement.",
                    "Post the fee to the processor fee expense account.",
                    "Re-run the reconciliation.",
                ),
                True,
            ),
            ExceptionCategory.TIMING_DIFFERENCE.value: (
                "CARRY_FORWARD",
                (
                    "Confirm the item clears in the next period.",
                    "Carry it forward as a reconciling item.",
                ),
                False,
            ),
            ExceptionCategory.DUPLICATE_RECORD.value: (
                "INVESTIGATE_DUPLICATE",
                (
                    "Confirm with the source system whether the record was sent twice.",
                    "If it is a genuine duplicate payment, raise a recovery case.",
                ),
                False,
            ),
        }
        code, steps, needs_journal = playbook.get(
            category,
            (
                "INVESTIGATE",
                ("Review the supporting documentation.", "Assign an owner."),
                False,
            ),
        )
        return {
            "resolution_code": code,
            "steps": steps,
            "requires_journal_entry": needs_journal,
            "confidence": 0.6,
            "reason_codes": ["PLAYBOOK"],
            "human_explanation": f"Standard handling for {category or 'this category'}.",
            "cited_fields": ["category"],
        }


class HTTPProvider(AIProvider):
    """Talks to a hosted model API through an injected transport."""

    def __init__(
        self,
        transport: TransportProtocol,
        *,
        name: str = "hosted",
        model_name: str = "unknown",
        model_version: str = "unknown",
        cost_per_call_usd: Decimal = Decimal("0.002"),
        timeout_ms: int = 8_000,
    ) -> None:
        self._transport = transport
        self.name = name
        self.model_name = model_name
        self.model_version = model_version
        self.cost_per_call_usd = cost_per_call_usd
        self._timeout_ms = timeout_ms

    def _invoke(self, task: str, payload: MinimizedPayload) -> dict[str, Any]:
        user = build_prompt(task, payload.payload)
        text = self._transport.complete(SYSTEM_PROMPT, user, timeout_ms=self._timeout_ms)
        return _parse_json_object(text)


def _parse_json_object(text: str) -> dict[str, Any]:
    """Extract a JSON object from a model response.

    Models sometimes wrap JSON in a fenced block. Anything that is not a single
    JSON object is a schema failure, which is handled upstream.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1]
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise SchemaValidationFailure("response was not valid JSON", raw=text) from exc
    if not isinstance(parsed, dict):
        raise SchemaValidationFailure("response was not a JSON object", raw=text)
    return parsed


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (ValueError, ArithmeticError):
        return None


def _summarize(suggestion: Any) -> str:
    if suggestion is None:
        return ""
    for attribute in ("category", "resolution_code", "kind", "canonical_entity"):
        value = getattr(suggestion, attribute, None)
        if value:
            return f"{attribute}={value}"
    return type(suggestion).__name__


def build_provider(
    *,
    enabled: bool,
    provider: str = "deterministic",
    transport: TransportProtocol | None = None,
    model_name: str = "",
    model_version: str = "",
) -> AIProvider:
    """Construct the configured provider.

    ``AI_ENABLED=false`` yields :class:`NullProvider`, so the disable switch is
    a real code path rather than a flag checked at each call site.
    """
    if not enabled:
        return NullProvider()
    if provider == "deterministic":
        return DeterministicProvider()
    if transport is None:
        raise ValueError(f"provider '{provider}' requires a transport; none was configured")
    return HTTPProvider(
        transport,
        name=provider,
        model_name=model_name or provider,
        model_version=model_version or "unknown",
    )
