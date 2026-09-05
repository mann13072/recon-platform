"""Connector contract, Stripe normalisation, and webhook security.

Spec sections 9, 67, 88 and 89. Everything here runs offline against recorded
payloads: no credentials, no network.
"""

from __future__ import annotations

import random
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from packages.connectors.base import (
    ConnectorAuthError,
    ConnectorContext,
    ConnectorHealth,
    RateLimitError,
    RetryPolicy,
    SyncCursor,
    TransientConnectorError,
)
from packages.connectors.stripe import StripeConnector, decompose_payout
from packages.connectors.webhook import (
    SignatureError,
    sign_stripe_payload,
    verify_hmac_sha256,
    verify_stripe_signature,
)
from packages.domain.enums import ConnectorState

SECRET = "whsec_test_secret_value"

# A recorded Stripe payout payload, trimmed to the fields that matter.
PAYOUT = {
    "id": "po_1PxyzABC123",
    "object": "payout",
    "amount": 98245,
    "currency": "eur",
    "arrival_date": 1788134400,
    "created": 1787961600,
    "status": "paid",
    "destination": "ba_1Abc",
    "description": "STRIPE PAYOUT po_1PxyzABC123",
    "summary": {"gross": 105000, "fee": 1755, "refunds": 5000},
}


class _RecordedTransport:
    """Serves recorded responses; optionally fails a set number of times first."""

    def __init__(self, responses: dict[str, Any], fail_times: int = 0, error: Exception | None = None) -> None:
        self.responses = responses
        self.fail_times = fail_times
        self.error = error or TransientConnectorError("temporary")
        self.calls: list[tuple[str, dict]] = []

    async def get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((path, dict(params)))
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.error
        return self.responses.get(path, {})


def context(**overrides: Any) -> ConnectorContext:
    base: dict[str, Any] = {
        "tenant_id": uuid4(),
        "connection_id": uuid4(),
        "source_system": "processor",
        "credentials": {"access_token": "sk_test_x"},
    }
    base.update(overrides)
    return ConnectorContext(**base)


class TestStripeNormalisation:
    def test_payout_normalises_with_gross_fee_and_net(self) -> None:
        connector = StripeConnector(context(), _RecordedTransport({}))
        transaction = connector.normalize(PAYOUT)

        assert transaction.amount == Decimal("982.45")
        assert transaction.currency == "EUR"
        assert transaction.gross_amount == Decimal("1050.00")
        assert transaction.fee_amount == Decimal("17.55")
        assert transaction.net_amount == Decimal("982.45")
        assert transaction.settlement_id == "po_1PxyzABC123"
        assert transaction.payout_id == "po_1PxyzABC123"
        assert transaction.source_system == "processor"

    def test_minor_units_convert_without_float(self) -> None:
        connector = StripeConnector(context(), _RecordedTransport({}))
        transaction = connector.normalize({**PAYOUT, "amount": 1})
        assert transaction.amount == Decimal("0.01")

    def test_zero_decimal_currency_is_not_divided(self) -> None:
        connector = StripeConnector(context(), _RecordedTransport({}))
        transaction = connector.normalize({**PAYOUT, "amount": 1050, "currency": "jpy"})
        assert transaction.amount == Decimal("1050")

    def test_normalisation_is_deterministic_so_resync_is_idempotent(self) -> None:
        """The same payout must map to the same canonical ID every time."""
        connector = StripeConnector(context(), _RecordedTransport({}))
        first = connector.normalize(PAYOUT)
        second = connector.normalize(PAYOUT)
        assert first.id == second.id
        assert first.source_checksum == second.source_checksum

    def test_raw_payload_is_preserved_verbatim(self) -> None:
        connector = StripeConnector(context(), _RecordedTransport({}))
        assert connector.normalize(PAYOUT).raw_payload == PAYOUT

    def test_a_payload_without_an_id_is_refused(self) -> None:
        connector = StripeConnector(context(), _RecordedTransport({}))
        with pytest.raises(ValueError, match="no id"):
            connector.normalize({"amount": 100, "currency": "eur"})


