"""Tests for the backtest engine and metrics computation.

All tests are pure (no DB, no network). Synthetic DataFrames are built from
plain price lists using the _build_df helper.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pandas as pd
import pytest

from app.backtest.engine import BacktestEngine, BacktestResult
from app.backtest.metrics import BacktestMetrics, compute_metrics
from app.core.domain import Instrument, Signal
from app.core.enums import AssetClass, SignalSide
from app.strategies.base import Strategy, StrategyContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_TIME = datetime(2024, 1, 1, tzinfo=UTC)


def _build_df(prices: list[float]) -> pd.DataFrame:
    """Build a minimal OHLCV DataFrame from a list of close prices.

    open = previous close (simulates realistic gap-free bars).
    high = max(open, close) + 0.5
    low  = min(open, close) - 0.5
    """
    rows = []
    for i, price in enumerate(prices):
        prev = prices[i - 1] if i > 0 else price
        rows.append(
            {
                "time": _BASE_TIME + timedelta(minutes=15 * i),
                "open": prev,
                "high": max(prev, price) + 0.5,
                "low": min(prev, price) - 0.5,
                "close": price,
                "volume": 1000.0,
            }
        )
    return pd.DataFrame(rows)


def _instrument(symbol: str = "TEST") -> Instrument:
    return Instrument(symbol=symbol, asset_class=AssetClass.EQUITY, provider_name="test")


class _MockStrategy(Strategy):
    """Strategy that emits BUY/SELL at specific bar indices.

    bar_num = len(candles) - 1  (0-indexed position in the full DataFrame).
    """

    name = "mock"
    version = "1.0"
    description = "Mock strategy for backtest unit tests"
    required_timeframe = "15m"
    required_lookback = 1

    def __init__(
        self,
        buy_on: set[int] | None = None,
        sell_on: set[int] | None = None,
    ) -> None:
        self.buy_on: set[int] = buy_on or set()
        self.sell_on: set[int] = sell_on or set()

    def generate_signal(
        self, candles: pd.DataFrame, ctx: StrategyContext
    ) -> Signal | None:
        bar_num = len(candles) - 1
        if bar_num in self.buy_on:
            return Signal(
                strategy_name=self.name,
                instrument=ctx.instrument,
                side=SignalSide.BUY,
                reason="mock buy",
                time=ctx.current_time,
            )
        if bar_num in self.sell_on:
            return Signal(
                strategy_name=self.name,
                instrument=ctx.instrument,
                side=SignalSide.SELL,
                reason="mock sell",
                time=ctx.current_time,
            )
        return None


_DEFAULT_PARAMS: dict[str, Any] = {
    "lookback": 5,
    "stop_loss_pct": 2.0,
    "risk_pct": 1.0,
}


# ---------------------------------------------------------------------------
# TestComputeMetrics
# ---------------------------------------------------------------------------


class TestComputeMetrics:
    def test_empty_pnls_returns_zeros(self) -> None:
        m = compute_metrics([], pd.Series(dtype=float), Decimal("10000"))
        assert m.total_trades == 0
        assert m.net_pnl == Decimal("0")
        assert m.sharpe_ratio == 0.0
        assert m.max_drawdown_pct == 0.0
        assert m.profit_factor == 0.0

    def test_all_wins_profit_factor_inf(self) -> None:
        equity = pd.Series([10_000.0, 10_100.0, 10_200.0])
        pnls = [Decimal("100"), Decimal("100")]
        m = compute_metrics(pnls, equity, Decimal("10000"))
        assert m.winning_trades == 2
        assert m.losing_trades == 0
        assert m.profit_factor == float("inf")
        assert m.win_rate_pct == 100.0

    def test_all_losses(self) -> None:
        equity = pd.Series([10_000.0, 9_900.0, 9_800.0])
        pnls = [Decimal("-100"), Decimal("-100")]
        m = compute_metrics(pnls, equity, Decimal("10000"))
        assert m.winning_trades == 0
        assert m.losing_trades == 2
        assert m.profit_factor == 0.0
        assert m.win_rate_pct == 0.0
        assert m.net_pnl == Decimal("-200")

    def test_mixed_pnl_computes_sharpe_and_drawdown(self) -> None:
        # 200 win, 100 loss → net = 100, profit_factor = 2.0
        pnls = [Decimal("200"), Decimal("-100")]
        equity = pd.Series([10_000.0, 10_200.0, 10_100.0])
        m = compute_metrics(pnls, equity, Decimal("10000"))
        assert m.net_pnl == Decimal("100")
        assert abs(m.profit_factor - 2.0) < 1e-9
        assert m.max_drawdown_pct < 0  # drawdown is negative (peak-to-trough)
        assert m.total_return_pct == pytest.approx(1.0)  # 100/10000 * 100

    def test_sharpe_zero_variance_returns_zero(self) -> None:
        equity = pd.Series([10_000.0, 10_000.0, 10_000.0, 10_000.0])
        m = compute_metrics([Decimal("0")], equity, Decimal("10000"))
        assert m.sharpe_ratio == 0.0

    def test_holding_bars_updates_avg(self) -> None:
        equity = pd.Series([10_000.0, 10_100.0])
        pnls = [Decimal("100")]
        m = compute_metrics(pnls, equity, Decimal("10000"), holding_bars=[10])
        assert m.avg_holding_bars == 10.0


# ---------------------------------------------------------------------------
# TestBacktestEngine
# ---------------------------------------------------------------------------


class TestBacktestEngine:
    def _engine(self, commission: float = 0.001) -> BacktestEngine:
        return BacktestEngine(
            strategy=_MockStrategy(),
            initial_equity=Decimal("10000"),
            commission_pct=commission,
        )

    # ---- basic cases -------------------------------------------------------

    def test_no_signals_flat_equity(self) -> None:
        """A strategy that emits no signals should leave equity unchanged."""
        prices = [100.0] * 50
        df = _build_df(prices)
        engine = BacktestEngine(
            strategy=_MockStrategy(),
            initial_equity=Decimal("10000"),
            commission_pct=0.0,
        )
        result = engine.run(df, _DEFAULT_PARAMS, _instrument())

        assert isinstance(result, BacktestResult)
        assert result.trades == []
        assert result.metrics.total_trades == 0
        # All equity values should equal initial equity
        assert (result.equity_curve == 10_000.0).all()

    def test_insufficient_bars_no_trades(self) -> None:
        """DataFrames shorter than lookback+2 return empty metrics."""
        prices = [100.0] * 5  # lookback=20 → 5 < 22
        df = _build_df(prices)
        engine = self._engine()
        result = engine.run(df, {"lookback": 20}, _instrument())

        assert result.metrics.total_trades == 0
        assert result.equity_curve.empty

    def test_result_str_contains_strategy_name(self) -> None:
        prices = [100.0] * 50
        df = _build_df(prices)
        engine = BacktestEngine(
            strategy=_MockStrategy(),
            initial_equity=Decimal("10000"),
        )
        result = engine.run(df, _DEFAULT_PARAMS, _instrument("SPY"))
        assert "mock" in str(result)
        assert "SPY" in str(result)

    # ---- buy → sell profit -------------------------------------------------

    def test_buy_sell_earns_profit(self) -> None:
        """BUY at bar 24, SELL at bar 34 — price jump from 100 → 110 yields a gain."""
        # Bars 0..24 at 100, bars 25..49 at 110
        prices = [100.0] * 25 + [110.0] * 25
        df = _build_df(prices)
        strategy = _MockStrategy(buy_on={24}, sell_on={34})
        engine = BacktestEngine(
            strategy=strategy,
            initial_equity=Decimal("10000"),
            commission_pct=0.001,
        )
        result = engine.run(df, _DEFAULT_PARAMS, _instrument())

        assert result.metrics.total_trades == 1
        assert result.metrics.winning_trades == 1
        assert result.metrics.net_pnl > 0
        assert result.trades[0]["exit_reason"] == "signal"
        # Entry at bar 25 open = prices[24] = 100.0
        assert result.trades[0]["entry_price"] == pytest.approx(100.0)
        # Exit at bar 35 open = prices[34] = 110.0
        assert result.trades[0]["exit_price"] == pytest.approx(110.0)

    # ---- forced exits ------------------------------------------------------

    def test_stop_loss_triggered(self) -> None:
        """Price drops past stop-loss on the fill bar → closed at stop price."""
        # Bar 25 open = prices[24] = 100.0 (entry).
        # Bar 25 low = min(100, 90) - 0.5 = 89.5 ≤ stop = 100 * 0.95 = 95.0
        prices = [100.0] * 25 + [90.0] * 25
        df = _build_df(prices)
        params = {**_DEFAULT_PARAMS, "stop_loss_pct": 5.0}
        strategy = _MockStrategy(buy_on={24})
        engine = BacktestEngine(
            strategy=strategy, initial_equity=Decimal("10000"), commission_pct=0.0
        )
        result = engine.run(df, params, _instrument())

        assert result.metrics.total_trades == 1
        assert result.metrics.net_pnl < 0
        assert result.trades[0]["exit_reason"] == "stop_loss"
        assert result.trades[0]["exit_price"] == pytest.approx(95.0)

    def test_take_profit_triggered(self) -> None:
        """Price jumps above take-profit on the fill bar → closed at TP price."""
        # Bar 25 open = 100.0, TP = 115.0, bar 25 high = max(100,120)+0.5 = 120.5 ≥ 115.0
        prices = [100.0] * 25 + [120.0] * 25
        df = _build_df(prices)
        params = {**_DEFAULT_PARAMS, "take_profit_pct": 15.0}
        strategy = _MockStrategy(buy_on={24})
        engine = BacktestEngine(
            strategy=strategy, initial_equity=Decimal("10000"), commission_pct=0.0
        )
        result = engine.run(df, params, _instrument())

        assert result.metrics.total_trades == 1
        assert result.metrics.net_pnl > 0
        assert result.trades[0]["exit_reason"] == "take_profit"
        assert result.trades[0]["exit_price"] == pytest.approx(115.0)

    def test_max_holding_bars_exit(self) -> None:
        """Position closed after max_holding_bars bars even without a signal."""
        # Flat prices — no SL, no TP, no SELL signal. Only max_bars closes it.
        prices = [100.0] * 50
        df = _build_df(prices)
        # BUY at bar 24 → fills at bar 25 (entry_bar=25). Max=5 → exit at bar 30.
        params = {**_DEFAULT_PARAMS, "max_holding_bars": 5, "stop_loss_pct": 90.0}
        strategy = _MockStrategy(buy_on={24})
        engine = BacktestEngine(
            strategy=strategy, initial_equity=Decimal("10000"), commission_pct=0.0
        )
        result = engine.run(df, params, _instrument())

        assert result.metrics.total_trades == 1
        assert result.trades[0]["exit_reason"] == "max_bars"
        # entry_bar=25, exit_bar should be 30 (25 + 5)
        assert result.trades[0]["exit_bar"] - result.trades[0]["entry_bar"] == 5

    # ---- commission --------------------------------------------------------

    def test_commission_reduces_pnl(self) -> None:
        """Net PnL with commission < net PnL without commission."""
        prices = [100.0] * 25 + [110.0] * 25
        df = _build_df(prices)
        strategy = _MockStrategy(buy_on={24}, sell_on={34})
        params = _DEFAULT_PARAMS

        result_zero = BacktestEngine(
            strategy=strategy, initial_equity=Decimal("10000"), commission_pct=0.0
        ).run(df, params, _instrument())

        strategy2 = _MockStrategy(buy_on={24}, sell_on={34})
        result_fee = BacktestEngine(
            strategy=strategy2, initial_equity=Decimal("10000"), commission_pct=0.001
        ).run(df, params, _instrument())

        assert result_fee.metrics.net_pnl < result_zero.metrics.net_pnl
        # Both should still be profitable
        assert result_zero.metrics.net_pnl > 0
        assert result_fee.metrics.net_pnl > 0

    # ---- end-of-data close -------------------------------------------------

    def test_open_position_closed_at_end_of_data(self) -> None:
        """If position is still open after the last bar, it's closed at last close."""
        prices = [100.0] * 25 + [110.0] * 25
        df = _build_df(prices)
        # BUY at bar 24 but never SELL — closes at end of data
        strategy = _MockStrategy(buy_on={24})
        engine = BacktestEngine(
            strategy=strategy, initial_equity=Decimal("10000"), commission_pct=0.0
        )
        result = engine.run(df, _DEFAULT_PARAMS, _instrument())

        assert result.metrics.total_trades == 1
        assert result.trades[0]["exit_reason"] == "end_of_data"

    # ---- equity curve -------------------------------------------------------

    def test_equity_curve_length_matches_evaluated_bars(self) -> None:
        """equity_curve has one entry per bar from lookback to end."""
        n = 50
        lookback = 5
        prices = [100.0] * n
        df = _build_df(prices)
        engine = self._engine()
        result = engine.run(df, {"lookback": lookback, "stop_loss_pct": 2.0}, _instrument())

        # Bars evaluated: from index lookback to n-1 inclusive = n - lookback bars
        assert len(result.equity_curve) == n - lookback
