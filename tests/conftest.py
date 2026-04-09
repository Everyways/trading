"""Shared test fixtures."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel


@pytest.fixture(scope="function")
def db_engine():
    """In-memory SQLite engine for unit tests (no PostgreSQL required)."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    yield engine
    SQLModel.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture(scope="function")
def db_session(db_engine):
    """SQLModel session backed by in-memory SQLite."""
    with Session(db_engine) as session:
        yield session


@pytest.fixture
def utcnow() -> datetime:
    return datetime(2026, 4, 9, 14, 0, 0, tzinfo=timezone.utc)