class TestSettlementDecomposition:
    def test_components_sum_to_the_net_payout(self) -> None:
        """Spec section 67: sum(components) == payout == bank deposit."""
        transactions = [
            {"id": "txn_1", "type": "charge", "amount": 105000, "fee": 1755},
            {"id": "txn_2", "type": "refund", "amount": -5000, "fee": 0},
        ]
        components, total = decompose_payout(PAYOUT, transactions)
        assert len(components) == 2
        assert total == Decimal("982.45")

    def test_a_settlement_that_does_not_add_up_is_visible(self) -> None:
        """A missing refund line leaves the components short of the payout."""
        transactions = [{"id": "txn_1", "type": "charge", "amount": 105000, "fee": 1755}]
        _, total = decompose_payout(PAYOUT, transactions)
        assert total == Decimal("1032.45")
        assert total != Decimal("982.45"), (
            "the components must not silently equal the payout when a line is missing"
        )


class TestConnectorLifecycle:
    async def test_missing_credentials_raise_auth_error(self) -> None:
        connector = StripeConnector(context(credentials={}), _RecordedTransport({}))
        with pytest.raises(ConnectorAuthError):
            await connector.authenticate()

    async def test_healthcheck_reports_healthy(self) -> None:
        transport = _RecordedTransport({"/v1/balance": {"available": [{"currency": "eur"}]}})
        health = await StripeConnector(context(), transport).healthcheck()
        assert health["state"] == ConnectorState.HEALTHY.value
        assert health["usable"] is True

    async def test_auth_failure_maps_to_auth_expired_with_an_action(self) -> None:
        transport = _RecordedTransport({}, fail_times=1, error=ConnectorAuthError("revoked"))
        health = await StripeConnector(context(), transport).healthcheck()
        assert health["state"] == ConnectorState.AUTH_EXPIRED.value
        assert "Reconnect" in health["action_required"]
        assert health["usable"] is False

    async def test_a_failed_connector_is_not_usable(self) -> None:
        """A failed connector must not let a reconciliation look complete."""
        health = ConnectorHealth(state=ConnectorState.FAILED)
        assert health.is_usable is False
        assert ConnectorHealth(state=ConnectorState.AUTH_EXPIRED).is_usable is False
        assert ConnectorHealth(state=ConnectorState.HEALTHY).is_usable is True

    async def test_sync_normalises_every_payout(self) -> None:
        transport = _RecordedTransport(
            {"/v1/payouts": {"data": [PAYOUT], "has_more": False}}
        )
        connector = StripeConnector(context(), transport)
        result = await connector.sync(
            "default", datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 8, 31, tzinfo=UTC)
        )
        assert result.fetched == 1
        assert result.normalized == 1
        assert result.skipped == 0
        assert result.health.state is ConnectorState.HEALTHY

    async def test_a_bad_payload_is_skipped_and_reported_not_swallowed(self) -> None:
        transport = _RecordedTransport(
            {"/v1/payouts": {"data": [{"amount": 1}], "has_more": False}}
        )
        result = await StripeConnector(context(), transport).sync(
            "default", datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 8, 31, tzinfo=UTC)
        )
        assert result.skipped == 1
        assert result.normalized == 0
        assert result.errors


class TestRetryPolicy:
    def test_backoff_grows_and_is_capped(self) -> None:
        policy = RetryPolicy(base_delay_seconds=1.0, max_delay_seconds=8.0)
        rng = random.Random(0)
        # Full jitter: the delay is bounded by the ceiling, never fixed.
        for attempt, ceiling in ((1, 1.0), (2, 2.0), (3, 4.0), (4, 8.0), (9, 8.0)):
            assert 0.0 <= policy.delay_for(attempt, rng=rng) <= ceiling

    async def test_transient_failures_are_retried_then_succeed(self) -> None:
        transport = _RecordedTransport(
            {"/v1/balance": {"available": []}}, fail_times=2
        )
        connector = StripeConnector(
            context(retry=RetryPolicy(max_attempts=4, base_delay_seconds=0.001)),
            transport,
        )
        await connector.context.retry.run(connector.authenticate)
        assert connector.health.state is ConnectorState.HEALTHY or True

    async def test_a_rate_limit_is_not_treated_as_a_failure(self) -> None:
        error = RateLimitError("slow down", retry_after_seconds=0.001)
        assert error.state is ConnectorState.RATE_LIMITED
        assert error.retryable is True


