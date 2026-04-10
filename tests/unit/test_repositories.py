"""Tests for InstrumentRepository and OHLCVRepository — uses in-memory SQLite."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.core.domain import Instrument as DomainInstrument
from app.core.enums import AssetClass
from app.data.repositories import InstrumentRepository, OHLCVRepository

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NOW = datetime(2024, 1, 15, 14, 30, tzinfo=UTC)


def _domain_instrument(
    symbol: str = "SPY",
    provider: str = "alpaca",
    asset_class: AssetClass = AssetClass.EQUITY,
) -> DomainInstrument:
    return DomainInstrument(
        symbol=symbol,
        asset_class=asset_class,
        provider_name=provider,
    )


def _make_candles(
    symbol: str,
    timeframe: str,
    start: datetime,
    count: int,
    delta: timedelta = timedelta(minutes=15),
) -> list:
    """Generate minimal domain Candle objects for testing."""
    from app.core.domain import Candle

    candles = []
    for i in range(count):
        t = start + i * delta
        candles.append(
            Candle(
                time=t,
                symbol=symbol,
                timeframe=timeframe,
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100.5"),
                volume=Decimal("10000"),
                is_closed=True,
            )
        )
    return candles


# ---------------------------------------------------------------------------
# InstrumentRepository
# ---------------------------------------------------------------------------


class TestInstrumentRepository:
    def test_get_returns_none_when_missing(self, db_session):
        repo = InstrumentRepository(db_session)
        assert repo.get("MISSING", "alpaca") is None

    def test_upsert_creates_new_record(self, db_session):
        repo = InstrumentRepository(db_session)
        domain = _domain_instrument("SPY")
        record = repo.upsert(domain)

        assert record.id is not None
        assert record.symbol == "SPY"
        assert record.provider_name == "alpaca"
        assert record.asset_class == "equity"
        assert record.active is True

    def test_upsert_idempotent(self, db_session):
        repo = InstrumentRepository(db_session)
        domain = _domain_instrument("SPY")
        first = repo.upsert(domain)
        second = repo.upsert(domain)
        assert first.id == second.id

    def test_upsert_updates_active_flag(self, db_session):
        repo = InstrumentRepository(db_session)
        domain = _domain_instrument("SPY")
        repo.upsert(domain)

        inactive = DomainInstrument(
            symbol="SPY", asset_class=AssetClass.EQUITY,
            provider_name="alpaca", active=False,
        )
        record = repo.upsert(inactive)
        assert record.active is False

    def test_get_or_create_creates_once(self, db_session):
        repo = InstrumentRepository(db_session)
        domain = _domain_instrument("QQQ")
        first = repo.get_or_create(domain)
        second = repo.get_or_create(domain)
        assert first.id == second.id

    def test_different_providers_are_separate(self, db_session):
        repo = InstrumentRepository(db_session)
        alpaca = repo.upsert(_domain_instrument("SPY", provider="alpaca"))
        dummy = repo.upsert(_domain_instrument("SPY", provider="dummy"))
        assert alpaca.id != dummy.id

    def test_upsert_stores_tick_size(self, db_session):
        repo = InstrumentRepository(db_session)
        domain = DomainInstrument(
            symbol="AAPL",
            asset_class=AssetClass.EQUITY,
            provider_name="alpaca",
            tick_size=Decimal("0.01"),
        )
        record = repo.upsert(domain)
        assert record.tick_size == Decimal("0.01")


# ---------------------------------------------------------------------------
# OHLCVRepository
# ---------------------------------------------------------------------------


class TestOHLCVRepository:
    def _setup_instrument(self, db_session) -> int:
        """Create and return an instrument ID."""
        repo = InstrumentRepository(db_session)
        rec = repo.upsert(_domain_instrument("SPY"))
        assert rec.id is not None
        return rec.id

    def test_bulk_upsert_empty_list_returns_zero(self, db_session):
        repo = OHLCVRepository(db_session)
        assert repo.bulk_upsert([], instrument_id=1) == 0

    def test_bulk_upsert_inserts_new_candles(self, db_session):
        instrument_id = self._setup_instrument(db_session)
        candles = _make_candles("SPY", "15m", NOW, count=10)

        repo = OHLCVRepository(db_session)
        inserted = repo.bulk_upsert(candles, instrument_id)
        assert inserted == 10

    def test_bulk_upsert_skips_duplicates(self, db_session):
        instrument_id = self._setup_instrument(db_session)
        candles = _make_candles("SPY", "15m", NOW, count=5)

        repo = OHLCVRepository(db_session)
        first = repo.bulk_upsert(candles, instrument_id)
        second = repo.bulk_upsert(candles, instrument_id)

        assert first == 5
        assert second == 0  # all already exist

    def test_bulk_upsert_partial_overlap(self, db_session):
        instrument_id = self._setup_instrument(db_session)
        repo = OHLCVRepository(db_session)

        first_batch = _make_candles("SPY", "15m", NOW, count=5)
        repo.bulk_upsert(first_batch, instrument_id)

        # Overlapping batch: last 2 of first + 3 new
        second_batch = _make_candles("SPY", "15m", NOW + timedelta(minutes=15 * 3), count=5)
        inserted = repo.bulk_upsert(second_batch, instrument_id)
        assert inserted == 3  # only the 3 genuinely new ones

    def test_query_returns_candles_in_range(self, db_session):
        instrument_id = self._setup_instrument(db_session)
        candles = _make_candles("SPY", "15m", NOW, count=10)

        repo = OHLCVRepository(db_session)
        repo.bulk_upsert(candles, instrument_id)

        result = repo.query(
            instrument_id,
            timeframe="15m",
            start=NOW,
            end=NOW + timedelta(minutes=15 * 5),
        )
        assert len(result) == 5

    def test_query_ordered_by_time(self, db_session):
        instrument_id = self._setup_instrument(db_session)
        candles = _make_candles("SPY", "15m", NOW, count=5)

        repo = OHLCVRepository(db_session)
        repo.bulk_upsert(candles, instrument_id)

        result = repo.query(instrument_id, "15m", NOW, NOW + timedelta(hours=2))
        times = [r.time for r in result]
        assert times == sorted(times)

    def test_query_empty_range_returns_empty(self, db_session):
        instrument_id = self._setup_instrument(db_session)
        repo = OHLCVRepository(db_session)

        future_start = NOW + timedelta(days=365)
        result = repo.query(instrument_id, "15m", future_start, future_start + timedelta(days=1))
        assert result == []

    def test_latest_timestamp_none_when_empty(self, db_session):
        instrument_id = self._setup_instrument(db_session)
        repo = OHLCVRepository(db_session)
        assert repo.latest_timestamp(instrument_id, "15m") is None

    def test_latest_timestamp_returns_max_time(self, db_session):
        instrument_id = self._setup_instrument(db_session)
        candles = _make_candles("SPY", "15m", NOW, count=5)

        repo = OHLCVRepository(db_session)
        repo.bulk_upsert(candles, instrument_id)

        latest = repo.latest_timestamp(instrument_id, "15m")
        expected = NOW + timedelta(minutes=15 * 4)
        # SQLite strips timezone info — compare naive
        assert latest is not None
        assert latest.replace(tzinfo=None) == expected.replace(tzinfo=None)

    def test_decimal_precision_preserved(self, db_session):
        from app.core.domain import Candle

        instrument_id = self._setup_instrument(db_session)
        candle = Candle(
            time=NOW,
            symbol="SPY",
            timeframe="15m",
            open=Decimal("450.1234567890"),
            high=Decimal("455.9876543210"),
            low=Decimal("449.0000000001"),
            close=Decimal("453.5000000005"),
            volume=Decimal("1234567.89"),
        )

        repo = OHLCVRepository(db_session)
        repo.bulk_upsert([candle], instrument_id)

        rows = repo.query(instrument_id, "15m", NOW, NOW + timedelta(minutes=1))
        assert len(rows) == 1
        assert rows[0].open == Decimal("450.1234567890")
        assert rows[0].close == Decimal("453.5000000005")
