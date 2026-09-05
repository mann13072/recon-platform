from packages.ingestion.parsers.csv_parser import (
    ParsedTable,
    detect_delimiter,
    detect_encoding,
    parse_csv,
)
from packages.ingestion.parsers.json_parser import parse_json
from packages.ingestion.parsers.xlsx_parser import list_sheets, parse_xlsx

__all__ = [
    "ParsedTable",
    "detect_delimiter",
    "detect_encoding",
    "list_sheets",
    "parse_csv",
    "parse_json",
    "parse_xlsx",
]


def parse_any(data: bytes, filename: str, **kwargs: object) -> ParsedTable:
    """Dispatch to the right parser based on the file extension."""
    lowered = filename.lower()
    if lowered.endswith((".xlsx", ".xlsm")):
        return parse_xlsx(data, **kwargs)  # type: ignore[arg-type]
    if lowered.endswith((".json", ".jsonl", ".ndjson")):
        return parse_json(data, **kwargs)  # type: ignore[arg-type]
    return parse_csv(data, **kwargs)  # type: ignore[arg-type]
