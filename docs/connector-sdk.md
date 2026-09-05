# Connector SDK

Connectors acquire source facts; they do not make reconciliation decisions. Every connector returns raw source records and maps them conservatively into `CanonicalTransaction` objects while preserving the original payload.

## Contract

`packages.connectors.base.Connector` is an abstract base class constructed with a `ConnectorContext`. Implementations provide:

```python
class Connector(ABC):
    async def authenticate(self) -> None: ...
    async def list_accounts(self) -> list[dict]: ...
    def fetch_transactions(
        self,
        account_id: str,
        start: datetime,
        end: datetime,
        cursor: str | None = None,
    ) -> AsyncIterator[dict]: ...
    def fetch_documents(
        self,
        start: datetime,
        end: datetime,
    ) -> AsyncIterator[dict]: ...
    async def healthcheck(self) -> dict: ...
    def normalize(self, raw: dict) -> CanonicalTransaction: ...
```

The base implementation also coordinates sync through `sync`, retry policy, state tracking, cursor/checkpoint handling, backfill windows, and standardized connector errors. Authentication failures, rate limits, transient failures, and permanent failures are distinct outcomes so workers can retry only when safe.

## Twelve requirements

Every production connector must satisfy all twelve requirements from specification section 9:

1. **Idempotent sync:** replaying the same page does not create duplicate source or transaction rows; source ID plus checksum participates in database uniqueness.
2. **Cursor/checkpoint:** return and persist a cursor only after a page succeeds.
3. **Backfill:** accept explicit start/end windows and retrieve historical records.
4. **Incremental updates:** continue from a cursor and handle changed source records as new checksummed versions.
5. **Source IDs:** retain stable provider account, record, transaction, batch, settlement, and payout identifiers where available.
6. **Retry strategy:** classify retryable errors and use bounded attempts/backoff rather than infinite loops.
7. **Rate-limit handling:** recognize provider throttling and honor retry timing through connector error metadata.
8. **Token refresh:** authenticate or refresh before acquisition without storing plaintext credentials in ordinary tables.
9. **Deletion/reversal behavior:** represent reversals or tombstones explicitly; never erase previously imported financial facts.
10. **Raw-payload preservation:** retain the exact upstream object in `raw_payload` and immutable `source_records`.
11. **Reconciliation-relevant mapping:** map dates, amount/currency, references, counterparty, fees, invoice, batch, settlement, and source lineage conservatively.
12. **Health state:** report healthy, degraded, retrying, or failed status with safe operational detail and last-attempt/success timestamps.

An empty result must mean a successful, genuinely empty upstream page. Unsupported connectors must never return an empty collection as a placeholder because that would look like a healthy sync of a quiet account.

## Stripe example

`StripeConnector` in `packages/connectors/stripe/connector.py` is the reference API implementation. Its transport is injected, making HTTP behavior testable without networking. A typical use is:

```python
from datetime import UTC, datetime
from packages.connectors.base import ConnectorContext
from packages.connectors.stripe.connector import StripeConnector

context = ConnectorContext(
    tenant_id=tenant_id,
    connection_id=connection_id,
    source_system="stripe",
    credentials={"access_token": access_token},
)
connector = StripeConnector(context=context, transport=stripe_transport)

await connector.authenticate()
accounts = await connector.list_accounts()
async for raw in connector.fetch_transactions(
    accounts[0]["id"],
    datetime(2026, 8, 1, tzinfo=UTC),
    datetime(2026, 9, 1, tzinfo=UTC),
):
    transaction = connector.normalize(raw)
    # Persist through the ingestion/service boundary, not from the connector.
```

The implementation pages Stripe payouts, preserves provider IDs and raw payload, maps arrival/creation dates without merging date concepts, represents money using `Decimal`, and exposes provider health. Its internal `_call` converts transport failures into the connector error taxonomy so the worker can update state and decide whether to retry.

## Adding a connector

1. Create a package under `packages/connectors/<provider>/` and subclass `Connector`.
2. Define an injectable transport rather than embedding untestable global HTTP calls.
3. Implement authentication and credential refresh using the encrypted credential service.
4. Map the provider's account discovery, transaction pages, document pages, cursor, and health semantics.
5. Preserve raw payload and stable source IDs. Use `Decimal` for amounts and keep all four date meanings distinct.
6. Translate provider errors into authentication, rate-limit, transient, or permanent connector errors.
7. Add deterministic tests for pagination, cursor advancement, idempotent replay, rate limiting, partial-page failure, reversal behavior, normalization, and health transitions.
8. Register the connector in the sync worker only after the implementation and tests are complete.

QuickBooks, Xero, generic bank, and SFTP packages currently declare `IMPLEMENTED = False`. The worker deliberately raises `NotImplementedError` for them. Do not replace that behavior with empty data: a loud unsupported state is operationally honest, while an empty success could cause finance users to believe an account was reconciled against complete data.
