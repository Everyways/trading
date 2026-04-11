"""Bollinger Bands Mean Reversion strategy.

Buy when price closes below the lower Bollinger Band with RSI confirmation
(momentum is weak but not necessarily at extreme oversold levels).
Sell when price closes back above the middle band (MA), or reaches the upper
band on an extended move.

An ATR-based stop-loss is used via the standard stop_loss_pct param.
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
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss
    return 100 - 100 / (1 + rs)


def _bollinger(
    close: pd.Series, period: int, std_mult: float
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Return (upper, middle, lower) Bollinger Bands."""
    mid = close.rolling(period).mean()
    std = close.rolling(period).std(ddof=0)
    return mid + std_mult * std, mid, mid - std_mult * std


@strategy_registry.register("bollinger_bands")
class BollingerBandsMR(Strategy):
    """Mean reversion on Bollinger Band extremes with RSI confirmation."""

    name = "bollinger_bands"
    version = "1.0.0"
    description = "Buy below lower BB (RSI confirm); sell above middle or upper BB"
    required_timeframe = "15m"
    required_lookback = 60

    def generate_signal(
        self,
        candles: pd.DataFrame,
        ctx: StrategyContext,
    ) -> Signal | None:
        p: dict[str, Any] = ctx.params
        close = candles["close"]

        bb_period: int = p.get("bb_period", 20)
        bb_std: float = p.get("bb_std", 2.0)
        rsi_period: int = p.get("rsi_period", 14)
        rsi_confirm: float = p.get("rsi_confirm", 45.0)

        upper, mid, lower = _bollinger(close, bb_period, float(bb_std))
        rsi = _rsi(close, rsi_period)

        last_close = float(close.iloc[-1])
        last_upper = upper.iloc[-1]
        last_mid = mid.iloc[-1]
        last_lower = lower.iloc[-1]
        last_rsi = rsi.iloc[-1]

        if pd.isna(last_lower) or pd.isna(last_rsi):
            return None

        # Bandwidth guard: skip when bands are degenerate (flat price series)
        bandwidth = float(last_upper - last_lower)
        if bandwidth < 1e-9:
            return None

        context = {
            "bb_upper": round(float(last_upper), 4),
            "bb_mid": round(float(last_mid), 4),
            "bb_lower": round(float(last_lower), 4),
            "rsi": round(float(last_rsi), 2),
            "close": last_close,
        }

        in_position = (
            ctx.current_position is not None and not ctx.current_position.is_flat
        )

        if in_position:
            # Exit 1: price recovered to the middle band
            if last_close >= float(last_mid):
                return Signal(
                    strategy_name=self.name,
                    instrument=ctx.instrument,
                    side=SignalSide.SELL,
                    reason=(
                        f"price {last_close:.2f} ≥ BB mid {last_mid:.2f} — mean reversion complete"
                    ),
                    context=context,
                    time=ctx.current_time,
                )
            # Exit 2: upper band touched (momentum continuation — cut)
            if last_close >= float(last_upper):
                return Signal(
                    strategy_name=self.name,
                    instrument=ctx.instrument,
                    side=SignalSide.SELL,
                    reason=f"price {last_close:.2f} reached BB upper {last_upper:.2f}",
                    context=context,
                    time=ctx.current_time,
                )
            return None

        # Entry: price below lower band AND RSI confirms weakness (not extreme yet)
        if last_close < float(last_lower) and float(last_rsi) < rsi_confirm:
            return Signal(
                strategy_name=self.name,
                instrument=ctx.instrument,
                side=SignalSide.BUY,
                reason=(
                    f"price {last_close:.2f} < BB lower {last_lower:.2f}, "
                    f"RSI={last_rsi:.1f} < {rsi_confirm}"
                ),
                context=context,
                time=ctx.current_time,
            )

        return None

    def validate_params(self, params: dict[str, Any]) -> None:
        bb_period = params.get("bb_period", 20)
        bb_std = params.get("bb_std", 2.0)
        rsi_confirm = params.get("rsi_confirm", 45.0)
        if not isinstance(bb_period, int) or bb_period < 5:
            raise ValueError(f"bb_period must be an int ≥ 5, got {bb_period!r}")
        if float(bb_std) <= 0:
            raise ValueError(f"bb_std must be > 0, got {bb_std!r}")
        if not (0 < float(rsi_confirm) < 100):
            raise ValueError(
                f"rsi_confirm must be between 0 and 100, got {rsi_confirm!r}"
            )