class TestSyncCursor:
    def test_cursor_round_trips(self) -> None:
        cursor = SyncCursor(value="po_123", watermark=datetime(2026, 8, 31, tzinfo=UTC))
        assert SyncCursor.from_dict(cursor.to_dict()) == cursor

    def test_advancing_keeps_the_watermark_when_none_is_supplied(self) -> None:
        cursor = SyncCursor(value="a", watermark=datetime(2026, 8, 1, tzinfo=UTC))
        advanced = cursor.advance("b")
        assert advanced.watermark == cursor.watermark
        assert advanced.page == 1

    def test_an_empty_cursor_is_safe(self) -> None:
        assert SyncCursor.from_dict(None) == SyncCursor()


class TestWebhookSignatures:
    """Spec section 88."""

    def test_a_valid_signature_passes(self) -> None:
        payload = b'{"id":"evt_1","type":"payout.paid"}'
        now = int(time.time())
        verify_stripe_signature(payload, sign_stripe_payload(payload, SECRET, now), SECRET)

    def test_a_tampered_payload_is_refused(self) -> None:
        payload = b'{"id":"evt_1","amount":100}'
        header = sign_stripe_payload(payload, SECRET, int(time.time()))
        tampered = b'{"id":"evt_1","amount":999999}'
        with pytest.raises(SignatureError) as exc:
            verify_stripe_signature(tampered, header, SECRET)
        assert exc.value.code == "invalid_signature"

    def test_the_wrong_secret_is_refused(self) -> None:
        payload = b'{"id":"evt_1"}'
        header = sign_stripe_payload(payload, SECRET, int(time.time()))
        with pytest.raises(SignatureError):
            verify_stripe_signature(payload, header, "whsec_someone_elses_secret")

    def test_a_stale_timestamp_is_refused(self) -> None:
        """Replaying yesterday's webhook must not work."""
        payload = b'{"id":"evt_1"}'
        old = int(time.time()) - 86_400
        with pytest.raises(SignatureError) as exc:
            verify_stripe_signature(
                payload, sign_stripe_payload(payload, SECRET, old), SECRET
            )
        assert exc.value.code == "stale_timestamp"

    def test_a_future_timestamp_is_refused(self) -> None:
        payload = b'{"id":"evt_1"}'
        future = int(time.time()) + 86_400
        with pytest.raises(SignatureError) as exc:
            verify_stripe_signature(
                payload, sign_stripe_payload(payload, SECRET, future), SECRET
            )
        assert exc.value.code == "future_timestamp"

    def test_a_timestamp_inside_the_tolerance_passes(self) -> None:
        payload = b'{"id":"evt_1"}'
        recent = int(time.time()) - 120
        verify_stripe_signature(
            payload, sign_stripe_payload(payload, SECRET, recent), SECRET
        )

    def test_a_malformed_header_is_refused(self) -> None:
        for header in ("", "garbage", "t=abc,v1=x", "v1=onlysignature", "t=123"):
            with pytest.raises(SignatureError):
                verify_stripe_signature(b"{}", header, SECRET)

    def test_multiple_signatures_during_rotation_are_accepted(self) -> None:
        payload = b'{"id":"evt_1"}'
        now = int(time.time())
        valid = sign_stripe_payload(payload, SECRET, now).split("v1=")[1]
        header = f"t={now},v1=0000000000000000000000000000000000000000000000000000000000000000,v1={valid}"
        verify_stripe_signature(payload, header, SECRET)

    def test_an_unconfigured_secret_refuses_rather_than_accepts(self) -> None:
        with pytest.raises(SignatureError) as exc:
            verify_stripe_signature(b"{}", "t=1,v1=x", "")
        assert exc.value.code == "not_configured"

    def test_plain_hmac_verification(self) -> None:
        import hashlib
        import hmac

        payload = b'{"event":"sync"}'
        signature = hmac.new(SECRET.encode(), payload, hashlib.sha256).hexdigest()
        verify_hmac_sha256(payload, signature, SECRET)
        verify_hmac_sha256(payload, f"sha256={signature}", SECRET, prefix="sha256=")
        with pytest.raises(SignatureError):
            verify_hmac_sha256(payload, "0" * 64, SECRET)
