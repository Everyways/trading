"""DummyProvider — minimal BrokerProvider for contract testing (§17.4).

Importing this module triggers registration via the decorator.
No other file needs to be modified to add this provider.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.core.domain import (
    Account,
    Candle,
    Fill,
    Instrument,
    OrderAck,
    OrderRequest,
    Position,
)
from app.core.enums import AssetClass, OrderSide, OrderStatus, OrderType
from app.core.registry import broker_registry
from app.providers.base import BrokerProvider
from app.providers.capabilities import BrokerCapabilities

_D = Decimal


@broker_registry.register("dummy")
class DummyProvider(BrokerProvider):
    """In-memory broker provider for tests. Simulates a paper account."""

    name = "dummy"
    capabilities = BrokerCapabilities()

    def __init__(self) -> None:
        self._connected = False

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def healthcheck(self) -> bool:
        return True

    async def get_account(self) -> Account:
        return Account(
            equity=_D("10000"),
            cash=_D("10000"),
            buying_power=_D("10000"),
        )

    async def get_positions(self) -> list[Position]:
        return []

    async def get_position(self, symbol: str) -> Position | None:
        return None

    async def submit_order(self, order: OrderRequest) -> OrderAck:
        return OrderAck(
            client_order_id=order.client_order_id,
            broker_order_id=str(uuid.uuid4()),
            status=OrderStatus.FILLED,
            symbol=order.symbol,
            side=order.side,
            type=order.type,
            qty=order.qty,
            filled_qty=order.qty,
            avg_fill_price=order.limit_price or _D("100"),
            submitted_at=datetime.now(tz=UTC),
            filled_at=datetime.now(tz=UTC),
        )

    async def cancel_order(self, broker_order_id: str) -> None:
        pass

    async def list_open_orders(self, symbol: str | None = None) -> list[OrderAck]:
        return []

    async def get_order(self, broker_order_id: str) -> OrderAck:
        return OrderAck(
            client_order_id=str(uuid.uuid4()),
            broker_order_id=broker_order_id,
            status=OrderStatus.FILLED,
            symbol="DUMMY",
            side=OrderSide.BUY,
            type=OrderType.MARKET,
            qty=_D("1"),
            filled_qty=_D("1"),
        )

    async def get_historical_candles(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> list[Candle]:
        return _generate_synthetic_candles(symbol, timeframe, start, end)

    async def stream_fills(self) -> AsyncIterator[Fill]:  # type: ignore[override]
        return
        yield  # makes this an async generator

    async def stream_candles(  # type: ignore[override]
        self,
        symbols: list[str],
        timeframe: str,
    ) -> AsyncIterator[Candle]:
        return
        yield  # makes this an async generator

    async def list_tradable_instruments(self) -> list[Instrument]:
        return [
            Instrument(
                symbol="SPY",
                asset_class=AssetClass.EQUITY,
                provider_name="dummy",
            )
        ]


def _generate_synthetic_candles(
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
) -> list[Candle]:
    """Generate deterministic OHLCV candles for testing."""
    # Parse timeframe to delta
    delta_map = {
        "1m": timedelta(minutes=1),
        "5m": timedelta(minutes=5),
        "15m": timedelta(minutes=15),
        "30m": timedelta(minutes=30),
        "1h": timedelta(hours=1),
        "4h": timedelta(hours=4),
        "1d": timedelta(days=1),
    }
    delta = delta_map.get(timeframe, timedelta(days=1))

    candles = []
    current = start
    base_price = _D("100")
    while current < end:
        open_ = base_price
        close = base_price + _D("1")
        high = close + _D("0.5")
        low = open_ - _D("0.5")
        candles.append(
            Candle(
                time=current,
                symbol=symbol,
                timeframe=timeframe,
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=_D("10000"),
                is_closed=True,
            )
        )
        current += delta
        base_price = close
    return candles
