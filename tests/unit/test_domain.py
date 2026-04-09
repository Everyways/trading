"""Tests for neutral domain entities (§17.2 — domain validation)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.core.domain import (
    Account,
    Candle,
    Fill,
    Instrument,
    OrderAck,
    OrderRequest,
    Position,
    Signal,
    Trade,
)
from app.core.enums import (
    AssetClass,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    SignalSide,
    TimeInForce,
)
from app.core.exceptions import LookAheadError


def _ts(hour: int = 12) -> datetime:
    return datetime(2026, 4, 9, hour, 0, 0, tzinfo=timezone.utc)


def _instrument(**kwargs) -> Instrument:  # type: ignore[return]
    defaults = dict(symbol="SPY", asset_class=AssetClass.EQUITY, provider_name="alpaca")
    defaults.update(kwargs)
    return Instrument(**defaults)


def _candle(**kwargs) -> Candle:  # type: ignore[return]
    defaults = dict(
        time=_ts(),
        symbol="SPY",
        timeframe="15m",
        open=Decimal("400.00"),
        high=Decimal("405.00"),
        low=Decimal("398.00"),
        close=Decimal("402.00"),
        volume=Decimal("1000000"),
        is_closed=True,
    )
    defaults.update(kwargs)
    return Candle(**defaults)


# ---------------------------------------------------------------------------
# Instrument
# ---------------------------------------------------------------------------

class TestInstrument:
    def test_symbol_uppercased(self) -> None:
        inst = _instrument(symbol="spy")
        assert inst.symbol == "SPY"

    def test_empty_symbol_raises(self) -> None:
        with pytest.raises(ValueError):
            _instrument(symbol="   ")

    def test_monetary_fields_are_decimal(self) -> None:
        inst = _instrument(tick_size=Decimal("0.01"), min_qty=Decimal("0.001"))
        assert isinstance(inst.tick_size, Decimal)
        assert isinstance(inst.min_qty, Decimal)


# ---------------------------------------------------------------------------
# Candle
# ---------------------------------------------------------------------------

class TestCandle:
    def test_all_prices_are_decimal(self) -> None:
        c = _candle()
        for attr in ("open", "high", "low", "close", "volume"):
            assert isinstance(getattr(c, attr), Decimal), f"{attr} should be Decimal"

    def test_assert_closed_passes_on_closed_candle(self) -> None:
        c = _candle(is_closed=True)
        c.assert_closed()  # must not raise

    def test_assert_closed_raises_on_open_candle(self) -> None:
        """Anti look-ahead guard — critical rule from §6.3."""
        c = _candle(is_closed=False)
        with pytest.raises(LookAheadError):
            c.assert_closed()

    def test_high_must_be_gte_open(self) -> None:
        with pytest.raises(ValueError):
            _candle(open=Decimal("410"), high=Decimal("405"))

    def test_high_must_be_gte_close(self) -> None:
        with pytest.raises(ValueError):
            _candle(close=Decimal("410"), high=Decimal("405"))

    def test_low_must_be_lte_open(self) -> None:
        with pytest.raises(ValueError):
            _candle(open=Decimal("395"), low=Decimal("400"))

    def test_volume_cannot_be_negative(self) -> None:
        with pytest.raises(ValueError):
            _candle(volume=Decimal("-1"))


# ---------------------------------------------------------------------------
# OrderRequest
# ---------------------------------------------------------------------------

class TestOrderRequest:
    def test_client_order_id_auto_generated(self) -> None:
        req = OrderRequest(symbol="SPY", side=OrderSide.BUY, type=OrderType.MARKET, qty=Decimal("10"))
        assert req.client_order_id  # non-empty UUID

    def test_client_order_ids_are_unique(self) -> None:
        r1 = OrderRequest(symbol="SPY", side=OrderSide.BUY, type=OrderType.MARKET, qty=Decimal("1"))
        r2 = OrderRequest(symbol="SPY", side=OrderSide.BUY, type=OrderType.MARKET, qty=Decimal("1"))
        assert r1.client_order_id != r2.client_order_id

    def test_qty_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            OrderRequest(symbol="SPY", side=OrderSide.BUY, type=OrderType.MARKET, qty=Decimal("0"))

    def test_negative_qty_raises(self) -> None:
        with pytest.raises(ValueError):
            OrderRequest(symbol="SPY", side=OrderSide.BUY, type=OrderType.MARKET, qty=Decimal("-5"))


# ---------------------------------------------------------------------------
# Position
# ---------------------------------------------------------------------------

class TestPosition:
    def test_market_value_computed(self) -> None:
        pos = Position(
            symbol="SPY",
            qty=Decimal("10"),
            avg_entry_price=Decimal("400"),
            current_price=Decimal("410"),
        )
        assert pos.market_value == Decimal("4100")

    def test_market_value_none_without_price(self) -> None:
        pos = Position(symbol="SPY", qty=Decimal("10"), avg_entry_price=Decimal("400"))
        assert pos.market_value is None

    def test_is_flat(self) -> None:
        pos = Position(symbol="SPY", qty=Decimal("0"), avg_entry_price=Decimal("400"))
        assert pos.is_flat


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------

class TestAccount:
    def test_all_monetary_fields_are_decimal(self) -> None:
        acc = Account(
            equity=Decimal("10000"),
            cash=Decimal("5000"),
            buying_power=Decimal("20000"),
        )
        assert isinstance(acc.equity, Decimal)
        assert isinstance(acc.cash, Decimal)
        assert isinstance(acc.buying_power, Decimal)


# ---------------------------------------------------------------------------
# Trade
# ---------------------------------------------------------------------------

class TestTrade:
    def _trade(self, pnl_net: Decimal) -> Trade:
        inst = _instrument()
        return Trade(
            strategy_name="rsi",
            instrument=inst,
            entry_time=_ts(10),
            exit_time=_ts(14),
            entry_price=Decimal("400"),
            exit_price=Decimal("410"),
            qty=Decimal("10"),
            side=OrderSide.BUY,
            pnl_gross=pnl_net + Decimal("1"),
            pnl_net=pnl_net,
            fees=Decimal("1"),
            duration_seconds=14400,
            mode="paper",
        )

    def test_winner(self) -> None:
        assert self._trade(Decimal("50")).is_winner

    def test_loser(self) -> None:
        assert not self._trade(Decimal("-20")).is_winner
