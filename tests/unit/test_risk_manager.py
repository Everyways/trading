"""Tests for RiskManager — uses in-memory SQLite via db_session fixture."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.domain import Instrument, OrderRequest, Position
from app.core.enums import AssetClass, OrderSide, OrderType, PositionSide
from app.risk.manager import RiskManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_GLOBAL_CONFIG = {
    "global_limits": {
        "max_monthly_loss_eur": 50,
        "max_daily_loss_pct": 3.0,
        "max_orders_per_minute_global": 20,
    },
    "pdt_compliance": {
        "enabled": True,
        "max_day_trades_per_5d": 3,
    },
}

_STRATEGY_RISK = {
    "max_daily_loss_pct": 2.0,
    "max_concurrent_positions": 2,
    "max_orders_per_minute": 5,
}

_EQUITY = Decimal("10000")
_EUR_PER_USD = Decimal("1.0")   # 1:1 for easy math in tests


def _order() -> OrderRequest:
    return OrderRequest(
        symbol="SPY",
        side=OrderSide.BUY,
        type=OrderType.MARKET,
        qty=Decimal("1"),
        strategy_name="test_strategy",
    )


def _position(symbol: str = "SPY", qty: Decimal = Decimal("10")) -> Position:
    return Position(
        symbol=symbol,
        qty=qty,
        avg_entry_price=Decimal("400"),
        side=PositionSide.LONG,
    )


def _risk(db_session) -> RiskManager:
    return RiskManager(db_session, _GLOBAL_CONFIG, eur_per_usd=_EUR_PER_USD)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRiskManagerHappyPath:
    def test_allows_order_when_all_checks_pass(self, db_session):
        risk = _risk(db_session)
        allowed, reason = risk.check_order(
            _order(), _EQUITY, [], "test_strategy", _STRATEGY_RISK
        )
        assert allowed is True
        assert reason == ""


class TestKillSwitch:
    def test_global_kill_blocks_all_orders(self, db_session):
        risk = _risk(db_session)
        risk.engage_kill_switch("global", reason="test")
        allowed, reason = risk.check_order(
            _order(), _EQUITY, [], "test_strategy", _STRATEGY_RISK
        )
        assert allowed is False
        assert "global kill switch" in reason

    def test_strategy_kill_blocks_only_that_strategy(self, db_session):
        risk = _risk(db_session)
        risk.engage_kill_switch("strategy", strategy_name="test_strategy", reason="test")

        # test_strategy is blocked
        allowed, _ = risk.check_order(_order(), _EQUITY, [], "test_strategy", _STRATEGY_RISK)
        assert allowed is False

        # other_strategy is not blocked
        allowed2, _ = risk.check_order(_order(), _EQUITY, [], "other_strategy", _STRATEGY_RISK)
        assert allowed2 is True

    def test_global_kill_persisted_to_db(self, db_session):
        risk = _risk(db_session)
        risk.engage_kill_switch("global", reason="monthly stop")
        assert risk.global_kill_engaged is True

        # A new RiskManager loading from same DB should see the kill switch
        risk2 = _risk(db_session)
        assert risk2.global_kill_engaged is True


class TestMonthlyHardStop:
    def test_monthly_loss_at_limit_engages_global_kill(self, db_session):
        risk = _risk(db_session)
        # Inject monthly loss just at the threshold (50€ with 1:1 rate = $50)
        risk._monthly_loss_eur = Decimal("50")

        allowed, reason = risk.check_order(
            _order(), _EQUITY, [], "test_strategy", _STRATEGY_RISK
        )
        assert allowed is False
        assert "monthly hard stop" in reason
        assert risk.global_kill_engaged is True

    def test_monthly_loss_below_limit_allows_order(self, db_session):
        risk = _risk(db_session)
        risk._monthly_loss_eur = Decimal("49.99")

        allowed, _ = risk.check_order(_order(), _EQUITY, [], "test_strategy", _STRATEGY_RISK)
        assert allowed is True

    def test_record_fill_updates_monthly_loss(self, db_session):
        risk = _risk(db_session)
        assert risk.monthly_loss_eur == Decimal("0")

        risk.record_fill(Decimal("-100"), "test_strategy", is_closing=True)
        # With 1:1 EUR/USD rate, $100 loss → 100€
        assert risk.monthly_loss_eur == Decimal("100")

    def test_record_fill_opening_does_not_update_monthly(self, db_session):
        risk = _risk(db_session)
        risk.record_fill(Decimal("-100"), "test_strategy", is_closing=False)
        assert risk.monthly_loss_eur == Decimal("0")


class TestDailyLossLimits:
    def test_global_daily_loss_blocks_at_limit(self, db_session):
        risk = _risk(db_session)
        # Inject a daily loss of 3% of $10,000 = $300
        risk._daily_global_pnl = Decimal("-300")

        allowed, reason = risk.check_order(_order(), _EQUITY, [], "test_strategy", _STRATEGY_RISK)
        assert allowed is False
        assert "global daily loss" in reason

    def test_strategy_daily_loss_pauses_strategy(self, db_session):
        risk = _risk(db_session)
        # Strategy daily loss: 2% of $10,000 = $200
        risk._state("test_strategy").daily_pnl = Decimal("-200")

        allowed, reason = risk.check_order(_order(), _EQUITY, [], "test_strategy", _STRATEGY_RISK)
        assert allowed is False
        assert "daily loss" in reason
        assert risk.is_halted("test_strategy") is True

    def test_reset_daily_unpauses_strategy(self, db_session):
        risk = _risk(db_session)
        risk._state("test_strategy").daily_pnl = Decimal("-200")
        risk.check_order(_order(), _EQUITY, [], "test_strategy", _STRATEGY_RISK)
        assert risk.is_halted("test_strategy") is True

        risk.reset_daily_state()
        assert risk.is_halted("test_strategy") is False

    def test_reset_daily_clears_pnl_counters(self, db_session):
        risk = _risk(db_session)
        risk._daily_global_pnl = Decimal("-100")
        risk._state("test_strategy").daily_pnl = Decimal("-50")

        risk.reset_daily_state()

        assert risk._daily_global_pnl == Decimal("0")
        assert risk._state("test_strategy").daily_pnl == Decimal("0")


class TestMaxPositions:
    def test_at_max_positions_blocks_order(self, db_session):
        risk = _risk(db_session)
        positions = [_position("SPY"), _position("QQQ")]  # 2 = max

        allowed, reason = risk.check_order(
            _order(), _EQUITY, positions, "test_strategy", _STRATEGY_RISK
        )
        assert allowed is False
        assert "max positions" in reason

    def test_below_max_positions_allows_order(self, db_session):
        risk = _risk(db_session)
        positions = [_position("SPY")]  # 1 < max=2

        allowed, _ = risk.check_order(
            _order(), _EQUITY, positions, "test_strategy", _STRATEGY_RISK
        )
        assert allowed is True

    def test_flat_positions_not_counted(self, db_session):
        risk = _risk(db_session)
        # A flat position (qty=0) should not count toward the limit
        flat = Position(
            symbol="IWM",
            qty=Decimal("0"),
            avg_entry_price=Decimal("200"),
            side=PositionSide.LONG,
        )
        positions = [_position("SPY"), flat]  # only 1 live

        allowed, _ = risk.check_order(
            _order(), _EQUITY, positions, "test_strategy", _STRATEGY_RISK
        )
        assert allowed is True


class TestPDTCompliance:
    def test_three_day_trades_block_fourth(self, db_session):
        risk = _risk(db_session)
        for _ in range(3):
            risk.record_day_trade("test_strategy")

        allowed, reason = risk.check_order(
            _order(), _EQUITY, [], "test_strategy", _STRATEGY_RISK
        )
        assert allowed is False
        assert "PDT" in reason

    def test_two_day_trades_allow_third(self, db_session):
        risk = _risk(db_session)
        for _ in range(2):
            risk.record_day_trade("test_strategy")

        allowed, _ = risk.check_order(_order(), _EQUITY, [], "test_strategy", _STRATEGY_RISK)
        assert allowed is True

    def test_old_day_trades_expire_after_5_days(self, db_session):
        risk = _risk(db_session)
        # Inject 3 trades older than 5 days directly into the deque
        old = datetime.now(tz=UTC) - timedelta(days=6)
        state = risk._state("test_strategy")
        for _ in range(3):
            state.day_trade_times.append(old)

        # Should not count (expired) → order allowed
        allowed, _ = risk.check_order(_order(), _EQUITY, [], "test_strategy", _STRATEGY_RISK)
        assert allowed is True


class TestOrderRateLimit:
    def test_exceeding_rate_limit_blocks_order(self, db_session):
        risk = _risk(db_session)
        # Record 5 orders (the limit)
        for _ in range(5):
            risk.record_order_submitted("test_strategy")

        allowed, reason = risk.check_order(
            _order(), _EQUITY, [], "test_strategy", _STRATEGY_RISK
        )
        assert allowed is False
        assert "rate limit" in reason

    def test_old_orders_expire_from_rate_window(self, db_session):
        risk = _risk(db_session)
        # Inject 5 orders older than 60 seconds
        old = datetime.now(tz=UTC) - timedelta(seconds=61)
        state = risk._state("test_strategy")
        for _ in range(5):
            state.order_times.append(old)

        allowed, _ = risk.check_order(_order(), _EQUITY, [], "test_strategy", _STRATEGY_RISK)
        assert allowed is True
