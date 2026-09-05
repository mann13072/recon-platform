"""Stripe connector (spec section 10 MVP source priority, section 67).

The HTTP layer is injected, so this is tested against recorded fixtures rather
than a live account. No Stripe SDK is imported.

The important modelling choice is the settlement decomposition of section 67:

    Sales + adjustments - refunds - chargebacks - fees - reserves = net settlement

A payout is normalised with its gross, fee and net amounts intact, so the
deterministic identity ``gross - fee == net`` can be checked. That is strictly
better than fuzzy matching a bank deposit against a payout.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID, uuid5

from packages.connectors.base import (
    Connector,
    ConnectorAuthError,
    ConnectorContext,
    RateLimitError,
    TransientConnectorError,
)
from packages.domain.dates import utc_now
from packages.domain.models.transaction import CanonicalTransaction, compute_checksum
from packages.ingestion.normalization import (
    normalize_bank_description,
    normalize_name,
    normalize_reference,
)

__all__ = ["StripeConnector", "StripeTransport", "SettlementComponent", "decompose_payout"]

# Stripe stores money in minor units; zero-decimal currencies are the exception.
_ZERO_DECIMAL = frozenset(
    {"BIF", "CLP", "DJF", "GNF", "JPY", "KMF", "KRW", "MGA", "PYG", "RWF",
     "UGX", "VND", "VUV", "XAF", "XOF", "XPF"}
)

STRIPE_NAMESPACE = UUID("2a6b1f2e-7c44-4d19-b0c9-9d4e2a5f1c33")


class StripeTransport(Protocol):
    """The HTTP surface this connector needs."""

    async def get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        ...


def _from_minor_units(amount: int, currency: str) -> Decimal:
    exponent = 0 if currency.upper() in _ZERO_DECIMAL else 2
    return Decimal(amount).scaleb(-exponent)


class SettlementComponent:
    """One line of a payout's composition (spec section 67)."""

    __slots__ = ("settlement_id", "type", "amount", "currency", "source_transaction_id")

    def __init__(
        self,
        settlement_id: str,
        type: str,  # noqa: A002 - the spec names this field 'type'
        amount: Decimal,
        currency: str,
        source_transaction_id: str,
    ) -> None:
        self.settlement_id = settlement_id
        self.type = type
        self.amount = amount
        self.currency = currency
        self.source_transaction_id = source_transaction_id

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"SettlementComponent({self.settlement_id}, {self.type}, "
            f"{self.amount} {self.currency})"
        )


def decompose_payout(
    payout: dict[str, Any], transactions: list[dict[str, Any]]
) -> tuple[list[SettlementComponent], Decimal]:
    """Break a payout into its components and return their exact total.

    The caller compares the total against the payout's own net amount. When
    they disagree the payout is not reconcilable and belongs in an exception -
    the platform never quietly accepts a settlement that does not add up.
    """
    currency = str(payout.get("currency", "eur")).upper()
    components: list[SettlementComponent] = []
    total = Decimal("0")

    for entry in transactions:
        amount = _from_minor_units(int(entry.get("amount", 0)), currency)
        fee = _from_minor_units(int(entry.get("fee", 0)), currency)
        net = amount - fee

        components.append(
            SettlementComponent(
                settlement_id=str(payout.get("id", "")),
                type=str(entry.get("type", "unknown")),
                amount=net,
                currency=currency,
                source_transaction_id=str(entry.get("id", "")),
            )
        )
        total += net

    return components, total


