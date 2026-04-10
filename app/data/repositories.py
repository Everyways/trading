"""Data-layer repositories for Instrument and OHLCV models.

Both repositories are synchronous (SQLModel Session). They can be called
from async code directly — for a 15-minute bar system the DB latency is
negligible and does not require async I/O.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlmodel import Session, select

from app.core.domain import Candle
from app.core.domain import Instrument as DomainInstrument
from app.data.models import OHLCV, Instrument


class InstrumentRepository:
    """CRUD operations for the instruments table."""

    def __init__(self, session: Session) -> None:
        self._s = session

    def get(self, symbol: str, provider_name: str) -> Instrument | None:
        """Return the DB instrument for (symbol, provider_name), or None."""
        stmt = select(Instrument).where(
            Instrument.symbol == symbol,
            Instrument.provider_name == provider_name,
        )
        return self._s.exec(stmt).first()

    def upsert(self, domain: DomainInstrument) -> Instrument:
        """Insert or update an instrument. Returns the (possibly new) DB record."""
        existing = self.get(domain.symbol, domain.provider_name)
        if existing:
            existing.active = domain.active
            if domain.tick_size is not None:
                existing.tick_size = domain.tick_size
            if domain.min_qty is not None:
                existing.min_qty = domain.min_qty
            self._s.add(existing)
            self._s.commit()
            self._s.refresh(existing)
            return existing

        record = Instrument(
            symbol=domain.symbol,
            asset_class=str(domain.asset_class.value),
            provider_name=domain.provider_name,
            tick_size=domain.tick_size,
            min_qty=domain.min_qty,
            active=domain.active,
        )
        self._s.add(record)
        self._s.commit()
        self._s.refresh(record)
        return record

    def get_or_create(self, domain: DomainInstrument) -> Instrument:
        """Return existing instrument or create it. Convenience wrapper."""
        existing = self.get(domain.symbol, domain.provider_name)
        if existing:
            return existing
        return self.upsert(domain)


def _as_naive_utc(dt: datetime) -> datetime:
    """Strip timezone for comparison — SQLite returns naive datetimes."""
    if dt.tzinfo is not None:
        return dt.astimezone(UTC).replace(tzinfo=None)
    return dt


class OHLCVRepository:
    """Read/write operations for the ohlcv table."""

    def __init__(self, session: Session) -> None:
        self._s = session

    def bulk_upsert(self, candles: list[Candle], instrument_id: int) -> int:
        """Persist a list of closed candles. Skips rows that already exist.

        Returns the number of rows actually inserted.
        """
        if not candles:
            return 0

        timeframe = candles[0].timeframe

        # Fetch timestamps already in the DB to avoid duplicates
        stmt = select(OHLCV.time).where(
            OHLCV.instrument_id == instrument_id,
            OHLCV.timeframe == timeframe,
        )
        existing: set[datetime] = {
            _as_naive_utc(row) for row in self._s.exec(stmt).all()
        }

        new_rows = [
            OHLCV(
                time=c.time,
                instrument_id=instrument_id,
                timeframe=c.timeframe,
                open=c.open,
                high=c.high,
                low=c.low,
                close=c.close,
                volume=c.volume,
                source="rest",
            )
            for c in candles
            if _as_naive_utc(c.time) not in existing
        ]

        self._s.add_all(new_rows)
        self._s.commit()
        return len(new_rows)

    def query(
        self,
        instrument_id: int,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> list[OHLCV]:
        """Return OHLCV rows in [start, end) ordered by time ascending."""
        stmt = (
            select(OHLCV)
            .where(
                OHLCV.instrument_id == instrument_id,
                OHLCV.timeframe == timeframe,
                OHLCV.time >= start,
                OHLCV.time < end,
            )
            .order_by(OHLCV.time)
        )
        return list(self._s.exec(stmt).all())

    def latest_timestamp(self, instrument_id: int, timeframe: str) -> datetime | None:
        """Return the most recent bar timestamp for an instrument+timeframe, or None."""
        from sqlalchemy import func

        stmt = select(func.max(OHLCV.time)).where(
            OHLCV.instrument_id == instrument_id,
            OHLCV.timeframe == timeframe,
        )
        return self._s.exec(stmt).first()
