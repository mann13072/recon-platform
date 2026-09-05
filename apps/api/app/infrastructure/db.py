"""Engine and session management."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from apps.api.app.config import Settings, get_settings
from apps.api.app.infrastructure.models import Base

__all__ = ["create_all", "get_engine", "session_scope", "make_session_factory"]

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine(settings: Settings | None = None) -> Engine:
    global _engine
    if _engine is not None:
        return _engine

    settings = settings or get_settings()
    kwargs: dict[str, Any] = {"echo": settings.database_echo, "future": True}

    if settings.is_sqlite:
        # A file-backed SQLite database needs this to be usable from the
        # threadpool FastAPI runs sync endpoints in.
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        kwargs.update(pool_pre_ping=True, pool_size=10, max_overflow=20)

    _engine = create_engine(settings.database_url, **kwargs)

    if settings.is_sqlite:
        _enable_sqlite_foreign_keys(_engine)

    return _engine


def _enable_sqlite_foreign_keys(engine: Engine) -> None:
    """SQLite ignores foreign keys unless asked not to.

    Without this the test database would accept rows PostgreSQL rejects, and
    the tests would be validating a weaker schema than production runs.
    """

    @event.listens_for(engine, "connect")
    def _set_pragma(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def make_session_factory(engine: Engine | None = None) -> sessionmaker[Session]:
    global _session_factory
    if engine is not None:
        return sessionmaker(bind=engine, expire_on_commit=False, future=True)
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(), expire_on_commit=False, future=True
        )
    return _session_factory


@contextmanager
def session_scope(engine: Engine | None = None) -> Iterator[Session]:
    """A transactional scope. Commits on success, rolls back on any exception."""
    factory = make_session_factory(engine)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_all(engine: Engine | None = None) -> None:
    """Create the schema directly.

    Production uses Alembic migrations (spec section 82: migrations only). This
    exists for the test suite and for ``scripts/seed_demo.py``.
    """
    Base.metadata.create_all(engine or get_engine())


def reset_engine() -> None:
    """Drop the cached engine. Used by tests that swap the database URL."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
