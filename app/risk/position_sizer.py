"""Fixed-fractional position sizing.

Usage:
    qty = size_position(
        account_equity=Decimal("500"),
        entry_price=Decimal("400"),
        stop_loss_pct=2.0,
        risk_pct=1.0,
    )
    # → 0.063 shares  (risk $5, stop $8, qty = 5/8)
"""

from __future__ import annotations

from decimal import Decimal

_MIN_QTY = Decimal("0.001")   # Alpaca minimum fractional share size
_PRECISION = Decimal("0.001")


def size_position(
    account_equity: Decimal,
    entry_price: Decimal,
    stop_loss_pct: float,
    risk_pct: float,
    min_qty: Decimal = _MIN_QTY,
    max_qty: Decimal | None = None,
) -> Decimal:
    """Calculate position size using the fixed-fractional method.

    Formula:
        risk_amount  = equity × risk_pct / 100
        stop_amount  = entry_price × stop_loss_pct / 100
        qty          = risk_amount / stop_amount

    Args:
        account_equity: Current account equity in USD.
        entry_price:    Expected fill price (use current close for market orders).
        stop_loss_pct:  Distance from entry to stop-loss as a percentage (e.g. 2.0 = 2%).
        risk_pct:       Fraction of equity to risk per trade (e.g. 1.0 = 1%).
        min_qty:        Floor quantity — returned when formula yields something smaller.
        max_qty:        Optional ceiling quantity.

    Returns:
        Quantity rounded to 3 decimal places (Alpaca fractional precision).
    """
    if stop_loss_pct <= 0 or risk_pct <= 0 or entry_price <= 0 or account_equity <= 0:
        return min_qty

    risk_amount = account_equity * Decimal(str(risk_pct)) / Decimal("100")
    stop_amount = entry_price * Decimal(str(stop_loss_pct)) / Decimal("100")
    qty = (risk_amount / stop_amount).quantize(_PRECISION)

    qty = max(qty, min_qty)
    if max_qty is not None:
        qty = min(qty, max_qty)
    return qty
