"""DataIngestionService — fetches candles from a broker and persists to DB.

Usage (in scripts or the runner):
    async with ... as session:
        service = DataIngestionService(provider, session)
        count = await service.ingest("SPY", "15m", start, end)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlmodel import Session

from app.core.domain import Instrument as DomainInstrument
from app.core.enums import AssetClass
from app.data.repositories import InstrumentRepository, OHLCVRepository
from app.providers.base import BrokerProvider

log = logging.getLogger(__name__)

# Alpaca historical data API limit: max ~10,000 bars per request.
# For 15m bars over 30 days: 30 * 96 = 2,880 bars — well within limits.
_DEFAULT_CHUNK_DAYS = 30


class DataIngestionService:
    """Orchestrates fetching OHLCV bars from a broker and storing them in the DB."""

    def __init__(self, provider: BrokerProvider, session: Session) -> None:
        self._provider = provider
        self._instrument_repo = InstrumentRepository(session)
        self._ohlcv_repo = OHLCVRepository(session)

    async def ingest(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        asset_class: AssetClass = AssetClass.EQUITY,
    ) -> int:
        """Fetch candles from broker and persist to DB for one symbol.

        Returns the number of new rows inserted (0 if already up to date).
        """
        # 1. Ensure instrument record exists
        domain_instrument = DomainInstrument(
            symbol=symbol,
            asset_class=asset_class,
            provider_name=self._provider.name,
        )
        db_instrument = self._instrument_repo.get_or_create(domain_instrument)
        assert db_instrument.id is not None  # guaranteed after commit

        # 2. Fetch candles from broker
        candles = await self._provider.get_historical_candles(
            symbol, timeframe, start, end
        )
        log.debug("%s: fetched %d candles [%s → %s]", symbol, len(candles), start, end)

        # 3. Persist (skip duplicates)
        inserted = self._ohlcv_repo.bulk_upsert(candles, db_instrument.id)
        log.info("%s %s: %d/%d candles inserted", symbol, timeframe, inserted, len(candles))
        return inserted

    async def backfill(
        self,
        symbols: list[str],
        timeframe: str,
        years: int,
        asset_class: AssetClass = AssetClass.EQUITY,
        chunk_days: int = _DEFAULT_CHUNK_DAYS,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> dict[str, int]:
        """Backfill multiple symbols over the requested history.

        Splits the date range into chunks to stay within API rate limits.
        Returns a dict of {symbol: total_inserted}.
        """
        if end is None:
            end = datetime.now(tz=UTC)
        if start is None:
            start = end - timedelta(days=365 * years)

        totals: dict[str, int] = {}
        for symbol in symbols:
            total = await self._backfill_symbol(
                symbol, timeframe, start, end, asset_class, chunk_days
            )
            totals[symbol] = total
            log.info(
                "Backfill complete: %s %s %dy → %d rows total", symbol, timeframe, years, total
            )
        return totals

    async def _backfill_symbol(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        asset_class: AssetClass,
        chunk_days: int,
    ) -> int:
        """Backfill a single symbol in date chunks. Returns total rows inserted."""
        total = 0
        chunk_start = start
        while chunk_start < end:
            chunk_end = min(chunk_start + timedelta(days=chunk_days), end)
            inserted = await self.ingest(
                symbol, timeframe, chunk_start, chunk_end, asset_class
            )
            total += inserted
            chunk_start = chunk_end
        return total
