"""Shared technical-indicator helpers used by strategies and risk components."""

from __future__ import annotations

import pandas as pd


def true_range(df: pd.DataFrame) -> pd.Series:
    """Per-bar true range: max(high-low, |high-prev_close|, |low-prev_close|)."""
    high = df["high"]
    low = df["low"]
    prev_close = df["close"].shift(1)
    return pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)


def ewm_atr(df: pd.DataFrame, period: int) -> pd.Series:
    """Exponentially-weighted ATR over *period* bars."""
    return true_range(df).ewm(span=period, adjust=False).mean()


def ema(series: pd.Series, span: int) -> pd.Series:
    """Exponential moving average."""
    return series.ewm(span=span, adjust=False).mean()
