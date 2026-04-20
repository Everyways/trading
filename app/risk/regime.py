"""Market regime detector — classifies SPY daily bars into four regimes.

Used by TradingRunner to gate strategies that only have edge in specific
market conditions (e.g. mean-reversion strategies work best in CHOP,
trend strategies in TREND_UP / TREND_DOWN).

Features:
  - 200-EMA direction (close vs EMA) → trend side
  - 14-period ATR / close × 100 → volatility proxy (VIX substitute, free)

Classification:
  HIGH_VOL   — ATR% in top tercile of 252-bar history
  CHOP       — ATR% in middle tercile
  TREND_UP   — ATR% in lower tercile AND close > 200-EMA
  TREND_DOWN — ATR% in lower tercile AND close < 200-EMA
"""

from __future__ import annotations

import logging
from enum import StrEnum

import pandas as pd

log = logging.getLogger(__name__)

_EMA_PERIOD = 200
_ATR_PERIOD = 14
_ATR_HISTORY_BARS = 252  # one calendar year of daily bars


class MarketRegime(StrEnum):
    TREND_UP = "trend_up"
    TREND_DOWN = "trend_down"
    CHOP = "chop"
    HIGH_VOL = "high_vol"


class RegimeDetector:
    """Classify market regime from a daily OHLCV DataFrame (SPY recommended).

    Args:
        ema_period:   Long-term EMA period for trend direction (default 200).
        atr_period:   ATR smoothing period (default 14).
    """

    def __init__(
        self,
        ema_period: int = _EMA_PERIOD,
        atr_period: int = _ATR_PERIOD,
    ) -> None:
        self._ema_period = ema_period
        self._atr_period = atr_period

    def detect(self, df: pd.DataFrame) -> MarketRegime:
        """Classify the regime of the most recent bar in *df*.

        Args:
            df: Daily OHLCV DataFrame sorted ascending with columns
                open, high, low, close, volume. Needs at least
                ``atr_period + 1`` rows; ideally ≥ 252 for reliable terciles.

        Returns:
            MarketRegime enum value.
        """
        if len(df) < self._atr_period + 1:
            log.warning(
                "RegimeDetector: only %d bars (need %d) — defaulting to CHOP",
                len(df), self._atr_period + 1,
            )
            return MarketRegime.CHOP

        atr_pct = self._compute_atr_pct(df)
        history = atr_pct.dropna()
        if len(history) < 3:
            return MarketRegime.CHOP

        current = float(history.iloc[-1])
        window = history.iloc[-_ATR_HISTORY_BARS:]
        q33 = float(window.quantile(0.33))
        q67 = float(window.quantile(0.67))

        if current > q67:
            regime = MarketRegime.HIGH_VOL
        elif current > q33:
            regime = MarketRegime.CHOP
        elif len(df) >= self._ema_period:
            ema = df["close"].ewm(span=self._ema_period, adjust=False).mean()
            if float(df["close"].iloc[-1]) >= float(ema.iloc[-1]):
                regime = MarketRegime.TREND_UP
            else:
                regime = MarketRegime.TREND_DOWN
        else:
            # Not enough history for the long EMA — treat as indeterminate chop
            regime = MarketRegime.CHOP

        return regime

    def _compute_atr_pct(self, df: pd.DataFrame) -> pd.Series:
        high = df["high"]
        low = df["low"]
        prev_close = df["close"].shift(1)
        true_range = pd.concat(
            [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
            axis=1,
        ).max(axis=1)
        atr = true_range.ewm(span=self._atr_period, adjust=False).mean()
        return atr / df["close"] * 100
