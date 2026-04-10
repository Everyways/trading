"""DummyProvider contract tests — §17.4.

Verifies that importing and registering a new provider requires only a single
file in tests/fixtures/dummy_provider/. No other file may be modified.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

# Importing the provider module triggers @broker_registry.register("dummy")
import tests.fixtures.dummy_provider.provider  # noqa: F401
from app.core.domain import Candle
from app.core.registry import broker_registry

# ---------------------------------------------------------------------------
# Registration contract
# ---------------------------------------------------------------------------


def test_dummy_is_registered():
    """Importing the fixture module registers "dummy" in the broker registry."""
    assert "dummy" in broker_registry


def test_dummy_registry_returns_correct_class():
    from tests.fixtures.dummy_provider.provider import DummyProvider
    assert broker_registry.get("dummy") is DummyProvider


def test_dummy_provider_instantiable():
    cls = broker_registry.get("dummy")
    provider = cls()
    assert provider is not None


# ---------------------------------------------------------------------------
# Interface contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_healthcheck_returns_true():
    cls = broker_registry.get("dummy")
    provider = cls()
    result = await provider.healthcheck()
    assert result is True


@pytest.mark.asyncio
async def test_get_account_returns_account():
    from app.core.domain import Account
    cls = broker_registry.get("dummy")
    provider = cls()
    account = await provider.get_account()
    assert isinstance(account, Account)
    assert account.equity > Decimal("0")


@pytest.mark.asyncio
async def test_get_positions_returns_list():
    cls = broker_registry.get("dummy")
    provider = cls()
    positions = await provider.get_positions()
    assert isinstance(positions, list)


@pytest.mark.asyncio
async def test_get_historical_candles_returns_candles():
    cls = broker_registry.get("dummy")
    provider = cls()

    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2024, 1, 2, tzinfo=UTC)

    candles = await provider.get_historical_candles("SPY", "1h", start, end)

    assert isinstance(candles, list)
    assert len(candles) > 0
    for candle in candles:
        assert isinstance(candle, Candle)
        assert candle.is_closed is True
        assert candle.symbol == "SPY"
        assert candle.timeframe == "1h"
        assert candle.high >= candle.open
        assert candle.high >= candle.close
        assert candle.low <= candle.open
        assert candle.low <= candle.close


@pytest.mark.asyncio
async def test_submit_order_returns_order_ack():
    from app.core.domain import OrderAck, OrderRequest
    from app.core.enums import OrderSide, OrderStatus, OrderType
    cls = broker_registry.get("dummy")
    provider = cls()
    req = OrderRequest(
        symbol="SPY",
        side=OrderSide.BUY,
        type=OrderType.MARKET,
        qty=Decimal("1"),
    )
    ack = await provider.submit_order(req)
    assert isinstance(ack, OrderAck)
    assert ack.client_order_id == req.client_order_id
    assert ack.status == OrderStatus.FILLED


@pytest.mark.asyncio
async def test_list_tradable_instruments_returns_instruments():
    from app.core.domain import Instrument
    cls = broker_registry.get("dummy")
    provider = cls()
    instruments = await provider.list_tradable_instruments()
    assert isinstance(instruments, list)
    assert len(instruments) > 0
    assert all(isinstance(i, Instrument) for i in instruments)


@pytest.mark.asyncio
async def test_stream_fills_is_empty_async_generator():
    cls = broker_registry.get("dummy")
    provider = cls()
    fills = []
    async for fill in provider.stream_fills():
        fills.append(fill)
    assert fills == []


@pytest.mark.asyncio
async def test_stream_candles_is_empty_async_generator():
    cls = broker_registry.get("dummy")
    provider = cls()
    candles = []
    async for candle in provider.stream_candles(["SPY"], "1m"):
        candles.append(candle)
    assert candles == []
