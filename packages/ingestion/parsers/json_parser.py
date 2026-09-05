"""JSON / JSONL parsing for API payload dumps."""

from __future__ import annotations

import json
from typing import Any

from packages.ingestion.parsers.csv_parser import ParsedTable

__all__ = ["parse_json"]


def _flatten(obj: Any, prefix: str = "", out: dict[str, str] | None = None) -> dict[str, str]:
    """Flatten nested objects to dotted keys so they can be mapped like columns.

    Lists are JSON-encoded rather than exploded: exploding would change the row
    count, and a row in an accounting import must correspond to one source row.
    """
    if out is None:
        out = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            _flatten(value, f"{prefix}.{key}" if prefix else str(key), out)
    elif isinstance(obj, list):
        out[prefix] = json.dumps(obj, separators=(",", ":"), default=str)
    elif obj is None:
        out[prefix] = ""
    elif isinstance(obj, bool):
        out[prefix] = "TRUE" if obj else "FALSE"
    else:
        out[prefix] = str(obj)
    return out


def parse_json(data: bytes, *, records_path: str | None = None) -> ParsedTable:
    """Parse a JSON array, a JSON object containing an array, or JSONL."""
    text = data.decode("utf-8-sig", errors="strict").strip()
    if not text:
        return ParsedTable(columns=[], rows=[], encoding="utf-8", delimiter="")

    records: list[Any]
    if text.startswith("["):
        records = json.loads(text)
    elif text.startswith("{"):
        payload = json.loads(text)
        if records_path:
            cursor: Any = payload
            for part in records_path.split("."):
                cursor = cursor[part]
            records = cursor
        else:
            arrays = [v for v in payload.values() if isinstance(v, list)]
            if len(arrays) == 1:
                records = arrays[0]
            elif not arrays:
                records = [payload]
            else:
                raise ValueError("JSON object contains multiple arrays; specify records_path")
    else:
        records = [json.loads(line) for line in text.splitlines() if line.strip()]

    flat_rows = [_flatten(record) for record in records]
    columns: list[str] = []
    seen: set[str] = set()
    for row in flat_rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                columns.append(key)

    normalised = [{col: row.get(col, "") for col in columns} for row in flat_rows]
    return ParsedTable(
        columns=columns,
        rows=normalised,
        encoding="utf-8",
        delimiter="",
        total_rows=len(normalised),
    )
