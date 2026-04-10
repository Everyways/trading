"""Database session management.

Provides a sync SQLAlchemy engine and session factory.
Tests use SQLite in-memory; production uses PostgreSQL via DATABASE_URL_SYNC.

Usage:
    from app.data.database import get_session

    with get_session() as session:
        repo = OHLCVRepository(session)
        ...
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Session

_engine: Engine | None = None


def _build_engine(database_url: str, *, sqlite_memory: bool = False) -> Engine:
    kwargs: dict = {"pool_pre_ping": True}
    if sqlite_memory:
        kwargs["connect_args"] = {"check_same_thread": False}
        kwargs["poolclass"] = StaticPool
    return create_engine(database_url, **kwargs)


def get_engine() -> Engine:
    """Return (or lazily create) the singleton sync engine from settings."""
    global _engine
    if _engine is None:
        from app.config import get_settings

        settings = get_settings()
        url = settings.database_url_sync
        _engine = _build_engine(url, sqlite_memory=url.startswith("sqlite:///:memory:"))
    return _engine


def set_engine(engine: Engine) -> None:
    """Override the global engine. Used in tests to inject a SQLite engine."""
    global _engine
    _engine = engine


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """Context manager yielding a SQLModel Session bound to the global engine."""
    with Session(get_engine()) as session:
        yield session
