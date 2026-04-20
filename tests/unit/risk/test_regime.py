"""Unit tests for the market regime detector.

Each test builds a synthetic daily OHLCV DataFrame with known properties and
asserts the classifier returns the expected MarketRegime.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.risk.regime import MarketRegime, RegimeDetector


def _make_df(
    closes: list[float],
    high_offset: float = 1.0,
    low_offset: float = 1.0,
) -> pd.DataFrame:
    """Build a minimal daily OHLCV DataFrame from a list of close prices."""
    n = len(closes)
    highs = [c + high_offset for c in closes]
    lows = [c - low_offset for c in closes]
    return pd.DataFrame(
        {
            "open": closes,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [1_000_000.0] * n,
        }
    )


class TestRegimeDetectorInsufficientData:
    def test_too_few_bars_returns_chop(self) -> None:
        detector = RegimeDetector(ema_period=200, atr_period=14)
        df = _make_df([100.0] * 10)  # only 10 bars, need 15
        assert detector.detect(df) == MarketRegime.CHOP


class TestHighVolRegime:
    def test_large_atr_pct_returns_high_vol(self) -> None:
        # Create a series of prices with extreme high-low ranges → high ATR%
        # Use 300 bars so the tercile window is well-populated
        rng = np.random.default_rng(42)
        # First 250 bars: small swings (baseline)
        quiet_closes = np.cumsum(rng.normal(0, 0.1, 250)) + 400.0
        quiet_highs = quiet_closes + 0.5
        quiet_lows = quiet_closes - 0.5

        # Last 50 bars: massive swings → push last ATR into top tercile
        volatile_closes = np.cumsum(rng.normal(0, 0.5, 50)) + float(quiet_closes[-1])
        volatile_highs = volatile_closes + 20.0  # very wide range
        volatile_lows = volatile_closes - 20.0

        closes = np.concatenate([quiet_closes, volatile_closes])
        highs = np.concatenate([quiet_highs, volatile_highs])
        lows = np.concatenate([quiet_lows, volatile_lows])

        df = pd.DataFrame(
            {
                "open": closes,
                "high": highs,
                "low": lows,
                "close": closes,
                "volume": 1e6,
            }
        )
        detector = RegimeDetector(ema_period=200, atr_period=14)
        assert detector.detect(df) == MarketRegime.HIGH_VOL


class TestChopRegime:
    def test_medium_atr_pct_returns_chop(self) -> None:
        """ATR% in the middle tercile → CHOP.

        Construction: 100 bars LOW (range=0.4) + 50 bars HIGH (range=20)
        + 102 bars MEDIUM (range=4), all at constant price 400.

        After EWM smoothing the final bar's ATR%≈1% sits strictly above
        q33≈0.1% (the low segment) and strictly below q67≈1.5-2% (dominated
        by the high-to-medium transition values), so the detector returns CHOP.
        """
        rows = (
            [{"open": 400, "high": 400.2, "low": 399.8, "close": 400, "volume": 1e6}] * 100
            + [{"open": 400, "high": 410.0, "low": 390.0, "close": 400, "volume": 1e6}] * 50
            + [{"open": 400, "high": 402.0, "low": 398.0, "close": 400, "volume": 1e6}] * 102
        )
        df = pd.DataFrame(rows)
        detector = RegimeDetector(ema_period=200, atr_period=14)
        assert detector.detect(df) == MarketRegime.CHOP


class TestTrendUpRegime:
    def test_steady_uptrend_returns_trend_up(self) -> None:
        # 260 bars: steadily rising prices, very small ATR → low-vol uptrend
        closes = [200.0 + i * 0.5 for i in range(260)]
        # Tiny intrabar range so ATR% stays low
        highs = [c + 0.1 for c in closes]
        lows = [c - 0.1 for c in closes]
        df = pd.DataFrame(
            {
                "open": closes,
                "high": highs,
                "low": lows,
                "close": closes,
                "volume": 1e6,
            }
        )
        detector = RegimeDetector(ema_period=200, atr_period=14)
        result = detector.detect(df)
        assert result == MarketRegime.TREND_UP


class TestTrendDownRegime:
    def test_steady_downtrend_returns_trend_down(self) -> None:
        """Two-phase series: HIGH ATR% history + LOW ATR% current → TREND_DOWN.

        Phase 1 (200 bars, flat at 400, large range=8): populates the 252-bar
        historical window with ATR%≈2%.  Phase 2 (60 bars, declining price,
        tiny range=0.2% of close): converges EWM ATR down to ~0.2%, well below
        q33≈q67≈2%.  The EMA(200) is still anchored near 400 while the close
        has fallen to ~334, so close < EMA → TREND_DOWN.
        """
        # Phase 1: 200 flat bars — large range → ATR% ≈ 2%
        p1_rows = [{"open": 400, "high": 404, "low": 396, "close": 400, "volume": 1e6}] * 200

        # Phase 2: 60 declining bars — small proportional range → ATR% ≈ 0.2%
        p2_rows = []
        price = 400.0
        for _ in range(60):
            price *= 0.997  # −0.3% per bar
            p2_rows.append(
                {
                    "open": price,
                    "high": price * 1.001,
                    "low": price * 0.999,
                    "close": price,
                    "volume": 1e6,
                }
            )

        df = pd.DataFrame(p1_rows + p2_rows)
        detector = RegimeDetector(ema_period=200, atr_period=14)
        result = detector.detect(df)
        assert result == MarketRegime.TREND_DOWN


class TestReturnType:
    def test_detect_always_returns_market_regime(self) -> None:
        detector = RegimeDetector()
        df = _make_df([100.0 + i for i in range(30)])
        result = detector.detect(df)
        assert isinstance(result, MarketRegime)

    @pytest.mark.parametrize("n_bars", [15, 50, 100, 260])
    def test_various_bar_counts_never_raise(self, n_bars: int) -> None:
        detector = RegimeDetector(ema_period=200, atr_period=14)
        df = _make_df([100.0 + i * 0.1 for i in range(n_bars)])
        result = detector.detect(df)
        assert result in list(MarketRegime)
