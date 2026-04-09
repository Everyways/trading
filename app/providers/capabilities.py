"""BrokerCapabilities — what a given broker supports.

Consulted by the GlobalRiskManager before validating orders.
"""

from decimal import Decimal

from pydantic import BaseModel


class BrokerCapabilities(BaseModel):
    """Feature flags and limits for a specific broker account."""

    supports_fractional: bool = False
    supports_short: bool = False
    supports_stop_orders: bool = True
    supports_stop_limit_orders: bool = True
    supports_bracket_orders: bool = False
    asset_classes: list[str] = ["equity"]
    min_order_value_usd: Decimal = Decimal("1")
    max_order_value_usd: Decimal = Decimal("1000000")
    order_rate_limit_per_minute: int = 200
    supports_extended_hours: bool = False
    timezone: str = "America/New_York"

    def supports_asset_class(self, asset_class: str) -> bool:
        """Return True if this broker supports the given asset class."""
        return asset_class.lower() in [a.lower() for a in self.asset_classes]
