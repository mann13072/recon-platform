"""CSV parsing with encoding and delimiter detection (spec section 10).

Returns rows as ``dict[str, str]`` keeping every value as text. Type coercion
happens later, during mapping, where the user has confirmed what each column
means. Guessing types at parse time is how "01/02" becomes the wrong date.
"""

from __future__ import annotations

import codecs
import csv
import io
from dataclasses import dataclass, field

__all__ = ["ParsedTable", "detect_delimiter", "detect_encoding", "parse_csv"]

# Ordered by likelihood in finance exports. UTF-8 first; cp1252 is the common
# Windows/Excel fallback and never fails to decode, so it must come last.
_ENCODING_CANDIDATES = ("utf-8-sig", "utf-8", "utf-16", "iso-8859-1", "cp1252")

_BOMS: tuple[tuple[bytes, str], ...] = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


@dataclass(slots=True)
class ParsedTable:
    """A parsed tabular source, before any mapping is applied."""

    columns: list[str]
    rows: list[dict[str, str]]
    encoding: str
    delimiter: str
    sheet_name: str | None = None
    total_rows: int = 0
    skipped_rows: list[tuple[int, str]] = field(default_factory=list)

    def sample(self, column: str, limit: int = 200) -> list[str]:
        values: list[str] = []
        for row in self.rows:
            value = (row.get(column) or "").strip()
            if value:
                values.append(value)
            if len(values) >= limit:
                break
        return values


def detect_encoding(data: bytes) -> str:
    """Detect a file encoding, preferring an explicit BOM."""
    for bom, encoding in _BOMS:
        if data.startswith(bom):
            return encoding

    for encoding in _ENCODING_CANDIDATES:
        try:
            data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        return encoding
    return "cp1252"


def detect_delimiter(text: str) -> str:
    """Detect the delimiter from the header and first data rows."""
    sample = "\n".join(text.splitlines()[:20])
    if not sample:
        return ","
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        return dialect.delimiter
    except csv.Error:
        # Sniffer fails on single-column files and on some quoted narratives.
        # Fall back to whichever candidate appears most often in the header.
        header = sample.splitlines()[0]
        counts = {d: header.count(d) for d in (",", ";", "\t", "|")}
        best = max(counts, key=lambda d: counts[d])
        return best if counts[best] else ","


def parse_csv(
    data: bytes,
    *,
    encoding: str | None = None,
    delimiter: str | None = None,
    max_rows: int | None = None,
) -> ParsedTable:
    """Parse CSV bytes into a :class:`ParsedTable`.

    Rows whose field count does not match the header are recorded in
    ``skipped_rows`` rather than being silently padded, because a shifted row in
    a bank export usually means the file is wrong, not that we should improvise.
    """
    resolved_encoding = encoding or detect_encoding(data)
    text = data.decode(resolved_encoding, errors="strict")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    resolved_delimiter = delimiter or detect_delimiter(text)

    reader = csv.reader(io.StringIO(text, newline=""), delimiter=resolved_delimiter)
    try:
        header = next(reader)
    except StopIteration:
        return ParsedTable(
            columns=[],
            rows=[],
            encoding=resolved_encoding,
            delimiter=resolved_delimiter,
            total_rows=0,
        )

    columns = _dedupe_headers([h.strip() for h in header])
    rows: list[dict[str, str]] = []
    skipped: list[tuple[int, str]] = []
    total = 0

    for line_number, raw_row in enumerate(reader, start=2):
        if not any(cell.strip() for cell in raw_row):
            continue
        total += 1
        if len(raw_row) != len(columns):
            skipped.append((line_number, f"expected {len(columns)} fields, found {len(raw_row)}"))
            continue
        rows.append({col: (raw_row[i] or "").strip() for i, col in enumerate(columns)})
        if max_rows is not None and len(rows) >= max_rows:
            break

    return ParsedTable(
        columns=columns,
        rows=rows,
        encoding=resolved_encoding,
        delimiter=resolved_delimiter,
        total_rows=total,
        skipped_rows=skipped,
    )


def _dedupe_headers(headers: list[str]) -> list[str]:
    """Make header names unique and non-empty without losing the original."""
    seen: dict[str, int] = {}
    result: list[str] = []
    for index, header in enumerate(headers):
        name = header or f"column_{index + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 0
        result.append(name)
    return result
