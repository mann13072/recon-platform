"""Data-quality gate (spec sections 10 and 85).

The gate reports PASS / WARN / FAIL. It never silently repairs accounting data;
every problem becomes a visible finding with the affected rows attached.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from packages.domain.enums import QualityLevel
from packages.domain.models.transaction import CanonicalTransaction
from packages.ingestion.profiler import ColumnType, FileProfile

__all__ = ["QualityFinding", "QualityReport", "run_quality_checks", "check_profile"]


@dataclass(frozen=True, slots=True)
class QualityFinding:
    code: str
    level: QualityLevel
    message: str
    affected_count: int = 0
    sample_rows: tuple[str, ...] = ()


@dataclass(slots=True)
class QualityReport:
    findings: list[QualityFinding] = field(default_factory=list)
    row_count: int = 0
    total_by_currency: dict[str, Decimal] = field(default_factory=dict)

    @property
    def level(self) -> QualityLevel:
        if any(f.level is QualityLevel.FAIL for f in self.findings):
            return QualityLevel.FAIL
        if any(f.level is QualityLevel.WARN for f in self.findings):
            return QualityLevel.WARN
        return QualityLevel.PASS

    @property
    def blocking(self) -> bool:
        """A FAIL blocks the reconciliation run (spec section 58)."""
        return self.level is QualityLevel.FAIL

    def add(self, finding: QualityFinding) -> None:
        self.findings.append(finding)

    def codes(self) -> set[str]:
        return {f.code for f in self.findings}


def check_profile(profile: FileProfile) -> list[QualityFinding]:
    """Findings that are visible from the raw file, before mapping."""
    findings: list[QualityFinding] = []

    if profile.row_count == 0:
        findings.append(
            QualityFinding("FILE_EMPTY", QualityLevel.FAIL, "The file contains no data rows.")
        )

    if profile.skipped_rows:
        findings.append(
            QualityFinding(
                "MALFORMED_ROWS",
                QualityLevel.FAIL,
                "Some rows do not match the header's column count and were not imported.",
                affected_count=len(profile.skipped_rows),
                sample_rows=tuple(f"line {n}: {why}" for n, why in profile.skipped_rows[:5]),
            )
        )

    for column in profile.columns:
        for warning in column.warnings:
            level = (
                QualityLevel.FAIL
                if "ambiguous" in warning and column.inferred_type is ColumnType.DATE
                else QualityLevel.WARN
            )
            findings.append(
                QualityFinding(
                    "AMBIGUOUS_DATE_FORMAT" if "DD/MM" in warning else "COLUMN_WARNING",
                    level,
                    f"Column '{column.name}': {warning}",
                )
            )

    return findings


def run_quality_checks(
    transactions: list[CanonicalTransaction],
    *,
    row_errors: list[tuple[int, str]] | None = None,
    profile: FileProfile | None = None,
    expected_total: Decimal | None = None,
    previous_row_count: int | None = None,
) -> QualityReport:
    """Run the full check list from spec section 10 over mapped transactions."""
    report = QualityReport(row_count=len(transactions))

    if profile is not None:
        for finding in check_profile(profile):
            report.add(finding)

    if row_errors:
        report.add(
            QualityFinding(
                "ROW_MAPPING_FAILED",
                QualityLevel.FAIL,
                "Some rows could not be mapped to the canonical model.",
                affected_count=len(row_errors),
                sample_rows=tuple(f"row {n}: {why}" for n, why in row_errors[:5]),
            )
        )

    if not transactions:
        if report.row_count == 0 and "FILE_EMPTY" not in report.codes():
            report.add(
                QualityFinding(
                    "NO_TRANSACTIONS",
                    QualityLevel.FAIL,
                    "Mapping produced no transactions.",
                )
            )
        return report

    # -- per-transaction checks -------------------------------------------
    missing_currency: list[str] = []
    impossible_dates: list[str] = []
    blank_reference = 0
    sign_by_type: dict[str, set[str]] = defaultdict(set)
    totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))

    checksum_counts: Counter[str] = Counter()
    source_id_counts: Counter[str] = Counter()
    external_id_counts: Counter[str] = Counter()

    for tx in transactions:
        if not tx.currency or len(tx.currency) != 3:
            missing_currency.append(tx.source_record_id)
        if tx.best_date is None:
            impossible_dates.append(tx.source_record_id)
        if not (tx.reference or tx.invoice_number or tx.external_transaction_id):
            blank_reference += 1
        if tx.transaction_type:
            sign_by_type[tx.transaction_type].add("+" if tx.amount >= 0 else "-")
        totals[tx.currency] += tx.amount

        checksum_counts[tx.source_checksum] += 1
        source_id_counts[tx.source_record_id] += 1
        if tx.external_transaction_id:
            external_id_counts[tx.external_transaction_id] += 1

    report.total_by_currency = dict(totals)

    if missing_currency:
        report.add(
            QualityFinding(
                "INVALID_CURRENCY",
                QualityLevel.FAIL,
                "Transactions have a missing or invalid currency code.",
                affected_count=len(missing_currency),
                sample_rows=tuple(missing_currency[:5]),
            )
        )

    if impossible_dates:
        report.add(
            QualityFinding(
                "MISSING_DATE",
                QualityLevel.WARN,
                "Transactions have no usable date; date tolerances cannot apply to them.",
                affected_count=len(impossible_dates),
                sample_rows=tuple(impossible_dates[:5]),
            )
        )

    duplicate_rows = [c for c, n in checksum_counts.items() if n > 1]
    if duplicate_rows:
        report.add(
            QualityFinding(
                "DUPLICATE_SOURCE_ROW",
                QualityLevel.WARN,
                "Identical source rows appear more than once in this file.",
                affected_count=sum(checksum_counts[c] - 1 for c in duplicate_rows),
                sample_rows=tuple(c[:12] for c in duplicate_rows[:5]),
            )
        )

    duplicate_ids = [i for i, n in source_id_counts.items() if n > 1]
    if duplicate_ids:
        report.add(
            QualityFinding(
                "DUPLICATE_SOURCE_RECORD_ID",
                QualityLevel.FAIL,
                "The same source record ID appears on more than one row, so "
                "records cannot be identified unambiguously.",
                affected_count=len(duplicate_ids),
                sample_rows=tuple(duplicate_ids[:5]),
            )
        )

    duplicate_external = [i for i, n in external_id_counts.items() if n > 1]
    if duplicate_external:
        report.add(
            QualityFinding(
                "DUPLICATE_TRANSACTION_ID",
                QualityLevel.FAIL,
                "The same external transaction ID appears on more than one row.",
                affected_count=len(duplicate_external),
                sample_rows=tuple(duplicate_external[:5]),
            )
        )

    blank_ratio = blank_reference / len(transactions)
    if blank_ratio >= 0.20:
        report.add(
            QualityFinding(
                "HIGH_BLANK_REFERENCE_RATE",
                QualityLevel.WARN,
                f"{blank_ratio:.0%} of transactions have no reference, invoice number "
                "or external ID; reference-based rules will not apply to them.",
                affected_count=blank_reference,
            )
        )

    inconsistent = [t for t, signs in sign_by_type.items() if len(signs) > 1]
    if inconsistent:
        report.add(
            QualityFinding(
                "INCONSISTENT_SIGNS",
                QualityLevel.WARN,
                "Transaction types contain both positive and negative amounts; "
                "confirm the debit/credit convention of this source.",
                affected_count=len(inconsistent),
                sample_rows=tuple(sorted(inconsistent)[:5]),
            )
        )

    if len(totals) > 1:
        report.add(
            QualityFinding(
                "MIXED_CURRENCIES",
                QualityLevel.WARN,
                "The file contains more than one currency: " + ", ".join(sorted(totals)),
                affected_count=len(totals),
            )
        )

    if expected_total is not None:
        actual = sum(totals.values(), Decimal("0"))
        if actual != expected_total:
            report.add(
                QualityFinding(
                    "TOTAL_IMBALANCE",
                    QualityLevel.FAIL,
                    f"Sum of imported amounts ({actual}) does not equal the declared "
                    f"control total ({expected_total}).",
                )
            )

    if previous_row_count is not None and previous_row_count > 0:
        change = abs(len(transactions) - previous_row_count) / previous_row_count
        if change >= 0.5:
            report.add(
                QualityFinding(
                    "ROW_COUNT_CHANGE",
                    QualityLevel.WARN,
                    f"Row count changed by {change:.0%} versus the previous import "
                    f"({previous_row_count} -> {len(transactions)}).",
                )
            )

    return report
