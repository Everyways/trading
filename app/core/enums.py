"""Domain enumerations — all string-based for JSON/DB compatibility."""

from enum import Enum


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class OrderStatus(str, Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class TimeInForce(str, Enum):
    DAY = "day"
    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"


class StrategyMode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


class AssetClass(str, Enum):
    EQUITY = "equity"
    CRYPTO = "crypto"
    OPTION = "option"
    FOREX = "forex"


class SignalSide(str, Enum):
    BUY = "buy"
    SELL = "sell"
    CLOSE = "close"


class KillSwitchScope(str, Enum):
    GLOBAL = "global"
    STRATEGY = "strategy"


class RiskSeverity(str, Enum):
    INFO = "info"
    WARN = "warn"
    CRITICAL = "critical"


class RiskEventType(str, Enum):
    DRAWDOWN_LIMIT = "drawdown_limit"
    DAILY_LOSS_LIMIT = "daily_loss_limit"
    MONTHLY_LOSS_LIMIT = "monthly_loss_limit"
    EXPOSURE_LIMIT = "exposure_limit"
    ORDER_RATE_LIMIT = "order_rate_limit"
    BROKER_ERROR = "broker_error"
    RECONCILIATION_MISMATCH = "reconciliation_mismatch"
    KILL_SWITCH_ENGAGED = "kill_switch_engaged"
    KILL_SWITCH_RELEASED = "kill_switch_released"
    GATE_BYPASSED = "gate_bypassed"
    UNIVERSE_RESOLVED = "universe_resolved"
    RUNNER_CRASH = "runner_crash"


class PositionSide(str, Enum):
    LONG = "long"
    SHORT = "short"


class DataSource(str, Enum):
    REST = "rest"
    WEBSOCKET = "ws"


class UniverseResolutionStrategy(str, Enum):
    FIRST_AVAILABLE = "first_available"


class OnNoProfileMatch(str, Enum):
    DISABLE_STRATEGY = "disable_strategy"
    FAIL_BOOT = "fail_boot"
