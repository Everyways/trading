"""Tests for DataIngestionService — uses DummyProvider + in-memory SQLite."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

# Import DummyProvider to register it
import tests.fixtures.dummy_provider.provider  # noqa: F401
from app.data.ingestion import DataIngestionService
from app.data.repositories import InstrumentRepository, OHLCVRepository

NOW = datetime(2024, 1, 15, tzinfo=UTC)
END = NOW + timedelta(days=1)


def _make_provider():
    from app.core.registry import broker_registry
    return broker_registry.get("dummy")()


# ---------------------------------------------------------------------------
# ingest()
# ---------------------------------------------------------------------------


class TestIngestionServiceIngest:
    @pytest.mark.asyncio
    async def test_ingest_inserts_candles(self, db_session):
        provider = _make_provider()
        service = DataIngestionService(provider, db_session)

        count = await service.ingest("SPY", "1h", NOW, END)

        assert count > 0

    @pytest.mark.asyncio
    async def test_ingest_creates_instrument_record(self, db_session):
        provider = _make_provider()
        service = DataIngestionService(provider, db_session)

        await service.ingest("QQQ", "1h", NOW, END)

        repo = InstrumentRepository(db_session)
        instrument = repo.get("QQQ", "dummy")
        assert instrument is not None
        assert instrument.symbol == "QQQ"

    @pytest.mark.asyncio
    async def test_ingest_idempotent(self, db_session):
        provider = _make_provider()
        service = DataIngestionService(provider, db_session)

        first = await service.ingest("SPY", "1h", NOW, END)
        second = await service.ingest("SPY", "1h", NOW, END)

        assert first > 0
        assert second == 0  # all rows already exist

    @pytest.mark.asyncio
    async def test_ingest_persists_to_db(self, db_session):
        provider = _make_provider()
        service = DataIngestionService(provider, db_session)

        await service.ingest("SPY", "1h", NOW, END)

        instrument_repo = InstrumentRepository(db_session)
        instrument = instrument_repo.get("SPY", "dummy")
        assert instrument is not None

        ohlcv_repo = OHLCVRepository(db_session)
        rows = ohlcv_repo.query(instrument.id, "1h", NOW, END)
        assert len(rows) > 0
        for row in rows:
            assert isinstance(row.close, Decimal)
            assert row.source == "rest"

    @pytest.mark.asyncio
    async def test_ingest_empty_range_returns_zero(self, db_session):
        provider = _make_provider()
        service = DataIngestionService(provider, db_session)

        # empty range: start == end
        count = await service.ingest("SPY", "1h", NOW, NOW)
        assert count == 0


# ---------------------------------------------------------------------------
# backfill()
# ---------------------------------------------------------------------------


class TestIngestionServiceBackfill:
    @pytest.mark.asyncio
    async def test_backfill_returns_counts_per_symbol(self, db_session):
        provider = _make_provider()
        service = DataIngestionService(provider, db_session)

        results = await service.backfill(["SPY", "QQQ"], timeframe="1h", years=1)

        assert "SPY" in results
        assert "QQQ" in results
        assert results["SPY"] > 0
        assert results["QQQ"] > 0

    @pytest.mark.asyncio
    async def test_backfill_idempotent(self, db_session):
        provider = _make_provider()
        service = DataIngestionService(provider, db_session)

        start = datetime(2024, 1, 1, tzinfo=UTC)
        end = datetime(2024, 2, 1, tzinfo=UTC)

        first = await service.backfill(["SPY"], timeframe="1h", years=1, start=start, end=end)
        second = await service.backfill(["SPY"], timeframe="1h", years=1, start=start, end=end)

        assert first["SPY"] > 0
        assert second["SPY"] == 0

    @pytest.mark.asyncio
    async def test_backfill_chunk_splits_correctly(self, db_session):
        """Verify chunking doesn't miss or duplicate bars at chunk boundaries."""
        provider = _make_provider()
        service = DataIngestionService(provider, db_session)

        start = datetime(2024, 1, 1, tzinfo=UTC)
        end = datetime(2024, 4, 1, tzinfo=UTC)  # 3 months → ~3 chunks of 30 days

        results = await service.backfill(
            ["SPY"], timeframe="1h", years=1, chunk_days=30, start=start, end=end
        )
        total = results["SPY"]

        # Verify no duplicate rows were created
        instrument_repo = InstrumentRepository(db_session)
        instrument = instrument_repo.get("SPY", "dummy")
        assert instrument is not None

        ohlcv_repo = OHLCVRepository(db_session)
        rows = ohlcv_repo.query(instrument.id, "1h", start, end)
        assert len(rows) == total  # no duplicates
