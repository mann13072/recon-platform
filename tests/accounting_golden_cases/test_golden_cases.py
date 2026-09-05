"""The golden accounting cases (spec sections 49, 50 and 103).

Every directory under ``tests/reconciliation_fixtures`` is a scenario: two CSV
sources, a rule configuration, and the matches and exceptions a competent
accountant would expect. The runner drives the real engine over them.

The expectations are written by hand in ``scripts/generate_fixture.py``, not
captured from a run, so a change in engine behaviour shows up as a failure
rather than as a silently updated snapshot.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

import pytest
import yaml

from packages.domain.dates import parse_date, utc_now
from packages.domain.models.reconciliation import ReconciliationConfig
from packages.domain.models.transaction import CanonicalTransaction, compute_checksum
from packages.ingestion.normalization import (
    normalize_bank_description,
    normalize_invoice_number,
    normalize_name,
    normalize_reference,
)
from packages.ingestion.parsers import parse_csv
from packages.matching.engine import MatchingContext, MatchingEngine
from packages.matching.templates import bank_gl_template, stripe_payout_template

FIXTURES = Path(__file__).resolve().parents[1] / "reconciliation_fixtures"
NAMESPACE = UUID("6f2c1a90-84d1-4d2a-9a3d-0c1b7f5e2d44")

TENANT = uuid5(NAMESPACE, "tenant")
RECONCILIATION = uuid5(NAMESPACE, "reconciliation")
RUN = uuid5(NAMESPACE, "run")


def scenario_names() -> list[str]:
    return sorted(
        directory.name
        for directory in FIXTURES.iterdir()
        if directory.is_dir() and (directory / "source_a.csv").exists()
    )


def _decimal(value: str | None) -> Decimal | None:
    text = (value or "").strip()
    return Decimal(text) if text else None


def load_transactions(path: Path, source_system: str) -> list[CanonicalTransaction]:
    """Build canonical transactions from a fixture CSV.

    The fixture columns are already canonical, so this bypasses the mapping
    suggester on purpose: a golden case tests the *engine*, and a mapping bug
    would otherwise show up here as a matching failure.
    """
    table = parse_csv(path.read_bytes())
    connection = uuid5(NAMESPACE, source_system)
    transactions: list[CanonicalTransaction] = []

    for row in table.rows:
        record_id = row["id"]
        description = row.get("description") or row.get("memo") or None
        reference = row.get("reference") or None
        invoice = row.get("invoice") or None
        counterparty = row.get("counterparty") or None
        settlement = row.get("settlement_id") or None

        # A fixture's "reference" column may itself be an invoice number.
        if invoice is None and reference and reference.upper().startswith("INV"):
            invoice = reference

        transactions.append(
            CanonicalTransaction(
                id=uuid5(NAMESPACE, f"{source_system}:{record_id}"),
                tenant_id=TENANT,
                source_system=source_system,
                source_connection_id=connection,
                source_record_id=record_id,
                transaction_date=parse_date(row.get("date")),
                amount=Decimal(row["amount"]),
                currency=row["currency"],
                description=description,
                normalized_description=normalize_bank_description(description),
                reference=reference,
                normalized_reference=normalize_reference(reference),
                invoice_number=invoice,
                normalized_invoice_number=normalize_invoice_number(invoice),
                counterparty_name=counterparty,
                normalized_counterparty=normalize_name(counterparty),
                settlement_id=settlement,
                payout_id=settlement,
                gross_amount=_decimal(row.get("gross_amount")),
                fee_amount=_decimal(row.get("fee_amount")),
                net_amount=_decimal(row.get("net_amount")),
                raw_payload=dict(row),
                source_checksum=compute_checksum(dict(row)),
                imported_at=utc_now(),
            )
        )
    return transactions


def load_scenario(name: str) -> dict[str, Any]:
    directory = FIXTURES / name
    config_document = yaml.safe_load((directory / "rule_config.yaml").read_text("utf-8"))
    config = ReconciliationConfig.from_yaml_dict(config_document)

    side_b_type = config.side_b.type
    rule_set = stripe_payout_template()[1] if side_b_type == "processor" else bank_gl_template()[1]

    return {
        "name": name,
        "config": config,
        "rule_set": rule_set,
        "side_a": load_transactions(directory / "source_a.csv", config.side_a.type),
        "side_b": load_transactions(directory / "source_b.csv", side_b_type),
        "expected_matches": json.loads((directory / "expected_matches.json").read_text("utf-8")),
        "expected_exceptions": json.loads(
            (directory / "expected_exceptions.json").read_text("utf-8")
        ),
    }


def run_scenario(scenario: dict[str, Any]) -> Any:
    engine = MatchingEngine(scenario["config"], scenario["rule_set"])
    return engine.run(
        scenario["side_a"],
        scenario["side_b"],
        MatchingContext(TENANT, RECONCILIATION, RUN, scenario["rule_set"].version),
    )


def record_ids(result: Any, scenario: dict[str, Any]) -> dict[UUID, str]:
    del result
    return {t.id: t.source_record_id for t in (*scenario["side_a"], *scenario["side_b"])}


@pytest.mark.parametrize("name", scenario_names())
class TestGoldenCases:
    def test_fixture_is_complete(self, name: str) -> None:
        """Spec section 49: every fixture ships all five files."""
        directory = FIXTURES / name
        for required in (
            "source_a.csv",
            "source_b.csv",
            "expected_matches.json",
            "expected_exceptions.json",
            "rule_config.yaml",
        ):
            assert (directory / required).exists(), f"{name} is missing {required}"

    def test_expected_matches_are_produced(self, name: str) -> None:
        scenario = load_scenario(name)
        result = run_scenario(scenario)
        names = record_ids(result, scenario)

        produced = {
            (
                tuple(sorted(names[i] for i in group.side_a_ids)),
                tuple(sorted(names[i] for i in group.side_b_ids)),
            ): group
            for group in result.matches
        }

        for expected in scenario["expected_matches"]["matches"]:
            key = (tuple(sorted(expected["side_a"])), tuple(sorted(expected["side_b"])))
            assert key in produced, (
                f"{name}: expected a match between {expected['side_a']} and "
                f"{expected['side_b']}, but the engine produced "
                f"{sorted(produced)}"
            )
            group = produced[key]
            assert group.cardinality.value == expected["relation"], (
                f"{name}: expected relation {expected['relation']}, got {group.cardinality.value}"
            )
            assert group.decision.value == expected["decision"], (
                f"{name}: expected decision {expected['decision']}, got "
                f"{group.decision.value}. Reasons: "
                f"{[r.code for r in group.reasons]}"
            )
            codes = {r.code for r in group.reasons}
            for reason in expected["reasons"]:
                assert reason in codes, (
                    f"{name}: expected reason {reason} on the match, got {sorted(codes)}"
                )

    def test_no_unexpected_automatic_matches(self, name: str) -> None:
        """False matches are more dangerous than unmatched records (spec 1.1)."""
        scenario = load_scenario(name)
        result = run_scenario(scenario)
        names = record_ids(result, scenario)

        expected_auto = {
            (tuple(sorted(m["side_a"])), tuple(sorted(m["side_b"])))
            for m in scenario["expected_matches"]["matches"]
            if m["decision"] == "AUTO_MATCH"
        }
        produced_auto = {
            (
                tuple(sorted(names[i] for i in group.side_a_ids)),
                tuple(sorted(names[i] for i in group.side_b_ids)),
            )
            for group in result.matches
            if group.decision.value == "AUTO_MATCH"
        }
        assert produced_auto == expected_auto, (
            f"{name}: the engine automatically matched {produced_auto - expected_auto} "
            f"which the fixture does not sanction, and missed "
            f"{expected_auto - produced_auto}"
        )

    def test_every_automatic_match_is_explainable(self, name: str) -> None:
        """Spec section 99 criterion 7: explain every automatic match."""
        scenario = load_scenario(name)
        result = run_scenario(scenario)
        for group in result.matches:
            if group.decision.value != "AUTO_MATCH":
                continue
            assert group.rule_id, f"{name}: an automatic match with no rule"
            assert group.rule_version, f"{name}: an automatic match with no rule version"
            assert group.reasons, f"{name}: an automatic match with no evidence codes"
            for reason in group.reasons:
                assert reason.description.strip(), (
                    f"{name}: evidence code {reason.code} has no description"
                )

    def test_expected_exception_transactions_reach_a_human(self, name: str) -> None:
        """Anything the fixture calls an exception must not be settled automatically.

        Reaching a human can mean two things, and both are correct: the record is
        left unmatched and becomes an exception, or it is offered as a *suggestion*
        for someone to accept or reject. What must never happen is an automatic
        match - that is the silent decision the spec forbids (section 1.1).
        """
        scenario = load_scenario(name)
        result = run_scenario(scenario)
        names = record_ids(result, scenario)

        auto_matched: set[str] = {
            names[member.transaction_id]
            for group in result.matches
            if group.decision.value == "AUTO_MATCH"
            for member in group.members
        }

        for expected in scenario["expected_exceptions"]["exceptions"]:
            for record_id in expected["transactions"]:
                assert record_id not in auto_matched, (
                    f"{name}: {record_id} was matched automatically, but this "
                    "fixture requires a human to decide it"
                )

    def test_result_is_reproducible(self, name: str) -> None:
        scenario = load_scenario(name)
        first = run_scenario(scenario)
        second = run_scenario(load_scenario(name))
        assert first.result_hash == second.result_hash, (
            f"{name}: two runs of the same fixture produced different results"
        )

    def test_invariants_hold(self, name: str) -> None:
        from packages.matching.engine import assert_invariants

        scenario = load_scenario(name)
        result = run_scenario(scenario)
        by_id = {t.id: t for t in (*scenario["side_a"], *scenario["side_b"])}
        assert_invariants(result.matches, by_id)


class TestFixtureCoverage:
    def test_every_scenario_directory_from_the_spec_exists(self) -> None:
        """Spec section 49 names the directories; all of them must be populated."""
        required = {
            "exact",
            "timing",
            "duplicates",
            "partial_payments",
            "split_payments",
            "fees",
            "refunds",
            "chargebacks",
            "reversals",
            "fx",
            "processor_settlements",
            "rounding",
            "one_to_many",
            "many_to_one",
            "many_to_many",
        }
        assert required.issubset(set(scenario_names())), (
            f"missing fixture scenarios: {sorted(required - set(scenario_names()))}"
        )

    def test_the_stripe_net_payout_case_from_the_spec(self) -> None:
        """Spec section 50's ``stripe_net_payout`` golden case, exactly."""
        scenario = load_scenario("processor_settlements")
        result = run_scenario(scenario)

        assert len(result.matches) == 1
        group = result.matches[0]
        assert group.cardinality.value == "1:1"
        assert group.decision.value == "AUTO_MATCH"
        codes = {r.code for r in group.reasons}
        assert "NET_AMOUNT_EXACT" in codes
        assert "SETTLEMENT_REFERENCE_EXACT" in codes

    def test_a_duplicate_payment_is_never_settled_automatically(self) -> None:
        """The most dangerous false match the spec names (section 1.1).

        Two identical payments against one ledger line: automatically matching
        either leg would leave the other as a lone unmatched item and hide the
        fact that the invoice was paid twice.
        """
        scenario = load_scenario("duplicates")
        result = run_scenario(scenario)
        assert not result.auto_matched, (
            "a duplicate payment was matched automatically, hiding the duplicate"
        )
        # Both legs must still be visible to a reviewer, one way or another.
        names = record_ids(result, scenario)
        surfaced = {
            names[member.transaction_id] for group in result.matches for member in group.members
        } | {t.source_record_id for t in result.unmatched_a}
        assert {"BANK-1", "BANK-2"}.issubset(surfaced)

    def test_the_five_cent_fee_difference_from_the_spec(self) -> None:
        """Spec section 100 step 7: 17.50 vs 17.55 leaves an exception, not a match."""
        scenario = load_scenario("rounding")
        result = run_scenario(scenario)
        assert not result.auto_matched, "a five-cent fee difference was automatically matched"
        assert result.unmatched_a, "the bank deposit should be left for a human"
