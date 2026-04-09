"""Domain exceptions — typed hierarchy for clean error handling."""


class TradingBotError(Exception):
    """Base exception for all bot errors."""


class BootError(TradingBotError):
    """Raised when the bot cannot start due to a configuration or safety error."""


class BrokerError(TradingBotError):
    """Raised on broker communication failures."""


class BrokerAuthError(BrokerError):
    """Raised on broker authentication failures (401/403)."""


class BrokerRateLimitError(BrokerError):
    """Raised when broker rate limit is hit (429)."""


class BrokerValidationError(BrokerError):
    """Raised on broker validation errors (422) — do not retry."""


class BrokerTimeoutError(BrokerError):
    """Raised when broker call times out."""


class RiskError(TradingBotError):
    """Raised when a risk limit is violated."""


class KillSwitchEngagedError(TradingBotError):
    """Raised when an operation is blocked by an active kill switch."""


class RegistryError(TradingBotError):
    """Raised on plugin registry errors."""


class ConfigError(TradingBotError):
    """Raised on configuration validation errors."""


class UniverseResolutionError(TradingBotError):
    """Raised when universe resolution fails with fail_boot policy."""


class DataError(TradingBotError):
    """Raised on data integrity or availability errors."""


class OrderIdempotenceError(TradingBotError):
    """Raised when an order with the same client_order_id already exists."""


class ReconciliationError(TradingBotError):
    """Raised when position reconciliation detects a mismatch."""


class LookAheadError(TradingBotError):
    """Raised when a non-closed candle is used for signal generation."""


class PDTViolationError(TradingBotError):
    """Raised when a trade would violate Pattern Day Trader rules."""
