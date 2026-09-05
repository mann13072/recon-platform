"""Column mapping: source columns -> canonical fields (spec section 10).

Two responsibilities:

1. *Suggest* a mapping from a file profile, so onboarding is not manual typing.
2. *Apply* a user-confirmed mapping, producing canonical transactions plus
   per-field lineage (spec section 59).

Suggestions are only ever suggestions. Nothing is ingested until the user
confirms, because a wrong amount-column guess is a silent financial error.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from packages.domain.dates import parse_date, utc_now
from packages.domain.models.transaction import CanonicalTransaction, compute_checksum
from packages.ingestion.normalization import (
    NORMALIZER_VERSIONS,
    NumberFormat,
    extract_references,
    normalize_bank_description,
    normalize_invoice_number,
    normalize_name,
    normalize_reference,
    normalize_text,
    parse_amount,
)
from packages.ingestion.profiler import ColumnType, FileProfile

__all__ = [
    "CANONICAL_FIELDS",
    "ColumnMapping",
    "FieldLineage",
    "MappingError",
    "MappingResult",
    "SourceMapping",
    "apply_mapping",
    "suggest_mapping",
]


class MappingError(ValueError):
    """Raised when a mapping is structurally invalid."""


# Canonical fields a source column may be mapped onto, with the value kind the
# mapper must produce.
CANONICAL_FIELDS: dict[str, str] = {
    "transaction_date": "date",
    "posting_date": "date",
    "value_date": "date",
    "settlement_date": "date",
    "amount": "amount",
    "currency": "currency",
    "debit_credit": "text",
    "description": "text",
    "reference": "text",
    "external_transaction_id": "text",
    "bank_reference": "text",
    "counterparty_name": "text",
    "counterparty_account": "text",
    "customer_id": "text",
    "vendor_id": "text",
    "invoice_number": "text",
    "purchase_order": "text",
    "check_number": "text",
    "batch_id": "text",
    "settlement_id": "text",
    "payout_id": "text",
    "gross_amount": "amount",
    "fee_amount": "amount",
    "tax_amount": "amount",
    "net_amount": "amount",
    "original_amount": "amount",
    "original_currency": "currency",
    "fx_rate": "amount",
    "status": "text",
    "transaction_type": "text",
    "source_account_id": "text",
    "source_record_id": "text",
}

# Header keyword hints, most specific first. Matching is on a normalised header.
_HEADER_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("settlement_id", ("SETTLEMENTID", "SETTLEMENT", "BATCHREF")),
    ("payout_id", ("PAYOUTID", "PAYOUT")),
    (
        "external_transaction_id",
        ("TRANSACTIONID", "TXNID", "CHARGEID", "EXTERNALID", "PAYMENTID", "SOURCEID"),
    ),
    ("invoice_number", ("INVOICENUMBER", "INVOICENO", "INVOICE", "INVNO", "BILLNO")),
    ("purchase_order", ("PURCHASEORDER", "PONUMBER", "PONO")),
    ("check_number", ("CHECKNUMBER", "CHEQUENUMBER", "CHECKNO", "CHKNO")),
    ("bank_reference", ("BANKREFERENCE", "BANKREF", "ENDTOEND", "EREF")),
    ("reference", ("REFERENCE", "PAYMENTREFERENCE", "REF", "MEMO", "REMITTANCE")),
    (
        "counterparty_account",
        ("IBAN", "COUNTERPARTYACCOUNT", "BENEFICIARYACCOUNT", "ACCOUNTNUMBER"),
    ),
    (
        "counterparty_name",
        ("COUNTERPARTY", "PAYEE", "PAYER", "BENEFICIARY", "CUSTOMERNAME", "VENDORNAME", "NAME"),
    ),
    (
        "description",
        ("DESCRIPTION", "NARRATIVE", "DETAILS", "PARTICULARS", "TRANSACTIONDETAILS", "TEXT"),
    ),
    ("gross_amount", ("GROSSAMOUNT", "GROSS")),
    ("fee_amount", ("FEEAMOUNT", "FEE", "FEES", "CHARGE", "COMMISSION")),
    ("tax_amount", ("TAXAMOUNT", "TAX", "VAT")),
    ("net_amount", ("NETAMOUNT", "NET", "NETSETTLEMENT")),
    ("original_amount", ("ORIGINALAMOUNT", "FOREIGNAMOUNT")),
    ("original_currency", ("ORIGINALCURRENCY", "FOREIGNCURRENCY")),
    ("fx_rate", ("FXRATE", "EXCHANGERATE", "RATE")),
    ("currency", ("CURRENCY", "CCY", "CUR")),
    ("amount", ("AMOUNT", "VALUE", "SUM", "BETRAG", "MONTANT")),
    ("settlement_date", ("SETTLEMENTDATE", "SETTLEDAT")),
    ("value_date", ("VALUEDATE", "VALUTA")),
    ("posting_date", ("POSTINGDATE", "POSTEDDATE", "BOOKINGDATE", "BOOKDATE")),
    (
        "transaction_date",
        ("TRANSACTIONDATE", "DATE", "TXNDATE", "CREATEDAT", "DATUM", "OPERATIONDATE"),
    ),
    ("status", ("STATUS", "STATE")),
    ("transaction_type", ("TYPE", "TRANSACTIONTYPE", "ENTRYTYPE")),
    ("debit_credit", ("DEBITCREDIT", "DRCR", "DC", "SIGN")),
    ("customer_id", ("CUSTOMERID", "CUSTOMERNO", "CLIENTID")),
    ("vendor_id", ("VENDORID", "SUPPLIERID")),
    ("batch_id", ("BATCHID", "BATCH")),
    ("source_account_id", ("ACCOUNT", "ACCOUNTID", "GLACCOUNT", "LEDGERACCOUNT")),
    ("source_record_id", ("ID", "ROWID", "RECORDID", "LINEID", "ENTRYID")),
)

_HEADER_CLEAN_RE = re.compile(r"[^A-Z0-9]+")


def _clean_header(name: str) -> str:
    return _HEADER_CLEAN_RE.sub("", name.upper())


@dataclass(slots=True)
class ColumnMapping:
    """One source column bound to one canonical field."""

    source_column: str
    canonical_field: str
    date_format: str | None = None
    number_format: NumberFormat | None = None
    negate: bool = False
    confidence: float = 0.0
    rationale: str = ""

    def __post_init__(self) -> None:
        if self.canonical_field not in CANONICAL_FIELDS:
            raise MappingError(f"unknown canonical field: {self.canonical_field}")


@dataclass(slots=True)
class SourceMapping:
    """A complete, user-confirmed mapping for one source file."""

    source_system: str
    columns: list[ColumnMapping] = field(default_factory=list)
    static_values: dict[str, str] = field(default_factory=dict)
    # When the file expresses direction with separate debit/credit columns.
    debit_column: str | None = None
    credit_column: str | None = None
    mapping_version: str = "v1"

    def field_for(self, canonical_field: str) -> ColumnMapping | None:
        for mapping in self.columns:
            if mapping.canonical_field == canonical_field:
                return mapping
        return None

    def validate(self) -> None:
        """Reject mappings that cannot produce a valid transaction."""
        seen: set[str] = set()
        for mapping in self.columns:
            if mapping.canonical_field in seen:
                raise MappingError(f"canonical field mapped twice: {mapping.canonical_field}")
            seen.add(mapping.canonical_field)

        has_amount = "amount" in seen or (self.debit_column and self.credit_column)
        if not has_amount:
            raise MappingError("mapping must provide 'amount', or both a debit and a credit column")
        if "currency" not in seen and "currency" not in self.static_values:
            raise MappingError("mapping must provide 'currency' as a column or a static value")


@dataclass(frozen=True, slots=True)
class FieldLineage:
    """Provenance for one canonical field value (spec section 59)."""

    canonical_field: str
    value: str
    source_record_id: str
    source_field: str
    transformation: str


@dataclass(slots=True)
class MappingResult:
    transactions: list[CanonicalTransaction] = field(default_factory=list)
    lineage: list[FieldLineage] = field(default_factory=list)
    row_errors: list[tuple[int, str]] = field(default_factory=list)


def suggest_mapping(profile: FileProfile, source_system: str) -> SourceMapping:
    """Propose a mapping from a profiled file.

    Header keywords decide *what* a column is; the profile decides whether the
    column's contents are compatible with that meaning. A header called "Amount"
    holding dates is not mapped to amount.
    """
    suggestions: dict[str, ColumnMapping] = {}
    used_columns: set[str] = set()

    for canonical_field, keywords in _HEADER_HINTS:
        if canonical_field in suggestions:
            continue
        for column in profile.columns:
            if column.name in used_columns:
                continue
            cleaned = _clean_header(column.name)
            hit = next((kw for kw in keywords if kw == cleaned), None)
            confidence = 0.95
            if hit is None:
                hit = next((kw for kw in keywords if kw in cleaned), None)
                confidence = 0.7
            if hit is None:
                continue
            if not _type_compatible(CANONICAL_FIELDS[canonical_field], column.inferred_type):
                continue

            suggestions[canonical_field] = ColumnMapping(
                source_column=column.name,
                canonical_field=canonical_field,
                date_format=column.date_format,
                number_format=column.number_format,
                confidence=confidence,
                rationale=f"header '{column.name}' matches '{hit}' and column type is "
                f"{column.inferred_type}",
            )
            used_columns.add(column.name)
            break

    return SourceMapping(source_system=source_system, columns=list(suggestions.values()))


def _type_compatible(expected_kind: str, actual: ColumnType) -> bool:
    if expected_kind == "date":
        return actual is ColumnType.DATE
    if expected_kind == "amount":
        return actual in {ColumnType.AMOUNT, ColumnType.INTEGER}
    if expected_kind == "currency":
        return actual in {ColumnType.CURRENCY, ColumnType.TEXT}
    return actual in {
        ColumnType.TEXT,
        ColumnType.IDENTIFIER,
        ColumnType.INTEGER,
        ColumnType.AMOUNT,
        ColumnType.DATE,
        ColumnType.BOOLEAN,
        ColumnType.CURRENCY,
    }


def apply_mapping(
    rows: list[dict[str, str]],
    mapping: SourceMapping,
    *,
    tenant_id: UUID,
    source_connection_id: UUID,
    source_account_id: str | None = None,
    imported_at: datetime | None = None,
    collect_lineage: bool = True,
) -> MappingResult:
    """Turn source rows into canonical transactions.

    Rows that cannot be mapped are collected in ``row_errors``. They are never
    dropped silently and never "repaired".
    """
    mapping.validate()
    stamp = imported_at or utc_now()
    result = MappingResult()

    for index, row in enumerate(rows, start=1):
        try:
            transaction, lineage = _map_row(
                row,
                index,
                mapping,
                tenant_id=tenant_id,
                source_connection_id=source_connection_id,
                source_account_id=source_account_id,
                imported_at=stamp,
                collect_lineage=collect_lineage,
            )
        except (ValueError, TypeError) as exc:
            result.row_errors.append((index, str(exc)))
            continue
        result.transactions.append(transaction)
        result.lineage.extend(lineage)

    return result


def _map_row(
    row: dict[str, str],
    row_number: int,
    mapping: SourceMapping,
    *,
    tenant_id: UUID,
    source_connection_id: UUID,
    source_account_id: str | None,
    imported_at: datetime,
    collect_lineage: bool,
) -> tuple[CanonicalTransaction, list[FieldLineage]]:
    values: dict[str, Any] = {}
    lineage: list[FieldLineage] = []

    for column in mapping.columns:
        raw = (row.get(column.source_column) or "").strip()
        if not raw:
            continue
        kind = CANONICAL_FIELDS[column.canonical_field]
        if kind == "date":
            values[column.canonical_field] = parse_date(raw, column.date_format)
            transformation = f"parse_date[{column.date_format or 'iso'}]"
        elif kind == "amount":
            values[column.canonical_field] = parse_amount(
                raw,
                column.number_format or NumberFormat.DOT_DECIMAL,
                negate=column.negate,
            )
            transformation = f"parse_amount[{column.number_format or 'DOT_DECIMAL'}]"
        elif kind == "currency":
            values[column.canonical_field] = raw.strip().upper()
            transformation = "upper"
        else:
            values[column.canonical_field] = raw
            transformation = "verbatim"

        if collect_lineage and values.get(column.canonical_field) is not None:
            lineage.append(
                FieldLineage(
                    canonical_field=column.canonical_field,
                    value=str(values[column.canonical_field]),
                    source_record_id=str(row_number),
                    source_field=column.source_column,
                    transformation=transformation,
                )
            )

    for field_name, static in mapping.static_values.items():
        values.setdefault(field_name, static.upper() if field_name.endswith("currency") else static)

    # Separate debit/credit columns collapse into a signed amount.
    if mapping.debit_column and mapping.credit_column:
        number_format = NumberFormat.DOT_DECIMAL
        amount_mapping = mapping.field_for("amount")
        if amount_mapping and amount_mapping.number_format:
            number_format = amount_mapping.number_format
        debit = parse_amount(row.get(mapping.debit_column), number_format)
        credit = parse_amount(row.get(mapping.credit_column), number_format)
        if debit and debit != 0 and credit and credit != 0:
            raise ValueError("row has both a debit and a credit value")
        if debit and debit != 0:
            values["amount"] = abs(debit)
            values["debit_credit"] = "debit"
        elif credit and credit != 0:
            values["amount"] = -abs(credit)
            values["debit_credit"] = "credit"
        else:
            raise ValueError("row has neither a debit nor a credit value")

    amount = values.get("amount")
    if amount is None:
        raise ValueError("missing amount")
    if not isinstance(amount, Decimal):
        raise ValueError(f"amount did not parse to Decimal: {amount!r}")

    currency = values.get("currency")
    if not currency:
        raise ValueError("missing currency")

    raw_direction = values.get("debit_credit")
    direction = (
        cast(Literal["debit", "credit", "unknown"], raw_direction)
        if raw_direction in {"debit", "credit", "unknown"}
        else _infer_direction(raw_direction, amount)
    )

    source_record_id = str(values.get("source_record_id") or row_number)

    description = values.get("description")
    reference = values.get("reference")
    counterparty = values.get("counterparty_name")
    invoice = values.get("invoice_number")

    # Identifiers hidden inside the narrative are promoted to real fields, but
    # only where the mapped column left them empty.
    extraction = extract_references(description)
    if not reference:
        found = extraction.first("reference") or extraction.first("settlement_id")
        if found:
            reference = found.value
    if not invoice:
        found_invoice = extraction.first("invoice_number")
        if found_invoice:
            invoice = found_invoice.value
    settlement_id = values.get("settlement_id")
    if not settlement_id:
        found_settlement = extraction.first("settlement_id")
        if found_settlement:
            settlement_id = found_settlement.value
    if not counterparty and extraction.entity:
        counterparty = extraction.entity

    payload = dict(row)
    checksum = compute_checksum(payload)

    transaction = CanonicalTransaction(
        id=uuid4(),
        tenant_id=tenant_id,
        source_system=mapping.source_system,
        source_connection_id=source_connection_id,
        source_record_id=source_record_id,
        source_account_id=values.get("source_account_id") or source_account_id,
        transaction_type=values.get("transaction_type"),
        transaction_date=values.get("transaction_date"),
        posting_date=values.get("posting_date"),
        value_date=values.get("value_date"),
        settlement_date=values.get("settlement_date"),
        amount=amount,
        currency=currency,
        debit_credit=direction,
        description=description,
        normalized_description=normalize_bank_description(description),
        reference=reference,
        normalized_reference=normalize_reference(reference),
        external_transaction_id=values.get("external_transaction_id"),
        bank_reference=values.get("bank_reference"),
        counterparty_name=counterparty,
        normalized_counterparty=normalize_name(counterparty),
        counterparty_account=values.get("counterparty_account"),
        customer_id=values.get("customer_id"),
        vendor_id=values.get("vendor_id"),
        invoice_number=invoice,
        normalized_invoice_number=normalize_invoice_number(invoice),
        purchase_order=values.get("purchase_order"),
        check_number=values.get("check_number"),
        batch_id=values.get("batch_id"),
        settlement_id=settlement_id,
        payout_id=values.get("payout_id"),
        gross_amount=values.get("gross_amount"),
        fee_amount=values.get("fee_amount"),
        tax_amount=values.get("tax_amount"),
        net_amount=values.get("net_amount"),
        original_amount=values.get("original_amount"),
        original_currency=values.get("original_currency"),
        fx_rate=values.get("fx_rate"),
        status=values.get("status"),
        raw_payload=payload,
        source_checksum=checksum,
        imported_at=imported_at,
    )

    if collect_lineage:
        for canonical_field, transformation in (
            ("normalized_reference", NORMALIZER_VERSIONS["normalize_reference"]),
            ("normalized_counterparty", NORMALIZER_VERSIONS["normalize_name"]),
            ("normalized_description", NORMALIZER_VERSIONS["normalize_bank_description"]),
        ):
            value = getattr(transaction, canonical_field)
            if value:
                lineage.append(
                    FieldLineage(
                        canonical_field=canonical_field,
                        value=str(value),
                        source_record_id=source_record_id,
                        source_field=canonical_field.removeprefix("normalized_"),
                        transformation=transformation,
                    )
                )

    return transaction, lineage


def _infer_direction(raw: str | None, amount: Decimal) -> Literal["debit", "credit", "unknown"]:
    """Interpret an explicit direction column, else fall back to the sign."""
    if raw:
        token = normalize_text(raw) or ""
        if token in {"D", "DR", "DEBIT", "DB"}:
            return "debit"
        if token in {"C", "CR", "CREDIT"}:
            return "credit"
    if amount > 0:
        return "debit"
    if amount < 0:
        return "credit"
    return "unknown"
