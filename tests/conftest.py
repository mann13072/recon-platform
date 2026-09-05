"""Shared fixtures.

The API tests run against an in-memory SQLite database so the whole HTTP
surface is exercised without a server. That is only meaningful because the
column types in ``apps.api.app.infrastructure.types`` behave identically on
SQLite and PostgreSQL - see ``tests/integration/test_persistence.py``, which
pins the money round-trip specifically.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("AUTH_DEV_SECRET", "test-only-secret-value-not-for-production")
os.environ.setdefault("AI_ENABLED", "false")


@pytest.fixture
def engine() -> Iterator[Engine]:
    """A fresh in-memory database per test.

    ``StaticPool`` plus a shared connection keeps every session in the same
    in-memory database; without it each connection would see an empty one.
    """
    from apps.api.app.infrastructure.models import Base

    eng = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )

    @event.listens_for(eng, "connect")
    def _fk_pragma(dbapi_connection, _record):  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    db = session_factory()
    try:
        yield db
        # A test that deliberately provoked an IntegrityError leaves the
        # transaction dead; committing it would raise from teardown and mask
        # the test's own result.
        if db.is_active:
            db.commit()
        else:
            db.rollback()
    finally:
        db.close()


@pytest.fixture
def tenant_id(session: Session) -> UUID:
    from apps.api.app.infrastructure.models import TenantRow

    identifier = uuid4()
    session.add(TenantRow(id=identifier, name="Acme GmbH", slug=f"acme-{identifier.hex[:8]}"))
    session.flush()
    return identifier


@pytest.fixture
def other_tenant_id(session: Session) -> UUID:
    from apps.api.app.infrastructure.models import TenantRow

    identifier = uuid4()
    session.add(TenantRow(id=identifier, name="Rival Ltd", slug=f"rival-{identifier.hex[:8]}"))
    session.flush()
    return identifier
