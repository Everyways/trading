"""Breakout strategy.

Generates a BUY signal when price closes above the N-bar high AND volume
exceeds the rolling volume MA by a configurable multiplier. No SELL signal
is generated here — exit is handled by the runner via stop-loss or take-profit.

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


@strategy_registry.register("breakout")
class Breakout(Strategy):
    """Price/volume breakout above the N-bar high."""

    name = "breakout"
    version = "1.0.0"
    description = "Buy breakout above N-bar high with volume confirmation"
    required_timeframe = "15m"
    required_lookback = 50

    def generate_signal(
        self,
        candles: pd.DataFrame,
        ctx: StrategyContext,
    ) -> Signal | None:
        p: dict[str, Any] = ctx.params
        lookback: int = p.get("lookback_bars", 20)
        vol_period: int = p.get("volume_ma_period", 20)
        vol_mult: float = p.get("volume_multiplier", 1.5)

        close = candles["close"]
        high = candles["high"]
        volume = candles["volume"]

        # Prior window excludes the current (last) bar to avoid look-ahead
        prior_high = high.iloc[-(lookback + 1):-1].max()
        vol_ma = volume.rolling(vol_period).mean()

        curr_close = float(close.iloc[-1])
        curr_vol = float(volume.iloc[-1])
        curr_vol_ma = float(vol_ma.iloc[-1])

        if pd.isna(prior_high) or pd.isna(curr_vol_ma) or curr_vol_ma == 0:
            return None

        prior_high_f = float(prior_high)
        vol_ratio = curr_vol / curr_vol_ma

        if curr_close > prior_high_f and vol_ratio >= vol_mult:
            return Signal(
                strategy_name=self.name,
                instrument=ctx.instrument,
                side=SignalSide.BUY,
                reason=(
                    f"Close {curr_close:.2f} broke {lookback}-bar high "
                    f"{prior_high_f:.2f} with vol ratio {vol_ratio:.2f}x"
                ),
                context={
                    "prior_high": prior_high_f,
                    "vol_ratio": round(vol_ratio, 4),
                    "vol_ma": round(curr_vol_ma, 2),
                },
                time=ctx.current_time,
            )

        return None

    def validate_params(self, params: dict[str, Any]) -> None:
        lookback = params.get("lookback_bars", 20)
        vol_mult = params.get("volume_multiplier", 1.5)
        if not isinstance(lookback, int) or lookback < 2:
            raise ValueError(f"lookback_bars must be an int >= 2, got {lookback!r}")
        if vol_mult <= 0:
            raise ValueError(f"volume_multiplier must be > 0, got {vol_mult!r}")
