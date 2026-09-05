#!/usr/bin/env python
"""Generate the golden reconciliation fixtures (spec sections 49 and 50).

Each scenario directory gets the five files the spec names:

    source_a.csv
    source_b.csv
    expected_matches.json
    expected_exceptions.json
    rule_config.yaml

The expectations are written by hand here, not captured from a run. A fixture
whose expectations were recorded from the engine would pass forever, including
when the engine is wrong - it would test that the code does what it does.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "reconciliation_fixtures"

BANK_GL_CONFIG = """reconciliation:
  name: "{name}"
  side_a:
    type: bank
    account: bank
  side_b:
    type: ledger
    account: ledger
  period:
    frequency: monthly
  rules:
    - bank_gl_exact_reference
    - bank_gl_exact_external_id
    - bank_gl_invoice_amount_date
    - bank_gl_amount_date_counterparty
  tolerances:
    date_days: {date_days}
    amount_absolute: "{amount_tolerance}"
  grouping:
    enabled: {grouping}
    max_group_size: {max_group_size}
  decision:
    auto_match_threshold: 0.995
    suggested_match_threshold: 0.80
    minimum_candidate_margin: 0.08
  controls:
    materiality_threshold: "{materiality}"
    manual_approval_above_materiality: true
"""

PROCESSOR_CONFIG = """reconciliation:
  name: "{name}"
  side_a:
    type: bank
    account: bank
  side_b:
    type: processor
    account: stripe
  period:
    frequency: monthly
  rules:
    - processor_payout_net_and_reference
    - processor_payout_settlement_id
  tolerances:
    date_days: {date_days}
    amount_absolute: "{amount_tolerance}"
  grouping:
    enabled: {grouping}
    max_group_size: {max_group_size}
  decision:
    auto_match_threshold: 0.995
    suggested_match_threshold: 0.80
    minimum_candidate_margin: 0.08
  controls:
    materiality_threshold: "{materiality}"
    manual_approval_above_materiality: true