class StripeConnector(Connector):
    name = "stripe"
    supports_documents = False

    def __init__(self, context: ConnectorContext, transport: StripeTransport) -> None:
        super().__init__(context)
        self._transport = transport

    # -- lifecycle ---------------------------------------------------------
    async def authenticate(self) -> None:
        if not self.context.credentials.get("access_token"):
            raise ConnectorAuthError(
                "This Stripe connection has no access token. Reconnect it."
            )

    async def list_accounts(self) -> list[dict]:
        response = await self._call("/v1/balance", {})
        return [
            {
                "id": self.context.config.get("stripe_account_id", "default"),
                "currency": str(entry.get("currency", "eur")).upper(),
                "type": "stripe_balance",
            }
            for entry in response.get("available", [{"currency": "eur"}])
        ]

    async def fetch_transactions(
        self,
        account_id: str,
        start: datetime,
        end: datetime,
        cursor: str | None = None,
    ) -> AsyncIterator[dict]:
        """Page through payouts in the window, newest first.

        Stripe pages with ``starting_after``; the cursor carries that value so a
        failed sync resumes rather than restarting.
        """
        params: dict[str, Any] = {
            "limit": 100,
            "arrival_date[gte]": int(start.replace(tzinfo=UTC).timestamp()),
            "arrival_date[lte]": int(end.replace(tzinfo=UTC).timestamp()),
        }
        if cursor:
            params["starting_after"] = cursor

        while True:
            page = await self._call("/v1/payouts", params)
            payouts = page.get("data", [])
            if not payouts:
                return

            for payout in payouts:
                yield payout

            if not page.get("has_more"):
                return
            params["starting_after"] = payouts[-1]["id"]
            self.context.cursor = self.context.cursor.advance(
                payouts[-1]["id"], watermark=end
            )

    async def fetch_documents(
        self, start: datetime, end: datetime
    ) -> AsyncIterator[dict]:
        """Stripe exposes no documents relevant to reconciliation."""
        del start, end
        return
        yield {}  # pragma: no cover - makes this an async generator

    async def healthcheck(self) -> dict:
        try:
            await self._call("/v1/balance", {})
        except Exception as exc:
            return self.record_failure(exc).to_dict()
        self.record_success()
        return self._health.to_dict()

    # -- normalisation -----------------------------------------------------
    def normalize(self, raw: dict) -> CanonicalTransaction:
        payout_id = str(raw.get("id") or "")
        if not payout_id:
            raise ValueError("payout payload has no id")

        currency = str(raw.get("currency", "eur")).upper()
        net = _from_minor_units(int(raw.get("amount", 0)), currency)

        summary = raw.get("summary") or {}
        gross = (
            _from_minor_units(int(summary["gross"]), currency)
            if "gross" in summary
            else None
        )
        fee = (
            _from_minor_units(int(summary["fee"]), currency)
            if "fee" in summary
            else None
        )

        arrival = raw.get("arrival_date")
        arrival_date = (
            datetime.fromtimestamp(int(arrival), tz=UTC).date() if arrival else None
        )
        created = raw.get("created")
        created_date = (
            datetime.fromtimestamp(int(created), tz=UTC).date() if created else None
        )

        description = str(raw.get("description") or f"STRIPE PAYOUT {payout_id}")
        counterparty = raw.get("destination_name") or "STRIPE"

        return CanonicalTransaction(
            # Deterministic from the provider ID, so re-syncing the same payout
            # produces the same canonical row rather than a duplicate.
            id=uuid5(STRIPE_NAMESPACE, f"{self.context.connection_id}:{payout_id}"),
            tenant_id=self.context.tenant_id,
            source_system="processor",
            source_connection_id=self.context.connection_id,
            source_record_id=payout_id,
            source_account_id=str(raw.get("destination") or "") or None,
            transaction_type="payout",
            transaction_date=arrival_date or created_date,
            settlement_date=arrival_date,
            posting_date=created_date,
            amount=net,
            currency=currency,
            debit_credit="debit" if net >= 0 else "credit",
            description=description,
            normalized_description=normalize_bank_description(description),
            reference=payout_id,
            normalized_reference=normalize_reference(payout_id),
            external_transaction_id=payout_id,
            settlement_id=payout_id,
            payout_id=payout_id,
            counterparty_name=str(counterparty),
            normalized_counterparty=normalize_name(str(counterparty)),
            gross_amount=gross,
            fee_amount=fee,
            net_amount=net,
            status=str(raw.get("status") or "") or None,
            raw_payload=dict(raw),
            source_checksum=compute_checksum(dict(raw)),
            imported_at=utc_now(),
        )

    # -- transport ---------------------------------------------------------
    async def _call(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            return await self._transport.get(path, params)
        except RateLimitError:
            raise
        except ConnectorAuthError:
            raise
        except Exception as exc:
            raise TransientConnectorError(f"Stripe request to {path} failed: {exc}") from exc
