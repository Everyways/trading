"""MA Crossover strategy.

Generates BUY signals when a short EMA crosses above a long EMA, and SELL
signals on the reverse (death cross). An ATR volatility filter suppresses
signals when the market is too quiet.

All calculation is pure pandas — no I/O, deterministic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pandas as pd

from app.core.domain import Signal
from app.core.enums import SignalSide
from app.core.registry import strategy_registry
from app.strategies.base import Strategy

if TYPE_CHECKING:
    from app.strategies.base import StrategyContext


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _atr(df: pd.DataFrame, period: int) -> pd.Series:
    high = df["high"]
    low = df["low"]
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(period).mean()


@strategy_registry.register("ma_crossover")
class MACrossover(Strategy):
    """EMA crossover with ATR volatility filter."""

    name = "ma_crossover"
    version = "1.0.0"
    description = "EMA golden/death cross with ATR filter"
    required_timeframe = "15m"
    required_lookback = 300

    def generate_signal(
        self,
        candles: pd.DataFrame,
        ctx: StrategyContext,
    ) -> Signal | None:
        p: dict[str, Any] = ctx.params
        close = candles["close"]

        short_span: int = p.get("ma_short", 20)
        long_span: int = p.get("ma_long", 50)
        atr_period: int = p.get("atr_period", 14)
        min_atr_pct: float = p.get("min_atr_pct", 0.5)

        short_ma = _ema(close, short_span)
        long_ma = _ema(close, long_span)
        atr = _atr(candles, atr_period)

        if len(short_ma) < 2:
            return None

        prev_short = short_ma.iloc[-2]
        curr_short = short_ma.iloc[-1]
        prev_long = long_ma.iloc[-2]
        curr_long = long_ma.iloc[-1]
        curr_atr = atr.iloc[-1]
        curr_close = float(close.iloc[-1])

        if pd.isna(curr_atr) or pd.isna(prev_long) or curr_close == 0:
            return None

        atr_pct = float(curr_atr) / curr_close * 100
        if atr_pct < min_atr_pct:
            return None  # market too quiet — skip

        context = {
            f"ema{short_span}": float(curr_short),
            f"ema{long_span}": float(curr_long),
            "atr_pct": round(atr_pct, 4),
        }

        # Golden cross: short crosses above long
        if prev_short <= prev_long and curr_short > curr_long:
            return Signal(
                strategy_name=self.name,
                instrument=ctx.instrument,
                side=SignalSide.BUY,
                reason=f"EMA{short_span} crossed above EMA{long_span}",
                context=context,
                time=ctx.current_time,
            )

        # Death cross: short crosses below long
        if prev_short >= prev_long and curr_short < curr_long:
            return Signal(
                strategy_name=self.name,
                instrument=ctx.instrument,
                side=SignalSide.SELL,
                reason=f"EMA{short_span} crossed below EMA{long_span}",
                context=context,
                time=ctx.current_time,
            )

        return None

    def validate_params(self, params: dict[str, Any]) -> None:
        short = params.get("ma_short", 20)
        long = params.get("ma_long", 50)
        if short >= long:
            raise ValueError(
                f"ma_short ({short}) must be less than ma_long ({long})"
            )
