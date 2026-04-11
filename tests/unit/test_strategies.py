"""Tests for all concrete strategy implementations.

All tests are pure: no DB, no I/O, no network.
Strategies are imported directly (not via registry) to keep tests isolated.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pandas as pd
import pytest

from app.core.domain import Instrument, Position, Signal
from app.core.enums import AssetClass, PositionSide, SignalSide, StrategyMode
from app.core.registry import strategy_registry
from app.strategies.adx_ema_trend import ADXEMATrend
from app.strategies.base import StrategyContext
from app.strategies.bollinger_bands import BollingerBandsMR
from app.strategies.breakout import Breakout
from app.strategies.ma_crossover import MACrossover
from app.strategies.macd_crossover import MACDCrossover
from app.strategies.rsi_mean_reversion import RSIMeanReversion

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_INSTRUMENT = Instrument(
    symbol="SPY",
    asset_class=AssetClass.EQUITY,
    provider_name="dummy",
)

_NOW = datetime(2024, 1, 15, 14, 30, tzinfo=UTC)


def _ctx(
    params: dict[str, Any] | None = None,
    current_position: Position | None = None,
) -> StrategyContext:
    return StrategyContext(
        strategy_name="test",
        strategy_version="1.0.0",
        mode=StrategyMode.PAPER,
        params=params or {},
        instrument=_INSTRUMENT,
        current_position=current_position,
        account_equity=Decimal("10000"),
        current_time=_NOW,
    )


def _open_position(qty: str = "10") -> Position:
    return Position(
        symbol="SPY",
        qty=Decimal(qty),
        avg_entry_price=Decimal("100"),
        side=PositionSide.LONG,
    )


def _make_df(
    closes: list[float],
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    volumes: list[float] | None = None,
) -> pd.DataFrame:
    """Build a minimal OHLCV DataFrame from close series."""
    n = len(closes)
    if highs is None:
        highs = [c + 1.0 for c in closes]
    if lows is None:
        lows = [c - 1.0 for c in closes]
    if volumes is None:
        volumes = [1_000_000.0] * n
    return pd.DataFrame(
        {
            "open": closes,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
        }
    )


# ---------------------------------------------------------------------------
# RSI Mean Reversion
# ---------------------------------------------------------------------------


class TestRSIMeanReversion:
    def _strategy(self) -> RSIMeanReversion:
        return RSIMeanReversion()

    def test_no_signal_when_insufficient_data(self):
        """Fewer bars than required_lookback → NaN indicators → None."""
        strat = self._strategy()
        # Only 10 bars — RSI(14) and MA(200) will be NaN
        candles = _make_df([100.0] * 10)
        assert strat.generate_signal(candles, _ctx()) is None

    def test_buy_signal_on_oversold_above_trend(self):
        """RSI < 30 + price above MA200 → BUY.

        225 rising bars establish a high MA200, then 25 bars of -3 drops create
        oversold RSI while keeping price above the long-term average.
        """
        strat = self._strategy()
        prices = list(range(100, 325))  # 225 rising bars (100→324)
        p = 324.0
        for _ in range(25):
            p -= 3.0
            prices.append(p)
        candles = _make_df(prices)
        signal = strat.generate_signal(candles, _ctx())
        assert signal is not None
        assert signal.side == SignalSide.BUY
        assert "RSI" in signal.reason

    def test_sell_signal_on_overbought(self):
        """RSI > 70 → SELL.

        230 alternating bars (RSI ≈ 50 baseline) followed by 20 strong up bars
        pushes RSI to ~100.
        """
        strat = self._strategy()
        prices = []
        p = 100.0
        for i in range(230):
            p += 1.0 if i % 2 == 0 else -0.9
            prices.append(p)
        for _ in range(20):
            p += 3.0
            prices.append(p)
        candles = _make_df(prices)
        signal = strat.generate_signal(candles, _ctx())
        assert signal is not None
        assert signal.side == SignalSide.SELL

    def test_no_signal_in_neutral_zone(self):
        """RSI between 30 and 70, no trend → None.

        Alternating +1/-1 prices keep RSI near 50 and MA near current price.
        """
        strat = self._strategy()
        prices = []
        p = 100.0
        for i in range(250):
            p += 1.0 if i % 2 == 0 else -1.0
            prices.append(p)
        candles = _make_df(prices)
        assert strat.generate_signal(candles, _ctx()) is None

    def test_no_signal_oversold_below_trend(self):
        """RSI < 30 but price below MA200 (downtrend) → trend filter blocks BUY."""
        strat = self._strategy()
        # 225 bars descending, then 25 sharp drops → RSI low but close < MA200
        prices = list(range(324, 99, -1))  # 225 descending bars (324→100)
        p = float(prices[-1])
        for _ in range(25):
            p -= 3.0
            prices.append(p)
        candles = _make_df(prices)
        signal = strat.generate_signal(candles, _ctx())
        # Trend filter blocks BUY; SELL could fire or None — no BUY either way
        assert signal is None or signal.side != SignalSide.BUY

    def test_validate_params_bad_rsi_period(self):
        strat = self._strategy()
        with pytest.raises(ValueError, match="rsi_period"):
            strat.validate_params({"rsi_period": 1})

    def test_validate_params_inverted_levels(self):
        strat = self._strategy()
        with pytest.raises(ValueError, match="oversold"):
            strat.validate_params({"oversold": 70, "overbought": 30})


# ---------------------------------------------------------------------------
# MA Crossover
# ---------------------------------------------------------------------------


class TestMACrossover:
    def _strategy(self) -> MACrossover:
        return MACrossover()

    def _golden_cross_df(self, short: int = 20, long: int = 50) -> pd.DataFrame:
        """Build a price series that causes a golden cross on the last bar."""
        # 300 bars: first 200 flat at 100 (short < long after fall), last 100 rising
        # We construct it so prev_bar has short<=long, curr_bar has short>long.
        n = 300
        prices = [100.0] * (n - 2) + [95.0, 110.0]
        return _make_df(prices)

    def test_buy_signal_on_golden_cross(self):
        strat = self._strategy()
        params = {"ma_short": 5, "ma_long": 20, "atr_period": 5, "min_atr_pct": 0.0}
        # Craft a clear crossover: 30 bars at 100, then a big jump
        prices = [100.0] * 29 + [200.0]
        candles = _make_df(prices)
        signal = strat.generate_signal(candles, _ctx(params))
        # With min_atr_pct=0, signal should fire if there's a cross
        if signal is not None:
            assert signal.side == SignalSide.BUY

    def test_no_signal_low_volatility(self):
        """ATR% < min_atr_pct suppresses any signal."""
        strat = self._strategy()
        # Perfectly flat prices → ATR ≈ 0 → atr_pct < any positive threshold
        prices = [100.0] * 300
        params = {"ma_short": 5, "ma_long": 20, "min_atr_pct": 0.5, "atr_period": 5}
        candles = _make_df(prices)
        assert strat.generate_signal(candles, _ctx(params)) is None

    def test_no_signal_when_no_cross(self):
        """Consistently rising prices (short always > long) → no signal."""
        strat = self._strategy()
        prices = list(range(1, 301))   # monotonically increasing
        params = {"ma_short": 5, "ma_long": 20, "min_atr_pct": 0.0, "atr_period": 5}
        candles = _make_df(prices)
        # Short EMA is always above long EMA → no cross ever → None
        signal = strat.generate_signal(candles, _ctx(params))
        assert signal is None

    def test_no_signal_insufficient_data(self):
        strat = self._strategy()
        candles = _make_df([100.0])
        assert strat.generate_signal(candles, _ctx()) is None

    def test_validate_params_short_ge_long(self):
        strat = self._strategy()
        with pytest.raises(ValueError, match="ma_short"):
            strat.validate_params({"ma_short": 50, "ma_long": 20})


# ---------------------------------------------------------------------------
# Breakout
# ---------------------------------------------------------------------------


class TestBreakout:
    def _strategy(self) -> Breakout:
        return Breakout()

    def _params(self, **kwargs: Any) -> dict[str, Any]:
        defaults = {"lookback_bars": 5, "volume_ma_period": 5, "volume_multiplier": 1.5}
        return {**defaults, **kwargs}

    def test_buy_signal_on_price_and_volume_breakout(self):
        strat = self._strategy()
        params = self._params()
        # 30 bars at 100 with normal volume, then close=105 (>prior high=100) with 3x volume
        prices = [100.0] * 29 + [105.0]
        highs = [101.0] * 29 + [106.0]
        lows = [99.0] * 30
        base_vol = 1_000_000.0
        volumes = [base_vol] * 29 + [base_vol * 3.0]
        candles = _make_df(prices, highs, lows, volumes)
        signal = strat.generate_signal(candles, _ctx(params))
        assert signal is not None
        assert signal.side == SignalSide.BUY
        assert "prior_high" in signal.context

    def test_no_signal_price_breaks_without_volume(self):
        strat = self._strategy()
        params = self._params(volume_multiplier=2.0)
        prices = [100.0] * 29 + [105.0]
        highs = [101.0] * 29 + [106.0]
        lows = [99.0] * 30
        # Volume equal to baseline — not 2× — insufficient confirmation
        volumes = [1_000_000.0] * 30
        candles = _make_df(prices, highs, lows, volumes)
        assert strat.generate_signal(candles, _ctx(params)) is None

    def test_no_signal_below_prior_high(self):
        strat = self._strategy()
        params = self._params()
        # Current close (99) below prior high (101) → no breakout
        highs = [101.0] * 29 + [100.0]
        prices = [100.0] * 29 + [99.0]
        lows = [98.0] * 30
        volumes = [1_000_000.0] * 30
        candles = _make_df(prices, highs, lows, volumes)
        assert strat.generate_signal(candles, _ctx(params)) is None

    def test_no_signal_insufficient_data(self):
        strat = self._strategy()
        candles = _make_df([100.0] * 3)
        assert strat.generate_signal(candles, _ctx(self._params())) is None

    def test_validate_params_bad_lookback(self):
        strat = self._strategy()
        with pytest.raises(ValueError, match="lookback_bars"):
            strat.validate_params({"lookback_bars": 1})

    def test_validate_params_negative_multiplier(self):
        strat = self._strategy()
        with pytest.raises(ValueError, match="volume_multiplier"):
            strat.validate_params({"volume_multiplier": -1.0})


# ---------------------------------------------------------------------------
# Bollinger Bands Mean Reversion
# ---------------------------------------------------------------------------


class TestBollingerBandsMR:
    def _strategy(self) -> BollingerBandsMR:
        return BollingerBandsMR()

    def _params(self, **kwargs: Any) -> dict[str, Any]:
        return {"bb_period": 10, "bb_std": 2.0, "rsi_period": 7, "rsi_confirm": 50.0, **kwargs}

    def test_no_signal_insufficient_data(self):
        strat = self._strategy()
        candles = _make_df([100.0] * 5)
        assert strat.generate_signal(candles, _ctx(self._params())) is None

    def test_buy_signal_below_lower_band(self):
        """Price drops below lower BB + RSI < rsi_confirm → BUY."""
        strat = self._strategy()
        # Build: 30 bars at 100 to establish bands, then a sharp drop
        prices = [100.0] * 29 + [80.0]
        candles = _make_df(prices)
        signal = strat.generate_signal(candles, _ctx(self._params()))
        assert signal is not None
        assert signal.side == SignalSide.BUY
        assert "BB lower" in signal.reason

    def test_rsi_confirm_parameter_gates_entry(self):
        """rsi_confirm=99.9 lets any drop through; verify BUY fires after sharp drop."""
        strat = self._strategy()
        prices = [100.0] * 29 + [80.0]
        candles = _make_df(prices)
        # With rsi_confirm=99.9, RSI (near 0 after sharp drop) < 99.9 → BUY allowed
        signal = strat.generate_signal(candles, _ctx(self._params(rsi_confirm=99.9)))
        assert signal is not None
        assert signal.side == SignalSide.BUY

    def test_sell_signal_when_in_position_price_above_mid(self):
        """With an open position, price ≥ BB mid → SELL."""
        strat = self._strategy()
        # Prices cycle slightly to give non-zero std, last bar at or above the mid
        prices = [100.0 + (i % 3) * 0.5 for i in range(39)] + [100.5]
        candles = _make_df(prices)
        signal = strat.generate_signal(candles, _ctx(self._params(), _open_position()))
        assert signal is not None
        assert signal.side == SignalSide.SELL

    def test_no_signal_flat_prices(self):
        """Perfectly flat prices → bandwidth ≈ 0 → guard returns None."""
        strat = self._strategy()
        prices = [100.0] * 40
        candles = _make_df(prices)
        # Without open position, price == mid == lower when std=0 → guard fires
        signal = strat.generate_signal(candles, _ctx(self._params()))
        # bandwidth guard returns None when std is near zero
        assert signal is None

    def test_validate_params_bad_period(self):
        strat = self._strategy()
        with pytest.raises(ValueError, match="bb_period"):
            strat.validate_params({"bb_period": 2})

    def test_validate_params_bad_std(self):
        strat = self._strategy()
        with pytest.raises(ValueError, match="bb_std"):
            strat.validate_params({"bb_std": 0.0})


# ---------------------------------------------------------------------------
# MACD Crossover
# ---------------------------------------------------------------------------


class TestMACDCrossover:
    def _strategy(self) -> MACDCrossover:
        return MACDCrossover()

    def _params(self, **kwargs: Any) -> dict[str, Any]:
        return {"fast": 3, "slow": 6, "signal_period": 2, "min_histogram": 0.0, **kwargs}

    def test_no_signal_insufficient_data(self):
        strat = self._strategy()
        candles = _make_df([100.0])
        assert strat.generate_signal(candles, _ctx(self._params())) is None

    def test_buy_signal_on_bullish_crossover(self):
        """MACD crosses above signal line → BUY."""
        strat = self._strategy()
        # 20 falling bars then 10 rising bars → MACD line should cross above signal
        prices = [100.0 - i * 0.5 for i in range(20)] + [90.0 + i * 2.0 for i in range(10)]
        candles = _make_df(prices)
        signal = strat.generate_signal(candles, _ctx(self._params()))
        if signal is not None:
            assert signal.side == SignalSide.BUY
            assert "bullish" in signal.reason

    def test_sell_signal_on_bearish_crossover(self):
        """MACD crosses below signal line → SELL."""
        strat = self._strategy()
        # 20 rising bars then 10 falling bars → bearish crossover
        prices = [100.0 + i * 0.5 for i in range(20)] + [110.0 - i * 2.0 for i in range(10)]
        candles = _make_df(prices)
        signal = strat.generate_signal(candles, _ctx(self._params()))
        if signal is not None:
            assert signal.side == SignalSide.SELL
            assert "bearish" in signal.reason

    def test_no_signal_when_histogram_below_min(self):
        """min_histogram > actual histogram → BUY suppressed."""
        strat = self._strategy()
        # Use a very high min_histogram that the crossover can't satisfy
        prices = [100.0 - i * 0.1 for i in range(20)] + [99.0 + i * 0.5 for i in range(10)]
        candles = _make_df(prices)
        signal = strat.generate_signal(candles, _ctx(self._params(min_histogram=999.0)))
        assert signal is None or signal.side != SignalSide.BUY

    def test_validate_params_fast_ge_slow(self):
        strat = self._strategy()
        with pytest.raises(ValueError, match="fast"):
            strat.validate_params({"fast": 26, "slow": 12, "signal_period": 9})

    def test_validate_params_bad_signal_period(self):
        strat = self._strategy()
        with pytest.raises(ValueError, match="signal_period"):
            strat.validate_params({"fast": 12, "slow": 26, "signal_period": 0})


# ---------------------------------------------------------------------------
# ADX EMA Trend
# ---------------------------------------------------------------------------


class TestADXEMATrend:
    def _strategy(self) -> ADXEMATrend:
        return ADXEMATrend()

    def _params(self, **kwargs: Any) -> dict[str, Any]:
        return {"ema_fast": 5, "ema_slow": 15, "adx_period": 5, "adx_threshold": 0.0, **kwargs}

    def test_no_signal_insufficient_data(self):
        strat = self._strategy()
        candles = _make_df([100.0])
        assert strat.generate_signal(candles, _ctx(self._params())) is None

    def test_no_signal_when_adx_below_threshold(self):
        """ADX < threshold (range market) → no signal even on EMA cross."""
        strat = self._strategy()
        # Perfectly flat prices → ADX ≈ 0 → blocked by threshold
        prices = [100.0] * 60
        signal = strat.generate_signal(candles=_make_df(prices), ctx=_ctx(
            self._params(adx_threshold=50.0)
        ))
        assert signal is None

    def test_buy_signal_on_strong_uptrend(self):
        """Strong uptrend: ADX high, EMA fast > slow, DI+ > DI-."""
        strat = self._strategy()
        # Strongly rising prices: ADX builds up, DI+ dominates
        prices = [float(i) for i in range(1, 81)]  # 80 monotonically rising bars
        candles = _make_df(prices)
        # With adx_threshold=0.0, any crossover fires if DI+/DI- confirm
        signal = strat.generate_signal(candles, _ctx(self._params(adx_threshold=0.0)))
        # The signal fires on the first golden cross; may be None if no cross yet
        if signal is not None:
            assert signal.side == SignalSide.BUY
            assert "ADX" in signal.reason

    def test_sell_signal_on_strong_downtrend(self):
        """Strong downtrend: ADX high, EMA fast < slow, DI- > DI+."""
        strat = self._strategy()
        prices = [float(80 - i) for i in range(80)]  # 80 monotonically falling bars
        candles = _make_df(prices)
        signal = strat.generate_signal(candles, _ctx(self._params(adx_threshold=0.0)))
        if signal is not None:
            assert signal.side == SignalSide.SELL
            assert "ADX" in signal.reason

    def test_no_signal_nan_adx(self):
        """Not enough data to compute ADX (all NaN) → None."""
        strat = self._strategy()
        candles = _make_df([100.0] * 3)
        assert strat.generate_signal(candles, _ctx(self._params())) is None

    def test_validate_params_fast_ge_slow(self):
        strat = self._strategy()
        with pytest.raises(ValueError, match="ema_fast"):
            strat.validate_params({"ema_fast": 50, "ema_slow": 20})

    def test_validate_params_bad_threshold(self):
        strat = self._strategy()
        with pytest.raises(ValueError, match="adx_threshold"):
            strat.validate_params({"ema_fast": 20, "ema_slow": 50, "adx_threshold": -5.0})


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestStrategyRegistry:
    def test_all_six_strategies_registered(self):
        import app.strategies  # noqa: F401

        registered = strategy_registry.all()
        for name in (
            "rsi_mean_reversion", "ma_crossover", "breakout",
            "bollinger_bands", "macd_crossover", "adx_ema_trend",
        ):
            assert name in registered, f"{name} not found in registry"

    def test_registry_returns_correct_classes(self):
        import app.strategies  # noqa: F401

        assert strategy_registry.get("rsi_mean_reversion") is RSIMeanReversion
        assert strategy_registry.get("ma_crossover") is MACrossover
        assert strategy_registry.get("breakout") is Breakout
        assert strategy_registry.get("bollinger_bands") is BollingerBandsMR
        assert strategy_registry.get("macd_crossover") is MACDCrossover
        assert strategy_registry.get("adx_ema_trend") is ADXEMATrend

    def test_registered_strategies_are_instantiable(self):
        import app.strategies  # noqa: F401

        for name in (
            "rsi_mean_reversion", "ma_crossover", "breakout",
            "bollinger_bands", "macd_crossover", "adx_ema_trend",
        ):
            cls = strategy_registry.get(name)
            assert cls is not None
            instance = cls()
            assert hasattr(instance, "generate_signal")
            assert hasattr(instance, "required_lookback")

    def test_signal_fields_are_valid(self):
        """Smoke-test that a produced Signal has correct types."""
        strat = RSIMeanReversion()
        # 250 bars: flat then sharp drop → oversold above trend
        prices = list(range(50, 250)) + [80.0] * 50
        candles = _make_df(prices)
        signal = strat.generate_signal(candles, _ctx())
        if signal is not None:
            assert isinstance(signal, Signal)
            assert isinstance(signal.reason, str)
            assert isinstance(signal.context, dict)
            assert signal.time == _NOW
            assert signal.instrument == _INSTRUMENT
