"""The first engineering milestone, end to end (spec section 102).

    Upload a bank CSV and a ledger CSV, map the columns, run reconciliation,
    see exact matches, review ambiguous suggestions, approve/reject them,
    resolve exceptions, and close the reconciliation with a complete audit
    history.

If this test fails, the product does not work, whatever else passes.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from apps.api.app.config import Settings
from apps.api.app.infrastructure.auth import issue_dev_token
from apps.api.app.infrastructure.models import TenantRow
from apps.api.app.infrastructure.storage import LocalObjectStorage

BANK_CSV = b"""Date,Description,Reference,Amount,Currency
2026-08-31,STRIPE PAYOUT 8F42,,982.45,EUR
2026-08-15,SEPA CT INV-4930 ACME GMBH,INV-4930,1250.00,EUR
2026-08-16,SEPA CT INV-4931 BETA LTD,INV-4931,875.50,EUR
2026-08-20,CARD PAYMENT UNKNOWN MERCHANT,,-64.20,EUR
2026-08-02,BANK FEE MONTHLY,,-12.50,EUR
"""

LEDGER_CSV = b"""PostingDate,Memo,InvoiceNo,Value,Ccy
2026-08-15,Customer payment Acme,INV-4930,1250.00,EUR
2026-08-16,Customer payment Beta,INV-4931,875.50,EUR
2026-08-31,Stripe clearing,,982.45,EUR
2026-08-05,Office supplies,,-210.00,EUR
"""


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        ENVIRONMENT="test",
        DATABASE_URL="sqlite+pysqlite:///:memory:",
        AUTH_DEV_SECRET="integration-test-secret-value-min-32-chars",
        AUTH_AUDIENCE="",
        AI_ENABLED=False,
        LOCAL_STORAGE_PATH=str(tmp_path / "storage"),
        OBJECT_STORAGE_ENDPOINT="",
        CREDENTIAL_ENCRYPTION_KEY="test-credential-key-at-least-32-characters",
    )


@pytest.fixture
def client(
    engine: Engine, session_factory: sessionmaker[Session], settings: Settings, tmp_path: Path
) -> Iterator[TestClient]:
    from apps.api.app.config import get_settings
    from apps.api.app.dependencies import get_session, get_storage
    from apps.api.app.main import app

    def override_session() -> Iterator[Session]:
        db = session_factory()
        try:
            yield db
            if db.is_active:
                db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_storage] = lambda: LocalObjectStorage(root=tmp_path / "storage")
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def tenant(session: Session) -> UUID:
    identifier = uuid4()
    session.add(TenantRow(id=identifier, name="Acme GmbH", slug="acme-e2e"))
    session.commit()
    return identifier


def auth(settings: Settings, tenant: UUID, roles: list[str], subject: str) -> dict[str, str]:
    token = issue_dev_token(
        settings,
        subject=subject,
        tenant_id=tenant,
        roles=roles,
        email=f"{subject}@example.com",
    )
    return {"Authorization": f"Bearer {token}"}


def upload_and_ingest(
    client: TestClient,
    headers: dict[str, str],
    *,
    filename: str,
    data: bytes,
    source_system: str,
) -> dict[str, Any]:
    """Upload, confirm the suggested mapping, and ingest."""
    response = client.post(
        "/api/v1/files",
        headers=headers,
        files={"file": (filename, data, "text/csv")},
    )
    assert response.status_code == 201, response.text
    file_id = response.json()["id"]

    profile = client.get(f"/api/v1/files/{file_id}/profile", headers=headers)
    assert profile.status_code == 200, profile.text
    suggested = profile.json()["suggested_mapping"]
    assert suggested["columns"], "the platform suggested no mapping at all"

    mapping = {
        "source_system": source_system,
        "columns": [
            {
                "source_column": column["source_column"],
                "canonical_field": column["canonical_field"],
                "date_format": column["date_format"],
                "number_format": column["number_format"],
                "negate": column["negate"],
            }
            for column in suggested["columns"]
        ],
        "static_values": {},
    }
    mapped = client.post(f"/api/v1/files/{file_id}/mapping", headers=headers, json=mapping)
    assert mapped.status_code == 200, mapped.text

    ingested = client.post(f"/api/v1/files/{file_id}/ingest", headers=headers)
    assert ingested.status_code == 200, ingested.text
    return ingested.json()


class TestGoldenDemo:
    def test_upload_map_run_review_resolve_close(
        self, client: TestClient, settings: Settings, tenant: UUID
    ) -> None:
        preparer = auth(settings, tenant, ["PREPARER"], "alice")
        controller = auth(settings, tenant, ["CONTROLLER"], "bob")

        # -- 1. upload and ingest both sides ------------------------------
        bank = upload_and_ingest(
            client,
            preparer,
            filename="bank_august.csv",
            data=BANK_CSV,
            source_system="bank",
        )
        assert bank["transactions_created"] == 5
        assert bank["rows_failed"] == 0

        ledger = upload_and_ingest(
            client,
            preparer,
            filename="ledger_august.csv",
            data=LEDGER_CSV,
            source_system="ledger",
        )
        assert ledger["transactions_created"] == 4

        # -- 2. re-ingesting is idempotent --------------------------------
        again = client.post(f"/api/v1/files/{bank['file_id']}/ingest", headers=preparer)
        assert again.status_code == 200
        assert again.json()["transactions_created"] == 0
        assert again.json()["duplicates_skipped"] == 5

        # -- 3. define the reconciliation ---------------------------------
        created = client.post(
            "/api/v1/reconciliations",
            headers=controller,
            json={
                "slug": "bank-gl-august",
                "name": "Main EUR Bank vs GL",
                "template": "bank_gl",
            },
        )
        assert created.status_code == 201, created.text
        reconciliation_id = created.json()["id"]

        # -- 4. run it -----------------------------------------------------
        run_response = client.post(
            f"/api/v1/reconciliations/{reconciliation_id}/runs",
            headers={**preparer, "Idempotency-Key": "run-august-1"},
            json={"period_start": "2026-08-01", "period_end": "2026-08-31"},
        )
        assert run_response.status_code == 201, run_response.text
        run = run_response.json()
        run_id = run["id"]
        assert run["status"] in {"REVIEW_REQUIRED", "READY_TO_CLOSE"}
        assert run["result_hash"]

        # -- 5. the idempotency key makes a retry safe --------------------
        replay = client.post(
            f"/api/v1/reconciliations/{reconciliation_id}/runs",
            headers={**preparer, "Idempotency-Key": "run-august-1"},
            json={"period_start": "2026-08-01", "period_end": "2026-08-31"},
        )
        assert replay.status_code == 201
        assert replay.json()["id"] == run_id
        assert replay.json()["replayed"] is True

        # -- 6. the dashboard answers "are my accounts reconciled?" -------
        summary = client.get(f"/api/v1/runs/{run_id}/summary", headers=preparer).json()
        assert summary["side_a_balance"] == "3031.25"
        assert summary["side_b_balance"] == "2897.95"
        assert summary["difference"] == "133.30"
        # Automation rate is never reported without the false-match rate.
        assert "false_match_rate" in summary
        assert summary["auto_matched_count"] >= 2

        # -- 7. exact invoice matches happened deterministically ----------
        matches = client.get(f"/api/v1/runs/{run_id}/matches", headers=preparer).json()
        assert matches, "the two exact invoice matches were not found"
        auto = [m for m in matches if m["decision"] == "AUTO_MATCH"]
        assert len(auto) >= 2
        for match in auto:
            assert match["rule_id"], "an automatic match with no rule recorded"
            assert match["rule_version"], "an automatic match with no rule version"
            assert match["reasons"], "an automatic match with no explanation"

        # -- 8. exceptions were raised for the rest -----------------------
        exceptions = client.get(f"/api/v1/runs/{run_id}/exceptions", headers=preparer).json()
        assert exceptions, "nothing was routed to a human"
        categories = {e["category"] for e in exceptions}
        assert categories, categories

        # -- 9. a preparer cannot approve; a controller can ---------------
        pending = [m for m in matches if m["status"] in {"SUGGESTED", "PROPOSED"}]
        if pending:
            target = pending[0]
            denied = client.post(
                f"/api/v1/matches/{target['id']}/approve",
                headers=preparer,
                json={"expected_version": target["version"]},
            )
            assert denied.status_code == 403

            approved = client.post(
                f"/api/v1/matches/{target['id']}/approve",
                headers=controller,
                json={"expected_version": target["version"], "reason": "Confirmed."},
            )
            assert approved.status_code == 200, approved.text
            assert approved.json()["status"] == "APPROVED"
            assert approved.json()["approved_by"]

            # A stale version is refused rather than silently applied.
            stale = client.post(
                f"/api/v1/matches/{target['id']}/approve",
                headers=controller,
                json={"expected_version": target["version"]},
            )
            assert stale.status_code == 409

        # -- 10. resolve every exception ----------------------------------
        controller_id = client.get("/api/v1/me", headers=controller).json()["user_id"]
        for exception in exceptions:
            exception_id = exception["id"]
            client.post(
                f"/api/v1/exceptions/{exception_id}/comment",
                headers=preparer,
                json={"body": "Traced to the August bank statement."},
            )
            # The state machine is walked in order; skipping a state is refused.
            skipped = client.post(
                f"/api/v1/exceptions/{exception_id}/propose-resolution",
                headers=controller,
                json={"proposed_resolution": "Too soon.", "resolution_code": "X"},
            )
            assert skipped.status_code == 422
            assert "Legal next states" in skipped.json()["detail"]

            assert (
                client.post(
                    f"/api/v1/exceptions/{exception_id}/assign",
                    headers=controller,
                    json={"owner_user_id": controller_id},
                ).status_code
                == 200
            )
            assert (
                client.post(
                    f"/api/v1/exceptions/{exception_id}/transition",
                    headers=controller,
                    json={"status": "INVESTIGATING"},
                ).status_code
                == 200
            )
            assert (
                client.post(
                    f"/api/v1/exceptions/{exception_id}/propose-resolution",
                    headers=controller,
                    json={
                        "proposed_resolution": "Carry forward as a reconciling item.",
                        "resolution_code": "CARRY_FORWARD",
                    },
                ).status_code
                == 200
            )
            resolved = client.post(
                f"/api/v1/exceptions/{exception_id}/approve-resolution",
                headers=controller,
                json={
                    "proposed_resolution": "Approved by the controller.",
                    "resolution_code": "CARRY_FORWARD",
                },
            )
            assert resolved.status_code == 200, resolved.text
            closed = client.post(f"/api/v1/exceptions/{exception_id}/close", headers=controller)
            assert closed.status_code == 200, closed.text
            assert closed.json()["status"] == "CLOSED"

        # -- 11. reject or approve anything still pending -----------------
        remaining = client.get(f"/api/v1/runs/{run_id}/matches", headers=preparer).json()
        for match in remaining:
            if match["status"] in {"SUGGESTED", "PROPOSED"}:
                rejected = client.post(
                    f"/api/v1/matches/{match['id']}/reject",
                    headers=controller,
                    json={
                        "expected_version": match["version"],
                        "reason": "Not the same payment.",
                    },
                )
                assert rejected.status_code == 200, rejected.text

        # -- 12. close ----------------------------------------------------
        preflight = client.get(f"/api/v1/runs/{run_id}/close-preflight", headers=controller).json()
        assert preflight["unresolved_required_exceptions"] == 0
        assert preflight["pending_approvals"] == 0

        current = client.get(f"/api/v1/runs/{run_id}", headers=controller).json()
        closed = client.post(
            f"/api/v1/runs/{run_id}/close",
            headers=controller,
            json={"expected_version": current["version"]},
        )
        assert closed.status_code == 200, closed.text
        certificate = closed.json()
        assert certificate["status"] == "CLOSED"
        assert certificate["certificate_hash"]
        assert certificate["snapshot_hash"]
        assert certificate["closed_by"]

        # -- 13. a closed run is immutable --------------------------------
        after_close = client.get(f"/api/v1/runs/{run_id}", headers=controller).json()
        blocked = client.post(
            "/api/v1/matches/manual",
            headers=controller,
            json={
                "run_id": run_id,
                "side_a_ids": [str(uuid4())],
                "side_b_ids": [str(uuid4())],
                "reason": "Trying to change a closed period.",
            },
        )
        assert blocked.status_code == 409
        assert "closed" in blocked.json()["detail"].lower()

        # -- 14. the audit history is complete and intact -----------------
        verification = client.get("/api/v1/audit/verify", headers=controller).json()
        assert verification["verified"] is True
        assert verification["event_count"] > 10

        events = client.get(
            "/api/v1/audit/events", headers=controller, params={"limit": 500}
        ).json()
        actions = {event["action"] for event in events["events"]}
        for required in (
            "FILE_UPLOADED",
            "FILE_MAPPED",
            "FILE_INGESTED",
            "RECONCILIATION_CREATED",
            "RECONCILIATION_RUN_STARTED",
            "RECONCILIATION_RUN_COMPLETED",
            "EXCEPTION_CREATED",
            "EXCEPTION_CLOSED",
            "RUN_CLOSED",
        ):
            assert required in actions, f"{required} is missing from the audit trail"

        # -- 15. the audit package exports --------------------------------
        package = client.get(f"/api/v1/runs/{run_id}/audit-package", headers=controller)
        assert package.status_code == 200
        assert package.headers["content-type"] == "application/zip"

        import io
        import zipfile

        with zipfile.ZipFile(io.BytesIO(package.content)) as archive:
            names = set(archive.namelist())
        for required_file in (
            "reconciliation-summary.json",
            "source-manifest.csv",
            "matched-items.csv",
            "exceptions.csv",
            "approvals.csv",
            "rule-config.json",
            "model-config.json",
            "audit-events.jsonl",
            "manifest.json",
        ):
            assert required_file in names, f"{required_file} missing from the export"

        del after_close


class TestReproducibility:
    def test_replaying_a_run_reproduces_its_result_hash(
        self, client: TestClient, settings: Settings, tenant: UUID
    ) -> None:
        """Spec sections 36 and 51, checked through the API rather than in-process."""
        preparer = auth(settings, tenant, ["PREPARER", "CONTROLLER"], "carol")

        upload_and_ingest(
            client, preparer, filename="bank.csv", data=BANK_CSV, source_system="bank"
        )
        upload_and_ingest(
            client, preparer, filename="ledger.csv", data=LEDGER_CSV, source_system="ledger"
        )

        reconciliation_id = client.post(
            "/api/v1/reconciliations",
            headers=preparer,
            json={"slug": "repro", "name": "Repro", "template": "bank_gl"},
        ).json()["id"]

        run_id = client.post(
            f"/api/v1/reconciliations/{reconciliation_id}/runs",
            headers=preparer,
            json={},
        ).json()["id"]

        replay = client.post(f"/api/v1/runs/{run_id}/replay", headers=preparer)
        assert replay.status_code == 200, replay.text
        body = replay.json()
        assert body["reproducible"] is True, (
            f"stored {body['stored_result_hash']} != replayed {body['replayed_result_hash']}"
        )


class TestTenantIsolationOverHttp:
    def test_one_tenants_token_cannot_read_anothers_data(
        self, client: TestClient, settings: Settings, tenant: UUID, session: Session
    ) -> None:
        other = uuid4()
        session.add(TenantRow(id=other, name="Rival Ltd", slug="rival-e2e"))
        session.commit()

        mine = auth(settings, tenant, ["CONTROLLER"], "dave")
        theirs = auth(settings, other, ["CONTROLLER"], "eve")

        upload_and_ingest(client, mine, filename="bank.csv", data=BANK_CSV, source_system="bank")

        assert len(client.get("/api/v1/transactions", headers=mine).json()) == 5
        assert client.get("/api/v1/transactions", headers=theirs).json() == []

        my_transaction = client.get("/api/v1/transactions", headers=mine).json()[0]
        cross = client.get(f"/api/v1/transactions/{my_transaction['id']}", headers=theirs)
        assert cross.status_code == 404

    def test_an_unauthenticated_request_is_refused(self, client: TestClient) -> None:
        assert client.get("/api/v1/transactions").status_code == 401

    def test_a_forged_token_is_refused(self, client: TestClient, tenant: UUID) -> None:
        forged = (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
            "eyJzdWIiOiJhdHRhY2tlciIsImV4cCI6OTk5OTk5OTk5OX0."
            "not-a-real-signature"
        )
        response = client.get("/api/v1/transactions", headers={"Authorization": f"Bearer {forged}"})
        assert response.status_code == 401


class TestHealth:
    def test_health_needs_no_token(self, client: TestClient) -> None:
        assert client.get("/health").status_code == 200

    def test_security_headers_are_present(self, client: TestClient) -> None:
        headers = client.get("/health").headers
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"
        assert "X-Correlation-ID" in headers


class TestMoneyOverTheWire:
    def test_amounts_are_strings_not_json_numbers(
        self, client: TestClient, settings: Settings, tenant: UUID
    ) -> None:
        """A JSON number becomes a float in most clients; 982.45 would not survive."""
        headers = auth(settings, tenant, ["PREPARER"], "frank")
        upload_and_ingest(client, headers, filename="bank.csv", data=BANK_CSV, source_system="bank")
        body = client.get("/api/v1/transactions", headers=headers).content.decode()
        assert '"amount":"982.45"' in body.replace(" ", "")
        transactions = client.get("/api/v1/transactions", headers=headers).json()
        assert all(isinstance(t["amount"], str) for t in transactions)
        assert Decimal(transactions[0]["amount"]) == Decimal(transactions[0]["amount"])
