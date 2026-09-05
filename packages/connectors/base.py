"""The connector contract (spec section 9).

Every integration implements the same interface, so the platform's behaviour
does not change with the source. The requirements the spec lists - idempotent
sync, cursors, backfill, retry, rate limits, token refresh, reversal handling,
raw-payload preservation, health state - are represented here as concrete
mechanisms rather than as documentation.
"""

from __future__ import annotations

import asyncio
import random
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from packages.domain.dates import utc_now
from packages.domain.enums import ConnectorState
from packages.domain.models.transaction import CanonicalTransaction

__all__ = [
    "Connector",
    "ConnectorAuthError",
    "ConnectorContext",
    "ConnectorError",
    "ConnectorHealth",
    "RateLimitError",
    "RetryPolicy",
    "SyncCursor",
    "SyncResult",
    "TransientConnectorError",
]


class ConnectorError(Exception):
    """Base class for connector failures."""

    state: ConnectorState = ConnectorState.FAILED
    retryable: bool = False


class TransientConnectorError(ConnectorError):
    """A failure worth retrying: a timeout, a 5xx, a dropped connection."""

    state = ConnectorState.DEGRADED
    retryable = True


class RateLimitError(TransientConnectorError):
    """The provider asked us to slow down."""

    state = ConnectorState.RATE_LIMITED

    def __init__(self, message: str, retry_after_seconds: float = 60.0) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class ConnectorAuthError(ConnectorError):
    """Credentials are missing, expired or revoked. A human must act."""

    state = ConnectorState.AUTH_EXPIRED


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Exponential backoff with full jitter.

    Jitter matters: without it every worker that failed during an outage
    retries in lockstep and knocks the provider over again as it recovers.
    """

    max_attempts: int = 5
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 60.0

    def delay_for(self, attempt: int, *, rng: random.Random | None = None) -> float:
        ceiling = min(
            self.max_delay_seconds, self.base_delay_seconds * (2 ** max(attempt - 1, 0))
        )
        source = rng or random
        return source.uniform(0.0, ceiling)

    async def run(self, operation: Any) -> Any:
        """Execute ``operation`` with retries. Non-retryable errors propagate."""
        last: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                return await operation()
            except RateLimitError as exc:
                last = exc
                if attempt == self.max_attempts:
                    break
                await asyncio.sleep(exc.retry_after_seconds)
            except TransientConnectorError as exc:
                last = exc
                if attempt == self.max_attempts:
                    break
                await asyncio.sleep(self.delay_for(attempt))
        raise last if last else ConnectorError("operation failed with no error recorded")


@dataclass(frozen=True, slots=True)
class SyncCursor:
    """A resumable position in the provider's stream.

    Both a cursor and a watermark are kept. The cursor is the provider's own
    pagination token; the watermark is the timestamp we are confident we have
    everything up to. A provider that invalidates cursors can still be resumed
    from the watermark without re-reading history.
    """

    value: str | None = None
    watermark: datetime | None = None
    page: int = 0

    def advance(self, value: str | None, watermark: datetime | None = None) -> SyncCursor:
        return SyncCursor(
            value=value,
            watermark=watermark or self.watermark,
            page=self.page + 1,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "watermark": self.watermark.isoformat() if self.watermark else None,
            "page": self.page,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> SyncCursor:
        if not payload:
            return cls()
        raw = payload.get("watermark")
        return cls(
            value=payload.get("value"),
            watermark=datetime.fromisoformat(raw) if raw else None,
            page=int(payload.get("page", 0)),
        )


@dataclass(slots=True)
class ConnectorHealth:
    """What the user is shown about a connection (spec section 89)."""

    state: ConnectorState = ConnectorState.HEALTHY
    detail: str = ""
    last_success_at: datetime | None = None
    last_attempt_at: datetime | None = None
    next_retry_at: datetime | None = None
    error_category: str | None = None
    action_required: str | None = None

    @property
    def is_usable(self) -> bool:
        """Whether a reconciliation may rely on this connection.

        A failed connector must not silently produce a 'complete'
        reconciliation (spec section 89), so a run that depends on an unusable
        connection is blocked rather than quietly run on stale data.
        """
        return self.state in {ConnectorState.HEALTHY, ConnectorState.DEGRADED}

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "detail": self.detail,
            "last_success_at": (
                self.last_success_at.isoformat() if self.last_success_at else None
            ),
            "last_attempt_at": (
                self.last_attempt_at.isoformat() if self.last_attempt_at else None
            ),
            "next_retry_at": self.next_retry_at.isoformat() if self.next_retry_at else None,
            "error_category": self.error_category,
            "action_required": self.action_required,
            "usable": self.is_usable,
        }


@dataclass(slots=True)
class ConnectorContext:
    """Everything a connector needs that is not provider-specific."""

    tenant_id: UUID
    connection_id: UUID
    source_system: str
    config: dict[str, Any] = field(default_factory=dict)
    cursor: SyncCursor = field(default_factory=SyncCursor)
    credentials: dict[str, str] = field(default_factory=dict)
    retry: RetryPolicy = field(default_factory=RetryPolicy)


@dataclass(slots=True)
class SyncResult:
    fetched: int = 0
    normalized: int = 0
    skipped: int = 0
    cursor: SyncCursor = field(default_factory=SyncCursor)
    health: ConnectorHealth = field(default_factory=ConnectorHealth)
    errors: list[str] = field(default_factory=list)


class Connector(ABC):
    """The contract every integration implements."""

    name: str = "abstract"
    supports_documents: bool = False
    supports_backfill: bool = True

    def __init__(self, context: ConnectorContext) -> None:
        self.context = context
        self._health = ConnectorHealth()

    # -- lifecycle ---------------------------------------------------------
    @abstractmethod
    async def authenticate(self) -> None:
        """Establish or refresh credentials.

        Raises :class:`ConnectorAuthError` when a human must reconnect.
        """

    @abstractmethod
    async def list_accounts(self) -> list[dict]:
        ...

    @abstractmethod
    def fetch_transactions(
        self,
        account_id: str,
        start: datetime,
        end: datetime,
        cursor: str | None = None,
    ) -> AsyncIterator[dict]:
        """Yield raw provider payloads. Never normalised, never filtered."""

    @abstractmethod
    def fetch_documents(
        self,
        start: datetime,
        end: datetime,
    ) -> AsyncIterator[dict]:
        ...

    @abstractmethod
    async def healthcheck(self) -> dict:
        ...

    @abstractmethod
    def normalize(self, raw: dict) -> CanonicalTransaction:
        """Map one raw payload to the canonical model.

        Implementations must preserve the untouched payload in ``raw_payload``
        and derive ``source_record_id`` from a provider identifier that is
        stable across syncs - that stability is what makes sync idempotent.
        """

    # -- shared behaviour --------------------------------------------------
    @property
    def health(self) -> ConnectorHealth:
        return self._health

    def record_success(self) -> None:
        now = utc_now()
        self._health = ConnectorHealth(
            state=ConnectorState.HEALTHY,
            last_success_at=now,
            last_attempt_at=now,
        )

    def record_failure(self, error: Exception) -> ConnectorHealth:
        """Translate an exception into user-facing health state."""
        now = utc_now()
        if isinstance(error, ConnectorError):
            state = error.state
        else:
            state = ConnectorState.FAILED

        action = {
            ConnectorState.AUTH_EXPIRED: (
                "Reconnect this integration: its credentials are no longer valid."
            ),
            ConnectorState.RATE_LIMITED: (
                "No action needed. The provider is rate limiting us and the sync "
                "will resume automatically."
            ),
            ConnectorState.FAILED: (
                "Review the error and retry the sync. Reconciliations depending "
                "on this connection are blocked until it succeeds."
            ),
        }.get(state)

        self._health = ConnectorHealth(
            state=state,
            detail=str(error),
            last_success_at=self._health.last_success_at,
            last_attempt_at=now,
            error_category=type(error).__name__,
            action_required=action,
        )
        return self._health

    async def sync(
        self,
        account_id: str,
        start: datetime,
        end: datetime,
    ) -> SyncResult:
        """Fetch and normalise a window. Idempotent by construction.

        Nothing is deduplicated here: the canonical identity tuple and the
        database's unique constraint decide what is new. A connector that tried
        to dedupe itself would be guessing.
        """
        result = SyncResult(cursor=self.context.cursor)
        try:
            await self.context.retry.run(self.authenticate)
            async for payload in self.fetch_transactions(
                account_id, start, end, self.context.cursor.value
            ):
                result.fetched += 1
                try:
                    self.normalize(payload)
                except (ValueError, TypeError, KeyError) as exc:
                    result.skipped += 1
                    result.errors.append(f"payload could not be normalised: {exc}")
                    continue
                result.normalized += 1
            result.cursor = self.context.cursor.advance(
                self.context.cursor.value, watermark=end
            )
            self.record_success()
        except Exception as exc:
            result.health = self.record_failure(exc)
            result.errors.append(str(exc))
            return result

        result.health = self._health
        return result
