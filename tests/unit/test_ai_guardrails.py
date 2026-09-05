"""AI guardrails (spec sections 28, 29, 56, 104, 109).

The checklist items being proved here:

* AI can be disabled;
* AI output schema is validated;
* AI cannot approve financial actions;
* schema failures produce no state changes;
* the user can reject a suggestion.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from packages.ai import (
    AIDisabledError,
    DataRegionViolation,
    DeterministicProvider,
    HTTPProvider,
    NullProvider,
    TenantAISettings,
    build_provider,
    prepare_payload,
    redact_value,
)
from packages.ai.schemas import ExceptionClassificationSuggestion
from packages.controls import FORBIDDEN_FOR_NON_HUMAN, Permission, Principal
from packages.domain.enums import ActorType, AIPolicy, ExceptionCategory, Role

TENANT = uuid4()


def settings(**overrides: Any) -> TenantAISettings:
    base: dict[str, Any] = {"policy": AIPolicy.AI_REDACTED_DATA}
    base.update(overrides)
    return TenantAISettings(**base)


class TestDisableSwitch:
    def test_build_provider_returns_null_when_disabled(self) -> None:
        assert isinstance(build_provider(enabled=False), NullProvider)

    def test_disabled_tenant_policy_raises_before_any_call(self) -> None:
        with pytest.raises(AIDisabledError):
            prepare_payload(
                "classify_exception",
                {"amount": "10.00"},
                settings(policy=AIPolicy.AI_DISABLED),
            )

    def test_task_not_enabled_for_tenant_is_refused(self) -> None:
        with pytest.raises(AIDisabledError, match="not enabled"):
            prepare_payload(
                "resolve_entity",
                {"counterparty_name": "ACME"},
                settings(allowed_tasks=frozenset({"classify_exception"})),
            )

    def test_null_provider_never_returns_a_suggestion(self) -> None:
        envelope = NullProvider().classify_exception(TENANT, {"amount": "10.00"}, settings())
        assert envelope.suggestion is None
        assert not envelope.usable
        assert envelope.call.failure_reason is not None


class TestDataMinimisation:
    def test_fields_outside_the_task_allowlist_are_dropped(self) -> None:
        payload = prepare_payload(
            "classify_exception",
            {
                "amount": "982.45",
                "counterparty_account": "DE89370400440532013000",
                "customer_id": "CUST-1",
                "raw_payload": {"everything": "else"},
            },
            settings(),
        )
        assert "amount" in payload.payload
        assert "counterparty_account" not in payload.payload
        assert "customer_id" not in payload.payload
        assert set(payload.dropped_fields) == {
            "counterparty_account",
            "customer_id",
            "raw_payload",
        }

    def test_pii_inside_an_allowed_field_is_masked(self) -> None:
        payload = prepare_payload(
            "classify_exception",
            {"description": "TRANSFER DE89370400440532013000 TO a.person@example.com"},
            settings(),
        )
        assert "DE89370400440532013000" not in payload.payload["description"]
        assert "a.person@example.com" not in payload.payload["description"]
        assert payload.redacted_field_count >= 2

    def test_metadata_only_policy_sends_shapes_not_content(self) -> None:
        payload = prepare_payload(
            "classify_exception",
            {"description": "STRIPE PAYOUT 8F42"},
            settings(policy=AIPolicy.AI_METADATA_ONLY),
        )
        assert payload.payload["description"] == "<18 chars>"

    def test_redact_value_masks_cards_and_long_account_numbers(self) -> None:
        masked, count = redact_value("card 4111 1111 1111 1111 acct 123456789012")
        assert "4111" not in masked
        assert "123456789012" not in masked
        assert count == 2

    def test_input_hash_is_stable(self) -> None:
        data = {"amount": "10.00", "currency": "EUR"}
        first = prepare_payload("classify_exception", data, settings())
        second = prepare_payload(
            "classify_exception", dict(reversed(list(data.items()))), settings()
        )
        assert first.input_hash == second.input_hash


class TestDataRegion:
    def test_provider_outside_allowed_region_is_refused(self) -> None:
        with pytest.raises(DataRegionViolation):
            prepare_payload(
                "classify_exception",
                {"amount": "1.00"},
                settings(allowed_regions=frozenset({"eu"}), provider_region="us"),
            )


class _BadTransport:
    """A model that returns something that does not validate."""

    def __init__(self, response: str) -> None:
        self.response = response
        self.calls = 0

    def complete(self, system: str, user: str, *, timeout_ms: int) -> str:
        del system, user, timeout_ms
        self.calls += 1
        return self.response


class TestSchemaValidation:
    def test_non_json_response_produces_no_suggestion(self) -> None:
        provider = HTTPProvider(_BadTransport("I think this is a processor fee!"))
        envelope = provider.classify_exception(TENANT, {"amount": "1.00"}, settings())
        assert envelope.suggestion is None
        assert not envelope.usable
        assert envelope.call.schema_valid is False

    def test_json_missing_required_fields_produces_no_suggestion(self) -> None:
        provider = HTTPProvider(_BadTransport('{"category": "PROCESSOR_FEE"}'))
        envelope = provider.classify_exception(TENANT, {"amount": "1.00"}, settings())
        assert envelope.suggestion is None
        assert envelope.call.schema_valid is False
        assert "schema validation failed" in (envelope.call.failure_reason or "")

    def test_confidence_outside_zero_to_one_is_rejected(self) -> None:
        provider = HTTPProvider(_BadTransport('{"category": "PROCESSOR_FEE", "confidence": 4.2}'))
        envelope = provider.classify_exception(TENANT, {"amount": "1.00"}, settings())
        assert envelope.suggestion is None

    def test_invented_category_degrades_to_insufficient_evidence(self) -> None:
        """A model must not be able to extend the taxonomy."""
        provider = HTTPProvider(
            _BadTransport('{"category": "DEFINITELY_A_FEE", "confidence": 0.99}')
        )
        envelope = provider.classify_exception(TENANT, {"amount": "1.00"}, settings())
        assert isinstance(envelope.suggestion, ExceptionClassificationSuggestion)
        assert envelope.suggestion.category is ExceptionCategory.INSUFFICIENT_EVIDENCE

    def test_extra_fields_are_rejected(self) -> None:
        """A response cannot smuggle in fields the schema does not define."""
        provider = HTTPProvider(
            _BadTransport('{"category": "PROCESSOR_FEE", "confidence": 0.9, "auto_approve": true}')
        )
        envelope = provider.classify_exception(TENANT, {"amount": "1.00"}, settings())
        assert envelope.suggestion is None

    def test_fenced_json_is_still_parsed(self) -> None:
        provider = HTTPProvider(
            _BadTransport('```json\n{"category": "PROCESSOR_FEE", "confidence": 0.9}\n```')
        )
        envelope = provider.classify_exception(TENANT, {"amount": "1.00"}, settings())
        assert envelope.usable

    def test_transport_failure_is_recorded_not_raised(self) -> None:
        class Broken:
            def complete(self, system: str, user: str, *, timeout_ms: int) -> str:
                raise TimeoutError("model timed out")

        envelope = HTTPProvider(Broken()).classify_exception(TENANT, {"amount": "1.00"}, settings())
        assert envelope.suggestion is None
        assert "TimeoutError" in (envelope.call.failure_reason or "")


class TestAuditMetadata:
    def test_every_call_records_the_required_metadata(self) -> None:
        envelope = DeterministicProvider().classify_exception(
            TENANT,
            {"gross_amount": "1000.00", "fee_amount": "17.55", "net_amount": "982.45"},
            settings(),
        )
        call = envelope.call
        assert call.provider == "deterministic"
        assert call.model_name and call.model_version
        assert call.prompt_version == "classify_exception_v2"
        assert len(call.input_hash) == 64
        assert call.latency_ms >= 0
        assert call.policy == AIPolicy.AI_REDACTED_DATA.value
        assert call.user_decision == "pending"


class TestAICannotApprove:
    def test_ai_actor_holds_no_approval_permission(self) -> None:
        """Even with every role attached, an AI principal cannot approve."""
        ai = Principal(
            tenant_id=TENANT,
            actor_type=ActorType.AI_ASSISTANT,
            roles=frozenset(Role),
        )
        for permission in FORBIDDEN_FOR_NON_HUMAN:
            assert not ai.has(permission), f"AI unexpectedly holds {permission}"

    def test_ai_cannot_approve_a_match(self) -> None:
        ai = Principal(
            tenant_id=TENANT,
            actor_type=ActorType.AI_ASSISTANT,
            roles=frozenset({Role.CONTROLLER}),
        )
        assert not ai.has(Permission.APPROVE_MATCH)
        assert not ai.has(Permission.CLOSE_RUN)
        assert not ai.has(Permission.APPROVE_JOURNAL)

    def test_a_human_controller_can_approve(self) -> None:
        human = Principal(
            tenant_id=TENANT,
            actor_type=ActorType.USER,
            user_id=uuid4(),
            roles=frozenset({Role.CONTROLLER}),
        )
        assert human.has(Permission.APPROVE_MATCH)
        assert human.has(Permission.CLOSE_RUN)


class TestDeterministicProvider:
    def test_recognises_the_settlement_identity(self) -> None:
        envelope = DeterministicProvider().classify_exception(
            TENANT,
            {
                "gross_amount": "1000.00",
                "fee_amount": "17.55",
                "net_amount": "982.45",
                "settlement_id": "8F42",
            },
            settings(),
        )
        assert envelope.usable
        suggestion = envelope.suggestion
        assert isinstance(suggestion, ExceptionClassificationSuggestion)
        assert suggestion.category is ExceptionCategory.PROCESSOR_FEE
        assert "NET_AMOUNT_MATCH" in suggestion.reason_codes
        assert "SETTLEMENT_REFERENCE_MATCH" in suggestion.reason_codes

    def test_says_insufficient_evidence_rather_than_guessing(self) -> None:
        envelope = DeterministicProvider().classify_exception(
            TENANT, {"amount": "5000.00", "currency": "EUR"}, settings()
        )
        suggestion = envelope.suggestion
        assert isinstance(suggestion, ExceptionClassificationSuggestion)
        assert suggestion.category is ExceptionCategory.INSUFFICIENT_EVIDENCE
        assert suggestion.confidence == 0.0

    def test_is_reproducible(self) -> None:
        provider = DeterministicProvider()
        data = {"description": "CHARGEBACK 123", "amount": "50.00"}
        first = provider.classify_exception(TENANT, data, settings())
        second = provider.classify_exception(TENANT, data, settings())
        assert first.suggestion == second.suggestion
        assert first.call.input_hash == second.call.input_hash
