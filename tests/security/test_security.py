"""Security boundary tests exercised through the real FastAPI application."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID, uuid4

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from apps.api.app.config import Settings
from apps.api.app.infrastructure.auth import ROLES_CLAIM, issue_dev_token
from apps.api.app.infrastructure.mappers import transaction_to_row
from apps.api.app.infrastructure.models import (
    ExceptionRow,
    MatchGroupRow,
    ReconciliationRow,
    RunRow,
    TenantRow,
    UserRow,
)
from apps.api.app.infrastructure.storage import (
    LocalObjectStorage,
    safe_filename,
    storage_key,
)
from packages.audit.evidence import EvidenceMetadata
from packages.matching.templates import bank_gl_template
from tests.factories import make_transaction


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        ENVIRONMENT="test",
        DATABASE_URL="sqlite+pysqlite:///:memory:",
        AUTH_DEV_SECRET="security-test-secret-value-at-least-32-chars",
        AUTH_AUDIENCE="",
        AI_ENABLED=False,
        LOCAL_STORAGE_PATH=str(tmp_path / "storage"),
        OBJECT_STORAGE_ENDPOINT="",
        CREDENTIAL_ENCRYPTION_KEY="test-credential-key-at-least-32-characters",
        MAX_UPLOAD_BYTES=128,
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
    tenant_id = uuid4()
    session.add(TenantRow(id=tenant_id, name="Security Tenant", slug=f"security-{tenant_id}"))
    session.commit()
    return tenant_id


def auth(settings: Settings, tenant_id: UUID, roles: list[str], subject: str) -> dict[str, str]:
    token = issue_dev_token(
        settings,
        subject=subject,
        tenant_id=tenant_id,
        roles=roles,
        email=f"{subject}@example.com",
    )
    return {"Authorization": f"Bearer {token}"}


def _foreign_resources(session: Session) -> tuple[UUID, UUID, UUID, UUID]:
    """Create one transaction, run, match and exception owned by another tenant."""

    foreign_tenant = uuid4()
    reconciliation_id = uuid4()
    run_id = uuid4()
    match_id = uuid4()
    exception_id = uuid4()
    transaction = make_transaction("FOREIGN-TX", "10.00", tenant_id=foreign_tenant)
    config, _ = bank_gl_template()

    session.add(
        TenantRow(id=foreign_tenant, name="Foreign Tenant", slug=f"foreign-{foreign_tenant}")
    )
    session.flush()
    session.add(transaction_to_row(transaction))
    session.add(
        ReconciliationRow(
            id=reconciliation_id,
            tenant_id=foreign_tenant,
            slug="foreign-reconciliation",
            name="Foreign reconciliation",
            template="bank_gl",
            config=config.model_dump(mode="json"),
            config_version="v1",
            config_hash="foreign-config",
            entity="foreign",
        )
    )
    session.flush()
    session.add(
        RunRow(
            id=run_id,
            tenant_id=foreign_tenant,
            reconciliation_id=reconciliation_id,
            status="REVIEW_REQUIRED",
        )
    )
    session.flush()
    session.add(
        MatchGroupRow(
            id=match_id,
            tenant_id=foreign_tenant,
            run_id=run_id,
            reconciliation_id=reconciliation_id,
            cardinality="1:1",
            status="SUGGESTED",
            decision="SUGGEST",
            confidence=0.9,
            score=0.9,
            currency="EUR",
            total_amount="10.00",
            engine_stage="scoring",
        )
    )
    session.add(
        ExceptionRow(
            id=exception_id,
            tenant_id=foreign_tenant,
            reconciliation_run_id=run_id,
            transaction_ids=[str(transaction.id)],
            category="UNMATCHED_ITEM",
            severity="MEDIUM",
            status="OPEN",
            title="Foreign exception",
            detail="Belongs to another tenant",
        )
    )
    session.commit()
    return transaction.id, run_id, match_id, exception_id


class TestJWTValidation:
    def test_expired_jwt_is_rejected(
        self, client: TestClient, settings: Settings, tenant: UUID
    ) -> None:
        token = issue_dev_token(
            settings,
            subject="expired-user",
            tenant_id=tenant,
            roles=["VIEWER"],
            ttl_seconds=-10,
        )
        response = client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401

    def test_wrong_signing_secret_is_rejected(
        self, client: TestClient, settings: Settings, tenant: UUID
    ) -> None:
        wrong_settings = settings.model_copy(
            update={"auth_dev_secret": "different-secret-value-at-least-32-characters"}
        )
        token = issue_dev_token(
            wrong_settings,
            subject="forged-user",
            tenant_id=tenant,
            roles=["VIEWER"],
        )
        response = client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401

    def test_jwt_without_tenant_claim_is_rejected(
        self, client: TestClient, settings: Settings
    ) -> None:
        now = int(time.time())
        token = jwt.encode(
            {"sub": "tenantless", "iat": now, "exp": now + 300, ROLES_CLAIM: ["VIEWER"]},
            settings.auth_dev_secret,
            algorithm="HS256",
        )
        response = client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code in {401, 403}

    def test_unknown_tenant_is_rejected(self, client: TestClient, settings: Settings) -> None:
        headers = auth(settings, uuid4(), ["VIEWER"], "unknown-tenant-user")
        assert client.get("/api/v1/me", headers=headers).status_code == 403

    def test_deactivated_user_is_rejected(
        self,
        client: TestClient,
        settings: Settings,
        tenant: UUID,
        session: Session,
    ) -> None:
        subject = "deactivated-user"
        session.add(
            UserRow(
                id=uuid4(),
                tenant_id=tenant,
                external_subject=subject,
                email="deactivated@example.com",
                is_active=False,
            )
        )
        session.commit()
        headers = auth(settings, tenant, ["VIEWER"], subject)
        assert client.get("/api/v1/me", headers=headers).status_code == 403


class TestTenantIsolation:
    def test_cross_tenant_resources_cannot_be_read_or_approved(
        self,
        client: TestClient,
        settings: Settings,
        tenant: UUID,
        session: Session,
    ) -> None:
        transaction_id, run_id, match_id, exception_id = _foreign_resources(session)
        headers = auth(settings, tenant, ["CONTROLLER"], "local-controller")

        attempts = [
            client.get(f"/api/v1/transactions/{transaction_id}", headers=headers),
            client.get(f"/api/v1/runs/{run_id}", headers=headers),
            client.post(
                f"/api/v1/matches/{match_id}/approve",
                headers=headers,
                json={"expected_version": 1, "reason": "Must not cross tenants"},
            ),
            client.get(f"/api/v1/exceptions/{exception_id}", headers=headers),
        ]

        assert [response.status_code for response in attempts] == [404, 404, 404, 404]


class TestUploadSecurity:
    def test_path_traversal_filename_cannot_escape_storage(
        self,
        client: TestClient,
        settings: Settings,
        tenant: UUID,
        tmp_path: Path,
    ) -> None:
        headers = auth(settings, tenant, ["PREPARER"], "uploader")
        response = client.post(
            "/api/v1/files",
            headers=headers,
            files={"file": ("../../outside.csv", b"Amount,Currency\n1.00,EUR\n", "text/csv")},
        )
        assert response.status_code == 201, response.text
        assert response.json()["filename"] == safe_filename("../../outside.csv")
        assert not (tmp_path / "outside.csv").exists()

        key = storage_key(tenant, "uploads", "a" * 64, extension="../../exe")
        storage = LocalObjectStorage(tmp_path / "bounded-storage")
        assert storage._path(key).is_relative_to(storage.root)
        with pytest.raises(ValueError, match="escapes"):
            storage.put("../../outside", b"blocked")

    @pytest.mark.parametrize("signature", [b"MZ", b"\x7fELF"])
    def test_executable_upload_is_refused(
        self,
        signature: bytes,
        client: TestClient,
        settings: Settings,
        tenant: UUID,
    ) -> None:
        headers = auth(settings, tenant, ["PREPARER"], f"executable-{signature.hex()}")
        response = client.post(
            "/api/v1/files",
            headers=headers,
            files={"file": ("malware.csv", signature + b" payload", "text/csv")},
        )
        assert response.status_code == 422

    def test_oversized_upload_is_refused_with_413(
        self, client: TestClient, settings: Settings, tenant: UUID
    ) -> None:
        headers = auth(settings, tenant, ["PREPARER"], "oversized-uploader")
        response = client.post(
            "/api/v1/files",
            headers=headers,
            files={"file": ("large.csv", b"x" * 129, "text/csv")},
        )
        assert response.status_code == 413

    def test_disallowed_evidence_mime_type_is_refused(self) -> None:
        with pytest.raises(ValueError, match="not allowed"):
            EvidenceMetadata.inspect("payload.exe", "application/x-msdownload", b"MZ")


class TestResponseAndAuditSecurity:
    def test_all_security_headers_are_present(self, client: TestClient) -> None:
        response = client.get("/health")
        expected = {
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
            "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
            "Cache-Control": "no-store",
        }
        for header, value in expected.items():
            assert response.headers[header] == value

    def test_request_secret_never_reaches_audit_metadata(
        self, client: TestClient, settings: Settings, tenant: UUID
    ) -> None:
        secret = "sk_live_NEVER_STORE_THIS_VALUE"
        viewer = auth(settings, tenant, ["VIEWER"], "denied-viewer")
        denied = client.post(
            f"/api/v1/matches/{uuid4()}/approve",
            headers=viewer,
            json={"expected_version": 1, "reason": secret},
        )
        assert denied.status_code == 403

        controller = auth(settings, tenant, ["CONTROLLER"], "audit-controller")
        events = client.get("/api/v1/audit/events", headers=controller, params={"limit": 500})
        assert events.status_code == 200
        assert secret not in json.dumps(events.json())


def test_production_refuses_development_auth_secret() -> None:
    with pytest.raises(ValueError, match="AUTH_DEV_SECRET"):
        Settings(ENVIRONMENT="production", AUTH_DEV_SECRET="x")