"""

BANK_HEADER = "id,date,description,reference,counterparty,amount,currency"
LEDGER_HEADER = "id,date,memo,reference,invoice,counterparty,amount,currency"
PROCESSOR_HEADER = (
    "id,date,description,settlement_id,gross_amount,fee_amount,net_amount,amount,currency"
)


def rows(header: str, lines: list[str]) -> str:
    return header + "\n" + "\n".join(lines) + "\n"


# Each scenario: (side_a, side_b, expected matches, expected exceptions, config).
SCENARIOS: dict[str, dict[str, Any]] = {}


def scenario(
    name: str,
    *,
    source_a: str,
    source_b: str,
    matches: list[dict[str, Any]],
    exceptions: list[dict[str, Any]],
    config: str,
    description: str,
) -> None:
    SCENARIOS[name] = {
        "source_a": source_a,
        "source_b": source_b,
        "expected_matches": {"description": description, "matches": matches},
        "expected_exceptions": {"exceptions": exceptions},
        "rule_config": config,
    }


bank_gl = lambda **kw: BANK_GL_CONFIG.format(  # noqa: E731
    name=kw.get("name", "Bank vs GL"),
    date_days=kw.get("date_days", 3),
    amount_tolerance=kw.get("amount_tolerance", "0.01"),
    grouping=str(kw.get("grouping", False)).lower(),
    max_group_size=kw.get("max_group_size", 5),
    materiality=kw.get("materiality", "50000.00"),
)

processor = lambda **kw: PROCESSOR_CONFIG.format(  # noqa: E731
    name=kw.get("name", "Stripe payout vs Bank"),
    date_days=kw.get("date_days", 5),
    amount_tolerance=kw.get("amount_tolerance", "0.00"),
    grouping=str(kw.get("grouping", False)).lower(),
    max_group_size=kw.get("max_group_size", 6),
    materiality=kw.get("materiality", "50000.00"),
)


# --- 1. exact ---------------------------------------------------------------
scenario(
    "exact",
    description="Identical amount and reference on both sides. The strongest case.",
    source_a=rows(
        BANK_HEADER,
        [
            "BANK-1,2026-08-15,SEPA CT INV-4930,INV-4930,ACME GMBH,1250.00,EUR",
            "BANK-2,2026-08-16,SEPA CT INV-4931,INV-4931,BETA LTD,875.50,EUR",
        ],
    ),
    source_b=rows(
        LEDGER_HEADER,
        [
            "GL-1,2026-08-15,Customer payment,INV-4930,INV-4930,ACME GMBH,1250.00,EUR",
            "GL-2,2026-08-16,Customer payment,INV-4931,INV-4931,BETA LTD,875.50,EUR",
        ],
    ),
    matches=[
        {
            "side_a": ["BANK-1"],
            "side_b": ["GL-1"],
            "relation": "1:1",
            "decision": "AUTO_MATCH",
            "reasons": ["AMOUNT_EXACT", "REFERENCE_EXACT"],
        },
        {
            "side_a": ["BANK-2"],
            "side_b": ["GL-2"],
            "relation": "1:1",
            "decision": "AUTO_MATCH",
            "reasons": ["AMOUNT_EXACT", "REFERENCE_EXACT"],
        },
    ],
    exceptions=[],
    config=bank_gl(name="Exact match"),
)

# --- 2. timing --------------------------------------------------------------
scenario(
    "timing",
    description=(
        "Same amount and reference, but the ledger booked it six days later - "
        "beyond the three-day window."
    ),
    source_a=rows(
        BANK_HEADER,
        ["BANK-1,2026-08-28,SEPA CT INV-5001,INV-5001,ACME GMBH,4200.00,EUR"],
    ),
    source_b=rows(
        LEDGER_HEADER,
        ["GL-1,2026-09-03,Customer payment,INV-5001,INV-5001,ACME GMBH,4200.00,EUR"],
    ),
    matches=[],
    exceptions=[
        {
            "category": "TIMING_DIFFERENCE",
            "transactions": ["BANK-1", "GL-1"],
            "reason_codes": ["AMOUNT_EXACT", "DATE_OUTSIDE_WINDOW"],
        }
    ],
    config=bank_gl(name="Timing difference", date_days=3),
)

# --- 3. duplicates ----------------------------------------------------------
scenario(
    "duplicates",
    description=(
        "The same invoice paid twice against one ledger line. Neither leg may "
        "auto-match: doing so would hide a duplicate payment."
    ),
    source_a=rows(
        BANK_HEADER,
        [
            "BANK-1,2026-08-15,SEPA CT INV-4930,INV-4930,ACME GMBH,1250.00,EUR",
            "BANK-2,2026-08-15,SEPA CT INV-4930,INV-4930,ACME GMBH,1250.00,EUR",
        ],
    ),
    source_b=rows(
        LEDGER_HEADER,
        ["GL-1,2026-08-15,Customer payment,INV-4930,INV-4930,ACME GMBH,1250.00,EUR"],
    ),
    matches=[],
    exceptions=[
        {"category": "ANY", "transactions": ["BANK-1"], "reason_codes": []},
        {"category": "ANY", "transactions": ["BANK-2"], "reason_codes": []},
    ],
    config=bank_gl(name="Duplicate payment"),
)

# --- 4. partial payments ----------------------------------------------------
scenario(
    "partial_payments",
    description="The customer paid less than the invoice.",
    source_a=rows(
        BANK_HEADER,
        ["BANK-1,2026-08-15,SEPA CT INV-6001,INV-6001,ACME GMBH,800.00,EUR"],
    ),
    source_b=rows(
        LEDGER_HEADER,
        ["GL-1,2026-08-15,Invoice 6001,INV-6001,INV-6001,ACME GMBH,1000.00,EUR"],
    ),
    matches=[],
    exceptions=[
        {
            "category": "PARTIAL_PAYMENT",
            "transactions": ["BANK-1", "GL-1"],
            "reason_codes": ["AMOUNT_SHORT"],
        }
    ],
    config=bank_gl(name="Partial payment", amount_tolerance="250.00"),
)

# --- 5. split payments ------------------------------------------------------
scenario(
    "split_payments",
    description="One ledger invoice settled by three separate bank credits.",
    source_a=rows(
        BANK_HEADER,
        [
            "BANK-1,2026-08-10,PART PAYMENT INV-7001,INV-7001,ACME GMBH,400.00,EUR",
            "BANK-2,2026-08-11,PART PAYMENT INV-7001,INV-7001,ACME GMBH,300.00,EUR",
            "BANK-3,2026-08-12,PART PAYMENT INV-7001,INV-7001,ACME GMBH,300.00,EUR",
        ],
    ),
    source_b=rows(
        LEDGER_HEADER,
        ["GL-1,2026-08-10,Invoice 7001,INV-7001,INV-7001,ACME GMBH,1000.00,EUR"],
    ),
    matches=[
        {
            "side_a": ["BANK-1", "BANK-2", "BANK-3"],
            "side_b": ["GL-1"],
            "relation": "N:1",
            "decision": "SUGGEST",
            "reasons": ["GROUP_SUM_EXACT"],
        }
    ],
    exceptions=[],
    config=bank_gl(name="Split payment", grouping=True, max_group_size=5, date_days=5),
)

# --- 6. fees ----------------------------------------------------------------
scenario(
    "fees",
    description=(
        "The bank credited the invoice less a transfer fee. The difference is "
        "the fee, not a wrong amount."
    ),
    source_a=rows(
        BANK_HEADER,
        ["BANK-1,2026-08-15,SEPA CT INV-8001 LESS FEE,INV-8001,ACME GMBH,987.50,EUR"],
    ),
    source_b=rows(
        LEDGER_HEADER,
        ["GL-1,2026-08-15,Invoice 8001,INV-8001,INV-8001,ACME GMBH,1000.00,EUR"],
    ),
    matches=[],
    exceptions=[{"category": "ANY", "transactions": ["BANK-1", "GL-1"], "reason_codes": []}],
    config=bank_gl(name="Bank fee deduction", amount_tolerance="20.00"),
)

# --- 7. refunds -------------------------------------------------------------
scenario(
    "refunds",
    description="A refund appears on the bank with no ledger counterpart.",
    source_a=rows(
        BANK_HEADER,
        ["BANK-1,2026-08-18,REFUND TO CUSTOMER ORDER 991,,ACME GMBH,-250.00,EUR"],
    ),
    source_b=rows(LEDGER_HEADER, ["GL-1,2026-08-18,Sales,,,ACME GMBH,250.00,EUR"]),
    matches=[],
    exceptions=[
        {
            "category": "REFUND",
            "transactions": ["BANK-1"],
            "reason_codes": ["DESCRIPTION_INDICATES_REFUND"],
        }
    ],
    config=bank_gl(name="Refund"),
)

# --- 8. chargebacks ---------------------------------------------------------
scenario(
    "chargebacks",
    description="A chargeback debited by the processor with nothing booked yet.",
    source_a=rows(
        BANK_HEADER,
        ["BANK-1,2026-08-22,CHARGEBACK CASE 4471,,STRIPE,-120.00,EUR"],
    ),
    source_b=rows(LEDGER_HEADER, ["GL-1,2026-08-01,Opening,,,ACME GMBH,5000.00,EUR"]),
    matches=[],
    exceptions=[
        {
            "category": "CHARGEBACK",
            "transactions": ["BANK-1"],
            "reason_codes": ["DESCRIPTION_INDICATES_CHARGEBACK"],
        }
    ],
    config=bank_gl(name="Chargeback"),
)

# --- 9. reversals -----------------------------------------------------------
scenario(
    "reversals",
    description="A payment and its reversal, both on the bank side.",
    source_a=rows(
        BANK_HEADER,
        [
            "BANK-1,2026-08-14,SEPA CT INV-9001,INV-9001,ACME GMBH,500.00,EUR",
            "BANK-2,2026-08-15,REVERSAL OF INV-9001,INV-9001,ACME GMBH,-500.00,EUR",
        ],
    ),
    source_b=rows(
        LEDGER_HEADER, ["GL-1,2026-08-14,Invoice 9001,INV-9001,INV-9001,ACME GMBH,500.00,EUR"]
    ),
    matches=[
        {
            "side_a": ["BANK-1"],
            "side_b": ["GL-1"],
            "relation": "1:1",
            "decision": "AUTO_MATCH",
            "reasons": ["AMOUNT_EXACT", "REFERENCE_EXACT"],
        }
    ],
    exceptions=[{"category": "ANY", "transactions": ["BANK-2"], "reason_codes": []}],
    config=bank_gl(name="Reversal"),
)

# --- 10. fx -----------------------------------------------------------------
scenario(
    "fx",
    description=(
        "The bank received USD, the ledger booked EUR. FX is not a wider amount "
        "tolerance: with FX disabled these are not even eligible."
    ),
    source_a=rows(
        BANK_HEADER,
        ["BANK-1,2026-08-15,USD RECEIPT INV-1101,INV-1101,ACME INC,1100.00,USD"],
    ),
    source_b=rows(
        LEDGER_HEADER,
        ["GL-1,2026-08-15,Invoice 1101,INV-1101,INV-1101,ACME INC,1000.00,EUR"],
    ),
    matches=[],
    exceptions=[
        {"category": "ANY", "transactions": ["BANK-1"], "reason_codes": []},
        {"category": "ANY", "transactions": ["GL-1"], "reason_codes": []},
    ],
    config=bank_gl(name="FX difference"),
)

# --- 11. processor settlements ---------------------------------------------
scenario(
    "processor_settlements",
    description=(
        "Spec section 100: the bank deposit equals the net payout and the payout "
        "ID appears in the bank narrative."
    ),
    source_a=rows(
        BANK_HEADER,
        ["BANK-1,2026-08-31,STRIPE PAYOUT 8F42,,STRIPE,982.45,EUR"],
    ),
    source_b=rows(
        PROCESSOR_HEADER,
        ["PAYOUT-8F42,2026-08-31,Stripe payout,8F42,1050.00,17.55,982.45,982.45,EUR"],
    ),
    matches=[
        {
            "side_a": ["BANK-1"],
            "side_b": ["PAYOUT-8F42"],
            "relation": "1:1",
            "decision": "AUTO_MATCH",
            "reasons": ["NET_AMOUNT_EXACT", "SETTLEMENT_REFERENCE_EXACT"],
        }
    ],
    exceptions=[],
    config=processor(name="Stripe net payout"),
)

# --- 12. rounding -----------------------------------------------------------
scenario(
    "rounding",
    description=(
        "Spec section 100 step 7: the ledger booked the processor fee as 17.50 "
        "rather than 17.55, leaving five cents."
    ),
    source_a=rows(
        BANK_HEADER,
        ["BANK-1,2026-08-31,STRIPE PAYOUT 9C11,,STRIPE,982.45,EUR"],
    ),
    source_b=rows(
        PROCESSOR_HEADER,
        ["PAYOUT-9C11,2026-08-31,Stripe payout,9C11,1050.00,17.50,982.50,982.50,EUR"],
    ),
    matches=[],
    exceptions=[
        {
            "category": "ROUNDING_DIFFERENCE",
            "transactions": ["BANK-1", "PAYOUT-9C11"],
            "reason_codes": ["SMALL_DIFFERENCE"],
        }
    ],
    config=processor(name="Processor fee difference", amount_tolerance="0.10"),
)

# --- 13. one to many --------------------------------------------------------
scenario(
    "one_to_many",
    description="Spec section 20: bank 10,000 = ledger 4,000 + 3,000 + 3,000.",
    source_a=rows(
        BANK_HEADER,
        ["BANK-1,2026-08-31,BATCH DEPOSIT B77,,ACME GMBH,10000.00,EUR"],
    ),
    source_b=rows(
        LEDGER_HEADER,
        [
            "GL-1,2026-08-31,Batch item 1,,,ACME GMBH,4000.00,EUR",
            "GL-2,2026-08-31,Batch item 2,,,ACME GMBH,3000.00,EUR",
            "GL-3,2026-08-31,Batch item 3,,,ACME GMBH,3000.00,EUR",
        ],
    ),
    matches=[
        {
            "side_a": ["BANK-1"],
            "side_b": ["GL-1", "GL-2", "GL-3"],
            "relation": "1:N",
            "decision": "SUGGEST",
            "reasons": ["GROUP_SUM_EXACT"],
        }
    ],
    exceptions=[],
    config=bank_gl(name="One to many", grouping=True, max_group_size=5),
)

# --- 14. many to one --------------------------------------------------------
scenario(
    "many_to_one",
    description="Three bank credits settling one ledger receivable.",
    source_a=rows(
        BANK_HEADER,
        [
            "BANK-1,2026-08-05,DEPOSIT 1,,ACME GMBH,1500.00,EUR",
            "BANK-2,2026-08-06,DEPOSIT 2,,ACME GMBH,2500.00,EUR",
            "BANK-3,2026-08-07,DEPOSIT 3,,ACME GMBH,1000.00,EUR",
        ],
    ),
    source_b=rows(
        LEDGER_HEADER,
        ["GL-1,2026-08-05,Consolidated receivable,,,ACME GMBH,5000.00,EUR"],
    ),
    matches=[
        {
            "side_a": ["BANK-1", "BANK-2", "BANK-3"],
            "side_b": ["GL-1"],
            "relation": "N:1",
            "decision": "SUGGEST",
            "reasons": ["GROUP_SUM_EXACT"],
        }
    ],
    exceptions=[],
    config=bank_gl(name="Many to one", grouping=True, max_group_size=5, date_days=5),
)

# --- 15. many to many -------------------------------------------------------
scenario(
    "many_to_many",
    description=(
        "Two deposits against two ledger lines with no shared identifier. "
        "Unconstrained N:N is not attempted: everything goes to a human."
    ),
    source_a=rows(
        BANK_HEADER,
        [
            "BANK-1,2026-08-10,DEPOSIT A,,ACME GMBH,600.00,EUR",
            "BANK-2,2026-08-10,DEPOSIT B,,ACME GMBH,400.00,EUR",
        ],
    ),
    source_b=rows(
        LEDGER_HEADER,
        [
            "GL-1,2026-08-10,Receivable A,,,ACME GMBH,700.00,EUR",
            "GL-2,2026-08-10,Receivable B,,,ACME GMBH,300.00,EUR",
        ],
    ),
    matches=[],
    exceptions=[
        {"category": "ANY", "transactions": ["BANK-1"], "reason_codes": []},
        {"category": "ANY", "transactions": ["BANK-2"], "reason_codes": []},
    ],
    config=bank_gl(name="Many to many", grouping=True, max_group_size=4),
)


def write_all(target: Path = FIXTURES) -> int:
    written = 0
    for name, payload in SCENARIOS.items():
        directory = target / name
        directory.mkdir(parents=True, exist_ok=True)

        (directory / "source_a.csv").write_text(payload["source_a"], encoding="utf-8")
        (directory / "source_b.csv").write_text(payload["source_b"], encoding="utf-8")
        (directory / "rule_config.yaml").write_text(payload["rule_config"], encoding="utf-8")
        (directory / "expected_matches.json").write_text(
            json.dumps(payload["expected_matches"], indent=2) + "\n", encoding="utf-8"
        )
        (directory / "expected_exceptions.json").write_text(
            json.dumps(payload["expected_exceptions"], indent=2) + "\n", encoding="utf-8"
        )
        written += 1
    return written


if __name__ == "__main__":
    count = write_all()
    print(f"wrote {count} fixture scenarios to {FIXTURES}")
    sys.exit(0)
