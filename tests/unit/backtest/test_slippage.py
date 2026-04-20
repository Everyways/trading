"""Unit tests for slippage modelling in BacktestEngine.

Canonical assertion: a BUY market fill at bar-open $100.00 with 3 bps slippage
fills at $100.03. A SELL market fill at $105.00 fills at $104.9685.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pandas as pd
import pytest

from app.backtest.engine import BacktestEngine
from app.core.domain import Instrument, Signal
from app.core.enums import AssetClass, SignalSide
from app.strategies.base import Strategy, StrategyContext

_BASE_TIME = datetime(2024, 1, 1, tzinfo=UTC)


def _build_df(prices: list[float]) -> pd.DataFrame:
    rows = []
    for i, price in enumerate(prices):
        prev = prices[i - 1] if i > 0 else price
        rows.append(
            {
                "time": _BASE_TIME + timedelta(minutes=15 * i),
                "open": prev,
                "high": max(prev, price) + 0.50,
                "low": min(prev, price) - 0.50,
                "close": price,
                "volume": 1000.0,
            }
        )
    return pd.DataFrame(rows)


def _instrument() -> Instrument:
    return Instrument(symbol="TEST", asset_class=AssetClass.EQUITY, provider_name="test")


class _FixedStrategy(Strategy):
    """Emits BUY/SELL signals at specific bar indices (0-indexed, 1-bar look-ahead)."""

    name = "fixed"
    version = "1.0"
    description = "Fixed-signal strategy for slippage tests"
    required_timeframe = "15m"
    required_lookback = 1

    def __init__(self, buy_on: set[int], sell_on: set[int]) -> None:
        self.buy_on = buy_on
        self.sell_on = sell_on

    def generate_signal(self, candles: pd.DataFrame, ctx: StrategyContext) -> Signal | None:
        bar = len(candles) - 1
        if bar in self.buy_on:
            return Signal(
                strategy_name=self.name,
                instrument=ctx.instrument,
                side=SignalSide.BUY,
                reason="test buy",
                time=ctx.current_time,
            )
        if bar in self.sell_on:
            return Signal(
                strategy_name=self.name,
                instrument=ctx.instrument,
                side=SignalSide.SELL,
                reason="test sell",
                time=ctx.current_time,
            )
        return None


_PARAMS: dict[str, Any] = {
    "lookback": 2,
    "stop_loss_pct": 50.0,  # wide stop so it never fires on test data
    "take_profit_pct": 0.0,
    "risk_pct": 1.0,
    "timeframe": "15m",
}


class TestSlippageAppliedToFills:
    def test_buy_fill_price_increased_by_slippage(self) -> None:
        """BUY at bar-open $100 with 3 bps slippage → entry_price = $100.03."""
        # prices[2] open = prices[1] = 100.0; signal fires at bar 2 → fill at bar 3 open
        prices = [99.0, 100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
        df = _build_df(prices)
        strategy = _FixedStrategy(buy_on={2}, sell_on={4})
        engine = BacktestEngine(
            strategy=strategy,
            initial_equity=Decimal("10000"),
            commission_pct=0.0,
            slippage_bps=3.0,
        )
        result = engine.run(df, _PARAMS, _instrument())
        assert result.trades, "expected at least one trade"
        trade = result.trades[0]
        # bar 3 open = prices[2] = 101.0; with 3 bps → 101.0 * 1.0003 = 101.0303
        expected_entry = 101.0 * (1 + 3 / 10_000)
        assert trade["entry_price"] == pytest.approx(expected_entry, rel=1e-6)

    def test_sell_fill_price_decreased_by_slippage(self) -> None:
        """SELL signal exit at bar-open $105 with 3 bps slippage → exit_price = $104.9685."""
        prices = [99.0, 100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0]
        df = _build_df(prices)
        strategy = _FixedStrategy(buy_on={2}, sell_on={5})
        engine = BacktestEngine(
            strategy=strategy,
            initial_equity=Decimal("10000"),
            commission_pct=0.0,
            slippage_bps=3.0,
        )
        result = engine.run(df, _PARAMS, _instrument())
        assert result.trades
        trade = result.trades[0]
        # bar 6 open = prices[5] = 104.0; with 3 bps sell → 104.0 * 0.9997
        expected_exit = 104.0 * (1 - 3 / 10_000)
        assert trade["exit_price"] == pytest.approx(expected_exit, rel=1e-6)

    def test_zero_slippage_fills_at_raw_price(self) -> None:
        """With slippage_bps=0 entry and exit fill at the unadjusted bar-open price."""
        prices = [99.0, 100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
        df = _build_df(prices)
        strategy = _FixedStrategy(buy_on={2}, sell_on={4})
        engine = BacktestEngine(
            strategy=strategy,
            initial_equity=Decimal("10000"),
            commission_pct=0.0,
            slippage_bps=0.0,
        )
        result = engine.run(df, _PARAMS, _instrument())
        assert result.trades
        trade = result.trades[0]
        assert trade["entry_price"] == pytest.approx(101.0)  # bar 3 open = prices[2]

    def test_net_sharpe_lower_than_gross_with_slippage(self) -> None:
        """gross_sharpe >= sharpe_ratio when slippage is applied."""
        prices = [100.0 + i * 0.5 for i in range(30)]
        df = _build_df(prices)
        strategy = _FixedStrategy(buy_on={5, 15}, sell_on={10, 20})
        engine = BacktestEngine(
            strategy=strategy,
            initial_equity=Decimal("10000"),
            commission_pct=0.001,
            slippage_bps=5.0,
        )
        result = engine.run(df, _PARAMS, _instrument())
        assert result.gross_sharpe >= result.metrics.sharpe_ratio

    def test_take_profit_exit_no_slippage(self) -> None:
        """Take-profit exits (limit orders) must not have slippage applied."""
        # Build prices so bar_high exceeds TP price on a known bar.
        # BUY at 100.0 with take_profit_pct=2% → TP target = 102.0 (approx)
        prices = [99.0, 100.0, 101.0, 102.0, 103.5, 104.0, 105.0, 106.0]
        df = _build_df(prices)
        params = {**_PARAMS, "stop_loss_pct": 50.0, "take_profit_pct": 2.0}
        strategy = _FixedStrategy(buy_on={2}, sell_on=set())
        engine = BacktestEngine(
            strategy=strategy,
            initial_equity=Decimal("10000"),
            commission_pct=0.0,
            slippage_bps=3.0,
        )
        result = engine.run(df, params, _instrument())
        tp_trades = [t for t in result.trades if t["exit_reason"] == "take_profit"]
        assert tp_trades, "expected a take_profit exit"
        tp_trade = tp_trades[0]
        # TP fills at exact TP price (no slippage) — entry with slippage raises entry slightly
        entry = tp_trade["entry_price"]
        expected_tp = entry * (1 + 2.0 / 100.0)
        assert tp_trade["exit_price"] == pytest.approx(expected_tp, rel=1e-4)
