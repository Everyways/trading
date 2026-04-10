"""RSI Mean-Reversion strategy.

Buy when RSI is oversold (<30) and price is above its long-term moving average
(trend filter). Sell when RSI is overbought (>70).

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


def _rsi(close: pd.Series, period: int) -> pd.Series:
    """Wilder's RSI using simple rolling mean (equivalent for our lookback windows)."""
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss
    return 100 - 100 / (1 + rs)


@strategy_registry.register("rsi_mean_reversion")
class RSIMeanReversion(Strategy):
    """Buy oversold dips (RSI < 30) above MA200; exit overbought (RSI > 70)."""

    name = "rsi_mean_reversion"
    version = "1.0.0"
    description = "Buy oversold (RSI<30) above MA200; sell overbought (RSI>70)"
    required_timeframe = "15m"
    required_lookback = 250

    def generate_signal(
        self,
        candles: pd.DataFrame,
        ctx: StrategyContext,
    ) -> Signal | None:
        p: dict[str, Any] = ctx.params
        close = candles["close"]

        rsi_period: int = p.get("rsi_period", 14)
        trend_ma: int = p.get("trend_filter_ma", 200)
        oversold: float = p.get("oversold", 30)
        overbought: float = p.get("overbought", 70)

        rsi = _rsi(close, rsi_period)
        ma = close.rolling(trend_ma).mean()

        last_rsi = rsi.iloc[-1]
        last_close = close.iloc[-1]
        last_ma = ma.iloc[-1]

        if pd.isna(last_rsi) or pd.isna(last_ma):
            return None

        if last_rsi < oversold and last_close > last_ma:
            return Signal(
                strategy_name=self.name,
                instrument=ctx.instrument,
                side=SignalSide.BUY,
                reason=f"RSI={last_rsi:.1f} < {oversold} and price above MA{trend_ma}",
                context={"rsi": float(last_rsi), "ma": float(last_ma), "close": float(last_close)},
                time=ctx.current_time,
            )

        if last_rsi > overbought:
            return Signal(
                strategy_name=self.name,
                instrument=ctx.instrument,
                side=SignalSide.SELL,
                reason=f"RSI={last_rsi:.1f} > {overbought}",
                context={"rsi": float(last_rsi), "close": float(last_close)},
                time=ctx.current_time,
            )

        return None

    def validate_params(self, params: dict[str, Any]) -> None:
        rsi_period = params.get("rsi_period", 14)
        oversold = params.get("oversold", 30)
        overbought = params.get("overbought", 70)
        if not isinstance(rsi_period, int) or rsi_period < 2:
            raise ValueError(f"rsi_period must be an int >= 2, got {rsi_period!r}")
        if oversold >= overbought:
            raise ValueError(
                f"oversold ({oversold}) must be less than overbought ({overbought})"
            )
