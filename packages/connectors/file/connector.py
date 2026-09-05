"""File connector: CSV/XLSX uploads and SFTP drops, behind the same contract.

Real finance teams work from exports, so a file *is* a source system. Modelling
it as a connector rather than as a special case means uploads get the same
cursor, idempotency and health treatment as an API integration.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import datetime
from typing import Any
from uuid import uuid5

from packages.connectors.base import Connector, ConnectorContext
from packages.connectors.stripe.connector import STRIPE_NAMESPACE
from packages.domain.dates import utc_now
from packages.domain.models.transaction import CanonicalTransaction, compute_checksum
from packages.ingestion.mapping import SourceMapping, apply_mapping
from packages.ingestion.parsers import parse_any

__all__ = ["FileConnector"]


class FileConnector(Connector):
    """Reads tabular files supplied by a fetcher callable.

    The fetcher abstracts where the bytes come from - an upload, an SFTP poll, a
    bucket listing - so the same connector serves all three.
    """

    name = "file"
    supports_documents = False

    def __init__(
        self,
        context: ConnectorContext,
        *,
        mapping: SourceMapping,
        fetcher: Callable[[datetime, datetime], list[tuple[str, bytes]]],
    ) -> None:
        super().__init__(context)
        self._mapping = mapping
        self._fetcher = fetcher

    async def authenticate(self) -> None:
        """Nothing to authenticate: the caller already had the bytes."""
        return None

    async def list_accounts(self) -> list[dict]:
        return [
            {
                "id": self.context.config.get("account_id", "default"),
                "type": self.context.source_system,
            }
        ]

    async def fetch_transactions(
        self,
        account_id: str,
        start: datetime,
        end: datetime,
        cursor: str | None = None,
    ) -> AsyncIterator[dict]:
        """Yield one dict per source row, tagged with its file and row number.

        The row number is part of the identity, so two identical rows in one
        file remain two records - which is what lets duplicate detection see
        them.
        """
        del account_id, cursor
        for filename, data in self._fetcher(start, end):
            table = parse_any(data, filename)
            for index, row in enumerate(table.rows, start=1):
                yield {"__file__": filename, "__row__": index, **row}

    async def fetch_documents(
        self, start: datetime, end: datetime
    ) -> AsyncIterator[dict]:
        del start, end
        return
        yield {}  # pragma: no cover - makes this an async generator

    async def healthcheck(self) -> dict:
        self.record_success()
        return self._health.to_dict()

    def normalize(self, raw: dict) -> CanonicalTransaction:
        payload = {k: v for k, v in raw.items() if not k.startswith("__")}
        result = apply_mapping(
            [payload],
            self._mapping,
            tenant_id=self.context.tenant_id,
            source_connection_id=self.context.connection_id,
            collect_lineage=False,
        )
        if result.row_errors:
            _, reason = result.row_errors[0]
            raise ValueError(reason)

        transaction = result.transactions[0]
        record_id = f"{raw.get('__file__', 'file')}:{raw.get('__row__', 0)}"
        return transaction.model_copy(
            update={
                "id": uuid5(
                    STRIPE_NAMESPACE, f"{self.context.connection_id}:{record_id}"
                ),
                "source_record_id": record_id,
                "source_checksum": compute_checksum(payload),
                "imported_at": utc_now(),
            }
        )


def static_fetcher(files: list[tuple[str, bytes]]) -> Any:
    """A fetcher over a fixed list. Used by tests and by ``seed_demo``."""

    def fetch(start: datetime, end: datetime) -> list[tuple[str, bytes]]:
        del start, end
        return files

    return fetch
