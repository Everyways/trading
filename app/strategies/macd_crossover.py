"""MACD Crossover strategy.

Generates BUY signals when the MACD line crosses above the signal line
(bullish crossover) and SELL signals on the reverse (bearish crossover).

More nuanced than a raw EMA crossover: the MACD line is itself a difference
of two EMAs, so crossovers of the signal line reflect momentum shifts rather
than pure price level comparisons. This reduces lag while filtering minor
oscillations.

An optional ``min_histogram`` threshold discards low-conviction crossovers
(histogram too close to zero = weak momentum).

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


def _macd(
    close: pd.Series, fast: int, slow: int, signal_period: int
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Return (macd_line, signal_line, histogram)."""
    macd_line = _ema(close, fast) - _ema(close, slow)
    signal_line = _ema(macd_line, signal_period)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


@strategy_registry.register("macd_crossover")
class MACDCrossover(Strategy):
    """MACD line / signal line bullish and bearish crossovers."""

    name = "macd_crossover"
    version = "1.0.0"
    description = "MACD(12,26,9) bullish/bearish crossover with histogram filter"
    required_timeframe = "15m"
    required_lookback = 150

    def generate_signal(
        self,
        candles: pd.DataFrame,
        ctx: StrategyContext,
    ) -> Signal | None:
        p: dict[str, Any] = ctx.params
        close = candles["close"]

        fast: int = p.get("fast", 12)
        slow: int = p.get("slow", 26)
        signal_period: int = p.get("signal_period", 9)
        min_histogram: float = float(p.get("min_histogram", 0.0))

        macd_line, signal_line, histogram = _macd(close, fast, slow, signal_period)

        if len(macd_line) < 2:
            return None

        prev_macd = macd_line.iloc[-2]
        curr_macd = macd_line.iloc[-1]
        prev_sig = signal_line.iloc[-2]
        curr_sig = signal_line.iloc[-1]
        curr_hist = float(histogram.iloc[-1])

        if pd.isna(curr_macd) or pd.isna(curr_sig) or pd.isna(prev_macd):
            return None

        context = {
            "macd": round(float(curr_macd), 6),
            "signal": round(float(curr_sig), 6),
            "histogram": round(curr_hist, 6),
        }

        # Bullish crossover: MACD crosses above signal line
        if (
            float(prev_macd) <= float(prev_sig)
            and float(curr_macd) > float(curr_sig)
            and curr_hist >= min_histogram
        ):
            return Signal(
                strategy_name=self.name,
                instrument=ctx.instrument,
                side=SignalSide.BUY,
                reason=(
                    f"MACD({fast},{slow},{signal_period}) bullish crossover, "
                    f"hist={curr_hist:.6f}"
                ),
                context=context,
                time=ctx.current_time,
            )

        # Bearish crossover: MACD crosses below signal line
        if float(prev_macd) >= float(prev_sig) and float(curr_macd) < float(curr_sig):
            return Signal(
                strategy_name=self.name,
                instrument=ctx.instrument,
                side=SignalSide.SELL,
                reason=(
                    f"MACD({fast},{slow},{signal_period}) bearish crossover, "
                    f"hist={curr_hist:.6f}"
                ),
                context=context,
                time=ctx.current_time,
            )

        return None

    def validate_params(self, params: dict[str, Any]) -> None:
        fast = params.get("fast", 12)
        slow = params.get("slow", 26)
        signal_period = params.get("signal_period", 9)
        if not isinstance(fast, int) or not isinstance(slow, int):
            raise ValueError("fast and slow must be integers")
        if fast >= slow:
            raise ValueError(f"fast ({fast}) must be < slow ({slow})")
        if not isinstance(signal_period, int) or signal_period < 1:
            raise ValueError(
                f"signal_period must be a positive integer, got {signal_period!r}"
            )
