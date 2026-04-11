"""ADX Trend Filter + EMA Crossover strategy.

Only generates signals when the ADX confirms a strong directional trend
(ADX ≥ adx_threshold). In range-bound markets (ADX < threshold) the
strategy emits no signals, avoiding the whipsaw problem that plagues
plain EMA crossovers.

Signal logic:
  BUY  — fast EMA crosses above slow EMA AND DI+ > DI- AND ADX ≥ threshold
  SELL — fast EMA crosses below slow EMA AND DI- > DI+ AND ADX ≥ threshold

ADX and DI+/DI- are computed using Wilder's smoothing (EWM with
alpha = 1/period), consistent with the original J. Welles Wilder definition.

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


def _adx_di(
    df: pd.DataFrame, period: int
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Compute (ADX, DI+, DI-) using Wilder's smoothing.

    Returns three Series aligned to df's index.
    """
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    prev_high = high.shift(1)
    prev_low = low.shift(1)

    # True Range
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)

    # Raw directional movements
    dm_plus_raw = high - prev_high
    dm_minus_raw = prev_low - low
    dm_plus = dm_plus_raw.where(
        (dm_plus_raw > dm_minus_raw) & (dm_plus_raw > 0), 0.0
    )
    dm_minus = dm_minus_raw.where(
        (dm_minus_raw > dm_plus_raw) & (dm_minus_raw > 0), 0.0
    )

    # Wilder's smoothing: EWM with alpha = 1/period
    alpha = 1.0 / period
    atr_w = tr.ewm(alpha=alpha, adjust=False).mean()
    sdm_plus = dm_plus.ewm(alpha=alpha, adjust=False).mean()
    sdm_minus = dm_minus.ewm(alpha=alpha, adjust=False).mean()

    # Guard against zero ATR (flat price series)
    safe_atr = atr_w.replace(0.0, float("nan"))
    di_plus = 100.0 * sdm_plus / safe_atr
    di_minus = 100.0 * sdm_minus / safe_atr

    di_sum = (di_plus + di_minus).replace(0.0, float("nan"))
    dx = 100.0 * (di_plus - di_minus).abs() / di_sum
    adx = dx.ewm(alpha=alpha, adjust=False).mean()

    return adx, di_plus, di_minus


@strategy_registry.register("adx_ema_trend")
class ADXEMATrend(Strategy):
    """EMA crossover gated by ADX trend strength — no trades in range markets."""

    name = "adx_ema_trend"
    version = "1.0.0"
    description = "EMA cross + ADX≥threshold + DI confirmation — range filter"
    required_timeframe = "15m"
    required_lookback = 200

    def generate_signal(
        self,
        candles: pd.DataFrame,
        ctx: StrategyContext,
    ) -> Signal | None:
        p: dict[str, Any] = ctx.params
        close = candles["close"]

        ema_fast: int = p.get("ema_fast", 20)
        ema_slow: int = p.get("ema_slow", 50)
        adx_period: int = p.get("adx_period", 14)
        adx_threshold: float = float(p.get("adx_threshold", 25.0))

        fast_ema = _ema(close, ema_fast)
        slow_ema = _ema(close, ema_slow)
        adx, di_plus, di_minus = _adx_di(candles, adx_period)

        if len(fast_ema) < 2:
            return None

        curr_fast = float(fast_ema.iloc[-1])
        curr_slow = float(slow_ema.iloc[-1])
        prev_fast = float(fast_ema.iloc[-2])
        prev_slow = float(slow_ema.iloc[-2])
        curr_adx = adx.iloc[-1]
        curr_di_plus = float(di_plus.iloc[-1])
        curr_di_minus = float(di_minus.iloc[-1])

        if pd.isna(curr_adx) or pd.isna(curr_di_plus) or pd.isna(curr_di_minus):
            return None

        # Skip range-bound markets
        if float(curr_adx) < adx_threshold:
            return None

        context = {
            f"ema{ema_fast}": round(curr_fast, 4),
            f"ema{ema_slow}": round(curr_slow, 4),
            "adx": round(float(curr_adx), 2),
            "di_plus": round(curr_di_plus, 2),
            "di_minus": round(curr_di_minus, 2),
        }

        # Bullish crossover: fast EMA crosses above slow EMA, DI+ dominates
        if (
            prev_fast <= prev_slow
            and curr_fast > curr_slow
            and curr_di_plus > curr_di_minus
        ):
            return Signal(
                strategy_name=self.name,
                instrument=ctx.instrument,
                side=SignalSide.BUY,
                reason=(
                    f"EMA{ema_fast} crossed EMA{ema_slow}, "
                    f"ADX={curr_adx:.1f} ≥ {adx_threshold}, "
                    f"DI+={curr_di_plus:.1f} > DI-={curr_di_minus:.1f}"
                ),
                context=context,
                time=ctx.current_time,
            )

        # Bearish crossover: fast EMA crosses below slow EMA, DI- dominates
        if (
            prev_fast >= prev_slow
            and curr_fast < curr_slow
            and curr_di_minus > curr_di_plus
        ):
            return Signal(
                strategy_name=self.name,
                instrument=ctx.instrument,
                side=SignalSide.SELL,
                reason=(
                    f"EMA{ema_fast} crossed below EMA{ema_slow}, "
                    f"ADX={curr_adx:.1f} ≥ {adx_threshold}, "
                    f"DI-={curr_di_minus:.1f} > DI+={curr_di_plus:.1f}"
                ),
                context=context,
                time=ctx.current_time,
            )

        return None

    def validate_params(self, params: dict[str, Any]) -> None:
        ema_fast = params.get("ema_fast", 20)
        ema_slow = params.get("ema_slow", 50)
        adx_threshold = params.get("adx_threshold", 25.0)
        if not isinstance(ema_fast, int) or not isinstance(ema_slow, int):
            raise ValueError("ema_fast and ema_slow must be integers")
        if ema_fast >= ema_slow:
            raise ValueError(f"ema_fast ({ema_fast}) must be < ema_slow ({ema_slow})")
        if float(adx_threshold) <= 0:
            raise ValueError(f"adx_threshold must be > 0, got {adx_threshold!r}")
