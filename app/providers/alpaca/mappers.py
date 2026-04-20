"""Alpaca ↔ domain pure conversion functions.

No I/O, no state, no side effects — all functions are freely testable.
Import only alpaca types here; never let them leak outside this package.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from alpaca.data.models import Bar as AlpacaBar
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.enums import (
    AssetClass as AlpacaAssetClass,
)
from alpaca.trading.enums import (
    OrderClass as AlpacaOrderClass,
)
from alpaca.trading.enums import (
    OrderSide as AlpacaOrderSide,
)
from alpaca.trading.enums import (
    OrderStatus as AlpacaOrderStatus,
)
from alpaca.trading.enums import (
    OrderType as AlpacaOrderType,
)
from alpaca.trading.enums import (
    PositionSide as AlpacaPositionSide,
)
from alpaca.trading.enums import (
    TimeInForce as AlpacaTIF,
)
from alpaca.trading.enums import (
    TradeEvent,
)
from alpaca.trading.models import (
    Asset as AlpacaAsset,
)
from alpaca.trading.models import (
    Order as AlpacaOrder,
)
from alpaca.trading.models import (
    Position as AlpacaPosition,
)
from alpaca.trading.models import (
    TradeAccount as AlpacaTradeAccount,
)
from alpaca.trading.models import (
    TradeUpdate,
)
from alpaca.trading.requests import (
    LimitOrderRequest,
    MarketOrderRequest,
    StopLimitOrderRequest,
    StopLossRequest,
    StopOrderRequest,
    TakeProfitRequest,
)

from app.core.domain import (
    Account,
    Candle,
    Fill,
    Instrument,
    OrderAck,
    OrderRequest,
    Position,
)
from app.core.enums import (
    AssetClass,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    TimeInForce,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Timeframe mapping
# ---------------------------------------------------------------------------

TIMEFRAME_MAP: dict[str, TimeFrame] = {
    "1m": TimeFrame.Minute,
    "5m": TimeFrame(5, TimeFrameUnit.Minute),
    "15m": TimeFrame(15, TimeFrameUnit.Minute),
    "30m": TimeFrame(30, TimeFrameUnit.Minute),
    "1h": TimeFrame.Hour,
    "4h": TimeFrame(4, TimeFrameUnit.Hour),
    "1d": TimeFrame.Day,
    "1w": TimeFrame.Week,
    "1mo": TimeFrame.Month,
}

# ---------------------------------------------------------------------------
# Enum mappers
# ---------------------------------------------------------------------------

_ALPACA_STATUS_TO_DOMAIN: dict[AlpacaOrderStatus, OrderStatus] = {
    AlpacaOrderStatus.NEW: OrderStatus.PENDING,
    AlpacaOrderStatus.PENDING_NEW: OrderStatus.PENDING,
    AlpacaOrderStatus.ACCEPTED: OrderStatus.PENDING,
    AlpacaOrderStatus.PENDING_REVIEW: OrderStatus.PENDING,
    AlpacaOrderStatus.ACCEPTED_FOR_BIDDING: OrderStatus.PENDING,
    AlpacaOrderStatus.HELD: OrderStatus.PENDING,
    AlpacaOrderStatus.PARTIALLY_FILLED: OrderStatus.PARTIALLY_FILLED,
    AlpacaOrderStatus.FILLED: OrderStatus.FILLED,
    AlpacaOrderStatus.CALCULATED: OrderStatus.FILLED,
    AlpacaOrderStatus.CANCELED: OrderStatus.CANCELLED,
    AlpacaOrderStatus.DONE_FOR_DAY: OrderStatus.CANCELLED,
    AlpacaOrderStatus.REPLACED: OrderStatus.CANCELLED,
    AlpacaOrderStatus.EXPIRED: OrderStatus.EXPIRED,
    AlpacaOrderStatus.REJECTED: OrderStatus.REJECTED,
    AlpacaOrderStatus.SUSPENDED: OrderStatus.SUBMITTED,
    AlpacaOrderStatus.STOPPED: OrderStatus.SUBMITTED,
    AlpacaOrderStatus.PENDING_CANCEL: OrderStatus.SUBMITTED,
    AlpacaOrderStatus.PENDING_REPLACE: OrderStatus.SUBMITTED,
}

_ALPACA_ASSET_CLASS_TO_DOMAIN: dict[AlpacaAssetClass, AssetClass] = {
    AlpacaAssetClass.US_EQUITY: AssetClass.EQUITY,
    AlpacaAssetClass.US_OPTION: AssetClass.OPTION,
    AlpacaAssetClass.CRYPTO: AssetClass.CRYPTO,
}

_ALPACA_TIF_TO_DOMAIN: dict[AlpacaTIF, TimeInForce] = {
    AlpacaTIF.DAY: TimeInForce.DAY,
    AlpacaTIF.GTC: TimeInForce.GTC,
    AlpacaTIF.IOC: TimeInForce.IOC,
    AlpacaTIF.FOK: TimeInForce.FOK,
    AlpacaTIF.OPG: TimeInForce.DAY,   # fallback — logged below
    AlpacaTIF.CLS: TimeInForce.DAY,   # fallback — logged below
}

_DOMAIN_TIF_TO_ALPACA: dict[TimeInForce, AlpacaTIF] = {
    TimeInForce.DAY: AlpacaTIF.DAY,
    TimeInForce.GTC: AlpacaTIF.GTC,
    TimeInForce.IOC: AlpacaTIF.IOC,
    TimeInForce.FOK: AlpacaTIF.FOK,
}

_ALPACA_SIDE_TO_DOMAIN: dict[AlpacaOrderSide, OrderSide] = {
    AlpacaOrderSide.BUY: OrderSide.BUY,
    AlpacaOrderSide.SELL: OrderSide.SELL,
}

_DOMAIN_SIDE_TO_ALPACA: dict[OrderSide, AlpacaOrderSide] = {
    OrderSide.BUY: AlpacaOrderSide.BUY,
    OrderSide.SELL: AlpacaOrderSide.SELL,
}


# ---------------------------------------------------------------------------
# Public conversion functions
# ---------------------------------------------------------------------------


def timeframe_str_to_alpaca(tf: str) -> TimeFrame:
    """Convert a timeframe string (e.g. "15m") to an Alpaca TimeFrame.

    Raises ValueError for unknown timeframes.
    """
    try:
        return TIMEFRAME_MAP[tf]
    except KeyError:
        raise ValueError(
            f"Unknown timeframe '{tf}'. Supported: {list(TIMEFRAME_MAP)}"
        ) from None


def alpaca_order_status_to_domain(status: AlpacaOrderStatus) -> OrderStatus:
    """Map an Alpaca OrderStatus to the domain OrderStatus."""
    mapped = _ALPACA_STATUS_TO_DOMAIN.get(status)
    if mapped is None:
        log.warning("Unknown Alpaca OrderStatus %r — defaulting to SUBMITTED", status)
        return OrderStatus.SUBMITTED
    return mapped


def account_to_domain(acc: AlpacaTradeAccount) -> Account:
    """Convert an Alpaca TradeAccount to a domain Account."""
    return Account(
        equity=Decimal(acc.equity or "0"),
        cash=Decimal(acc.cash or "0"),
        buying_power=Decimal(acc.buying_power or "0"),
        currency="USD",
        day_trade_count=int(getattr(acc, "daytrade_count", 0) or 0),
        pattern_day_trader=bool(acc.pattern_day_trader),
    )


def position_to_domain(pos: AlpacaPosition) -> Position:
    """Convert an Alpaca Position to a domain Position."""
    side = (
        PositionSide.LONG
        if pos.side == AlpacaPositionSide.LONG
        else PositionSide.SHORT
    )
    qty = Decimal(pos.qty)
    if side == PositionSide.SHORT:
        qty = -abs(qty)

    return Position(
        symbol=pos.symbol,
        qty=qty,
        avg_entry_price=Decimal(pos.avg_entry_price),
        current_price=Decimal(pos.current_price) if pos.current_price else None,
        unrealized_pnl=Decimal(pos.unrealized_pl) if pos.unrealized_pl else None,
        side=side,
    )


def order_to_ack(order: AlpacaOrder) -> OrderAck:
    """Convert an Alpaca Order to a domain OrderAck."""
    status = alpaca_order_status_to_domain(order.status)

    # TIF mapping with OPG/CLS fallback warning
    alpaca_tif = order.time_in_force
    if alpaca_tif in (AlpacaTIF.OPG, AlpacaTIF.CLS):
        log.warning("TIF %r mapped to DAY — not natively supported in domain", alpaca_tif)

    # Qty fields may be str or float
    qty = Decimal(str(order.qty or "0"))
    filled_qty = Decimal(str(order.filled_qty or "0"))
    avg_fill_price = (
        Decimal(str(order.filled_avg_price))
        if order.filled_avg_price is not None
        else None
    )

    # Map order type — trailing_stop falls back to stop
    alpaca_type = order.type
    type_map: dict[AlpacaOrderType, OrderType] = {
        AlpacaOrderType.MARKET: OrderType.MARKET,
        AlpacaOrderType.LIMIT: OrderType.LIMIT,
        AlpacaOrderType.STOP: OrderType.STOP,
        AlpacaOrderType.STOP_LIMIT: OrderType.STOP_LIMIT,
        AlpacaOrderType.TRAILING_STOP: OrderType.STOP,
    }
    order_type = type_map.get(alpaca_type or AlpacaOrderType.MARKET, OrderType.MARKET)

    side = _ALPACA_SIDE_TO_DOMAIN.get(
        order.side or AlpacaOrderSide.BUY, OrderSide.BUY
    )

    stop_price = (
        Decimal(str(order.stop_price))
        if order.stop_price is not None
        else None
    )

    return OrderAck(
        client_order_id=order.client_order_id,
        broker_order_id=str(order.id),
        status=status,
        symbol=order.symbol or "",
        side=side,
        type=order_type,
        qty=qty,
        filled_qty=filled_qty,
        avg_fill_price=avg_fill_price,
        stop_price=stop_price,
        submitted_at=order.submitted_at,
        filled_at=order.filled_at,
    )


def bar_to_candle(
    bar: AlpacaBar,
    symbol: str,
    timeframe: str,
    is_closed: bool = True,
) -> Candle:
    """Convert an Alpaca Bar to a domain Candle.

    Bar OHLCV fields are float — converted via str() to avoid floating-point
    representation issues (e.g. 1.2300000000000002).
    """
    return Candle(
        time=bar.timestamp,
        symbol=symbol,
        timeframe=timeframe,
        open=Decimal(str(bar.open)),
        high=Decimal(str(bar.high)),
        low=Decimal(str(bar.low)),
        close=Decimal(str(bar.close)),
        volume=Decimal(str(bar.volume)),
        is_closed=is_closed,
    )


def asset_to_instrument(asset: AlpacaAsset) -> Instrument:
    """Convert an Alpaca Asset to a domain Instrument."""
    asset_class = _ALPACA_ASSET_CLASS_TO_DOMAIN.get(
        asset.asset_class, AssetClass.EQUITY
    )
    tick_size = (
        Decimal(str(asset.price_increment)) if asset.price_increment else None
    )
    min_qty = (
        Decimal(str(asset.min_order_size)) if asset.min_order_size else None
    )
    return Instrument(
        symbol=asset.symbol,
        asset_class=asset_class,
        provider_name="alpaca",
        tick_size=tick_size,
        min_qty=min_qty,
        active=asset.tradable,
    )


def order_request_to_alpaca(
    req: OrderRequest,
) -> MarketOrderRequest | LimitOrderRequest | StopOrderRequest | StopLimitOrderRequest:
    """Build the correct Alpaca order request subtype from a domain OrderRequest.

    client_order_id is always passed so the request is idempotent.
    """
    alpaca_side = _DOMAIN_SIDE_TO_ALPACA[req.side]
    alpaca_tif = _DOMAIN_TIF_TO_ALPACA.get(req.time_in_force, AlpacaTIF.DAY)

    common = dict(
        symbol=req.symbol,
        qty=float(req.qty),
        side=alpaca_side,
        time_in_force=alpaca_tif,
        client_order_id=req.client_order_id,
        extended_hours=req.extended_hours,
    )

    if req.type == OrderType.MARKET:
        if req.stop_loss_price and req.take_profit_price:
            return MarketOrderRequest(
                **common,
                order_class=AlpacaOrderClass.BRACKET,
                stop_loss=StopLossRequest(stop_price=float(req.stop_loss_price)),
                take_profit=TakeProfitRequest(limit_price=float(req.take_profit_price)),
            )
        return MarketOrderRequest(**common)
    if req.type == OrderType.LIMIT:
        return LimitOrderRequest(limit_price=float(req.limit_price or 0), **common)
    if req.type == OrderType.STOP:
        return StopOrderRequest(stop_price=float(req.stop_price or 0), **common)
    if req.type == OrderType.STOP_LIMIT:
        return StopLimitOrderRequest(
            stop_price=float(req.stop_price or 0),
            limit_price=float(req.limit_price or 0),
            **common,
        )
    raise ValueError(f"Unsupported order type: {req.type}")


def trade_update_to_fill(tu: TradeUpdate) -> Fill | None:
    """Convert an Alpaca TradeUpdate to a domain Fill.

    Returns None if the event is not a fill or partial fill.
    """
    if tu.event not in (TradeEvent.FILL, TradeEvent.PARTIAL_FILL):
        return None

    order = tu.order
    side = _ALPACA_SIDE_TO_DOMAIN.get(
        order.side or AlpacaOrderSide.BUY, OrderSide.BUY
    )

    return Fill(
        client_order_id=order.client_order_id,
        broker_order_id=str(order.id),
        symbol=order.symbol or "",
        qty=Decimal(str(tu.qty or "0")),
        price=Decimal(str(tu.price or "0")),
        side=side,
        timestamp=tu.timestamp,
    )
