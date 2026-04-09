"""Tests for domain enumerations."""

from app.core.enums import (
    AssetClass,
    KillSwitchScope,
    OrderSide,
    OrderStatus,
    OrderType,
    RiskSeverity,
    SignalSide,
    StrategyMode,
    TimeInForce,
)


def test_order_side_values() -> None:
    assert OrderSide.BUY == "buy"
    assert OrderSide.SELL == "sell"


def test_order_type_values() -> None:
    assert OrderType.MARKET == "market"
    assert OrderType.LIMIT == "limit"
    assert OrderType.STOP == "stop"
    assert OrderType.STOP_LIMIT == "stop_limit"


def test_strategy_mode_values() -> None:
    assert StrategyMode.PAPER == "paper"
    assert StrategyMode.LIVE == "live"


def test_asset_class_values() -> None:
    assert AssetClass.EQUITY == "equity"
    assert AssetClass.CRYPTO == "crypto"


def test_kill_switch_scope_values() -> None:
    assert KillSwitchScope.GLOBAL == "global"
    assert KillSwitchScope.STRATEGY == "strategy"


def test_risk_severity_ordering() -> None:
    """Severities must be distinguishable strings."""
    assert RiskSeverity.INFO != RiskSeverity.WARN
    assert RiskSeverity.WARN != RiskSeverity.CRITICAL


def test_enums_are_str_subclass() -> None:
    """All enums must be str subclasses for JSON/DB serialization."""
    assert isinstance(OrderSide.BUY, str)
    assert isinstance(StrategyMode.LIVE, str)
    assert isinstance(AssetClass.EQUITY, str)
    assert isinstance(SignalSide.CLOSE, str)
    assert isinstance(TimeInForce.GTC, str)
    assert isinstance(OrderStatus.FILLED, str)
    assert isinstance(KillSwitchScope.GLOBAL, str)
    assert isinstance(RiskSeverity.CRITICAL, str)
