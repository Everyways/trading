"""AlpacaProvider — BrokerProvider implementation for Alpaca Markets.

All REST calls are synchronous in alpaca-py and wrapped with asyncio.to_thread().
Streaming (WebSocket) is blocking and bridged via a thread + asyncio.Queue.

Registered as "alpaca" in the broker registry.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.live import StockDataStream
from alpaca.data.models import Bar as AlpacaBar
from alpaca.data.requests import StockBarsRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.models import TradeUpdate
from alpaca.trading.stream import TradingStream

from app.core.domain import (
    Account,
    Candle,
    Fill,
    Instrument,
    OrderAck,
    OrderRequest,
    Position,
)
from app.core.registry import broker_registry
from app.providers.alpaca.config import AlpacaConfig
from app.providers.alpaca.mappers import (
    account_to_domain,
    asset_to_instrument,
    bar_to_candle,
    order_request_to_alpaca,
    order_to_ack,
    position_to_domain,
    timeframe_str_to_alpaca,
    trade_update_to_fill,
)
from app.providers.base import BrokerProvider
from app.providers.capabilities import BrokerCapabilities

log = logging.getLogger(__name__)


@broker_registry.register("alpaca")
class AlpacaProvider(BrokerProvider):
    """Alpaca Markets broker provider (paper + live, US equities)."""

    name = "alpaca"

    def __init__(self, config: AlpacaConfig | None = None) -> None:
        self._config = config or AlpacaConfig()
        self._client: TradingClient | None = None
        self._data_client: StockHistoricalDataClient | None = None
        self.capabilities = BrokerCapabilities(
            supports_fractional=True,
            supports_short=True,
            supports_stop_orders=True,
            supports_stop_limit_orders=True,
            supports_bracket_orders=True,
            asset_classes=["equity"],
            min_order_value_usd=Decimal("1"),
            max_order_value_usd=Decimal("500000"),
            order_rate_limit_per_minute=200,
            supports_extended_hours=True,
            timezone="America/New_York",
        )

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Instantiate and authenticate the Alpaca REST clients."""
        paper = self._config.is_paper
        self._client = TradingClient(
            api_key=self._config.alpaca_api_key,
            secret_key=self._config.alpaca_api_secret,
            paper=paper,
        )
        self._data_client = StockHistoricalDataClient(
            api_key=self._config.alpaca_api_key,
            secret_key=self._config.alpaca_api_secret,
        )
        log.info("AlpacaProvider connected (paper=%s)", paper)

    async def disconnect(self) -> None:
        """Release client references."""
        self._client = None
        self._data_client = None
        log.info("AlpacaProvider disconnected")

    # ------------------------------------------------------------------
    # Account & positions
    # ------------------------------------------------------------------

    async def get_account(self) -> Account:
        assert self._client is not None, "Call connect() first"
        raw = await asyncio.to_thread(self._client.get_account)
        return account_to_domain(raw)

    async def get_positions(self) -> list[Position]:
        assert self._client is not None, "Call connect() first"
        raw_list = await asyncio.to_thread(self._client.get_all_positions)
        return [position_to_domain(p) for p in raw_list]

    async def get_position(self, symbol: str) -> Position | None:
        assert self._client is not None, "Call connect() first"
        try:
            raw = await asyncio.to_thread(self._client.get_open_position, symbol)
            return position_to_domain(raw)
        except Exception as exc:
            # Alpaca raises APIError (404) when no position exists for the symbol
            if "position does not exist" in str(exc).lower() or "404" in str(exc):
                return None
            raise

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    async def submit_order(self, order: OrderRequest) -> OrderAck:
        """Submit an order. client_order_id is generated before this call
        (in OrderRequest.__init__) ensuring idempotence even on retry."""
        assert self._client is not None, "Call connect() first"
        alpaca_req = order_request_to_alpaca(order)
        raw = await asyncio.to_thread(self._client.submit_order, alpaca_req)
        return order_to_ack(raw)

    async def cancel_order(self, broker_order_id: str) -> None:
        assert self._client is not None, "Call connect() first"
        await asyncio.to_thread(self._client.cancel_order_by_id, broker_order_id)

    async def get_order(self, broker_order_id: str) -> OrderAck:
        assert self._client is not None, "Call connect() first"
        raw = await asyncio.to_thread(self._client.get_order_by_id, broker_order_id)
        return order_to_ack(raw)

    # ------------------------------------------------------------------
    # Historical data
    # ------------------------------------------------------------------

    async def get_historical_candles(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> list[Candle]:
        assert self._data_client is not None, "Call connect() first"
        alpaca_tf = timeframe_str_to_alpaca(timeframe)
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=alpaca_tf,
            start=start,
            end=end,
        )
        bars_response = await asyncio.to_thread(self._data_client.get_stock_bars, req)
        bars: list[AlpacaBar] = bars_response.get(symbol, [])
        return [bar_to_candle(b, symbol, timeframe, is_closed=True) for b in bars]

    # ------------------------------------------------------------------
    # Streaming — fills
    # ------------------------------------------------------------------

    async def stream_fills(self) -> AsyncIterator[Fill]:
        """Yield fills from the Alpaca trade update stream.

        TradingStream.run() is blocking → executed in a daemon thread.
        A thread-safe asyncio.Queue bridges the two worlds.
        """
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Fill | None] = asyncio.Queue(maxsize=500)

        stream = TradingStream(
            api_key=self._config.alpaca_api_key,
            secret_key=self._config.alpaca_api_secret,
            paper=self._config.is_paper,
        )

        async def on_update(tu: TradeUpdate) -> None:
            fill = trade_update_to_fill(tu)
            if fill:
                loop.call_soon_threadsafe(queue.put_nowait, fill)

        stream.subscribe_trade_updates(on_update)
        thread = threading.Thread(target=stream.run, daemon=True, name="alpaca-trade-stream")
        thread.start()
        log.info("AlpacaProvider: trade update stream started")

        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield item
        finally:
            stream.stop()
            log.info("AlpacaProvider: trade update stream stopped")

    # ------------------------------------------------------------------
    # Streaming — candles
    # ------------------------------------------------------------------

    async def stream_candles(
        self,
        symbols: list[str],
        timeframe: str,
    ) -> AsyncIterator[Candle]:
        """Yield closed candles from the Alpaca market data stream.

        Note: Alpaca streams 1m bars natively. For timeframes > 1m the
        trading runner polls historical data at bar-close instead.
        StockDataStream.run() is blocking → daemon thread + asyncio.Queue.
        """
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Candle | None] = asyncio.Queue(maxsize=2000)

        stream = StockDataStream(
            api_key=self._config.alpaca_api_key,
            secret_key=self._config.alpaca_api_secret,
        )

        async def on_bar(bar: AlpacaBar) -> None:
            candle = bar_to_candle(bar, bar.symbol, timeframe, is_closed=True)
            loop.call_soon_threadsafe(queue.put_nowait, candle)

        stream.subscribe_bars(on_bar, *symbols)
        thread = threading.Thread(target=stream.run, daemon=True, name="alpaca-data-stream")
        thread.start()
        log.info("AlpacaProvider: market data stream started for %s", symbols)

        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield item
        finally:
            stream.stop()
            log.info("AlpacaProvider: market data stream stopped")

    # ------------------------------------------------------------------
    # Instruments
    # ------------------------------------------------------------------

    async def list_tradable_instruments(self) -> list[Instrument]:
        assert self._client is not None, "Call connect() first"
        from alpaca.trading.enums import AssetClass as AlpacaAssetClass
        from alpaca.trading.enums import AssetStatus
        from alpaca.trading.requests import GetAssetsRequest

        req = GetAssetsRequest(
            status=AssetStatus.ACTIVE,
            asset_class=AlpacaAssetClass.US_EQUITY,
        )
        raw_assets = await asyncio.to_thread(self._client.get_all_assets, req)
        return [asset_to_instrument(a) for a in raw_assets if a.tradable]

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    async def healthcheck(self) -> bool:
        """Return True if the Alpaca clock endpoint responds successfully."""
        if self._client is None:
            return False
        try:
            await asyncio.to_thread(self._client.get_clock)
            return True
        except Exception as exc:
            log.warning("AlpacaProvider healthcheck failed: %s", exc)
            return False
