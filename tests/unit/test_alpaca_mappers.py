"""Tests for app/providers/alpaca/mappers.py — pure functions, 0 network calls."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from alpaca.data.models import Bar as AlpacaBar
from alpaca.data.timeframe import TimeFrameUnit
from alpaca.trading.enums import (
    OrderStatus as AlpacaOrderStatus,
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
    StopOrderRequest,
)

from app.core.domain import OrderRequest
from app.core.enums import (
    AssetClass,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
)
from app.providers.alpaca.mappers import (
    account_to_domain,
    alpaca_order_status_to_domain,
    asset_to_instrument,
    bar_to_candle,
    order_request_to_alpaca,
    order_to_ack,
    position_to_domain,
    timeframe_str_to_alpaca,
    trade_update_to_fill,
)

NOW = datetime(2024, 1, 15, 14, 30, tzinfo=UTC)
NOW_ISO = NOW.isoformat()
ORDER_UUID = str(uuid.uuid4())
ASSET_UUID = str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Helpers to build minimal Alpaca objects
# Note: alpaca-py SDK models use wire-format aliases and custom constructors.
# ---------------------------------------------------------------------------


def _make_account(**kwargs) -> AlpacaTradeAccount:
    defaults = dict(
        id=str(uuid.uuid4()),
        account_number="TEST001",
        status="ACTIVE",
        currency="USD",
        equity="10000.00",
        cash="5000.00",
        buying_power="20000.00",
        pattern_day_trader=False,
        daytrade_count=0,
    )
    defaults.update(kwargs)
    return AlpacaTradeAccount.model_validate(defaults)


def _make_position(**kwargs) -> AlpacaPosition:
    defaults = dict(
        asset_id=str(uuid.uuid4()),
        symbol="SPY",
        exchange="NYSE",
        asset_class="us_equity",
        avg_entry_price="450.00",
        qty="10",
        side="long",
        market_value="4600.00",
        cost_basis="4500.00",
        unrealized_pl="100.00",
        current_price="460.00",
    )
    defaults.update(kwargs)
    return AlpacaPosition.model_validate(defaults)


def _make_order(**kwargs) -> AlpacaOrder:
    """Build an Alpaca Order object via model_validate.

    Required fields (from SDK): id, client_order_id, created_at, updated_at,
    submitted_at, asset_id, symbol, asset_class, qty, filled_qty, type, side,
    time_in_force, status, extended_hours.
    """
    defaults = dict(
        id=str(uuid.uuid4()),
        client_order_id=str(uuid.uuid4()),
        created_at=NOW_ISO,
        updated_at=NOW_ISO,
        submitted_at=NOW_ISO,
        asset_id=str(uuid.uuid4()),
        symbol="AAPL",
        asset_class="us_equity",
        qty="5",
        filled_qty="0",
        type="market",
        side="buy",
        time_in_force="day",
        status="new",
        extended_hours=False,
    )
    defaults.update(kwargs)
    return AlpacaOrder.model_validate(defaults)


def _make_bar(**kwargs) -> AlpacaBar:
    """Build an Alpaca Bar using the SDK's raw_data constructor.

    Wire-format keys: t=timestamp, o=open, h=high, l=low, c=close,
    v=volume, n=trade_count, vw=vwap.
    """
    symbol = kwargs.pop("symbol", "SPY")
    raw_defaults = dict(
        t=NOW_ISO,
        o=450.1234567,
        h=455.9876543,
        l=449.5000001,
        c=453.2500001,
        v=1234567.0,
        n=5000,
        vw=452.0,
    )
    # Allow overriding raw keys
    raw_defaults.update({k: v for k, v in kwargs.items()})
    return AlpacaBar(symbol=symbol, raw_data=raw_defaults)


def _make_asset(**kwargs) -> AlpacaAsset:
    """Build an Alpaca Asset. Note: wire-format uses 'class' for asset_class."""
    defaults = dict(
        id=str(uuid.uuid4()),
        **{"class": "us_equity"},  # wire-format alias
        exchange="NASDAQ",
        symbol="AAPL",
        name="Apple Inc.",
        status="active",
        tradable=True,
        marginable=True,
        shortable=True,
        easy_to_borrow=True,
        fractionable=True,
    )
    defaults.update(kwargs)
    return AlpacaAsset.model_validate(defaults)


def _make_trade_update(event: str = "fill", price: float = 100.0, qty: float = 5.0) -> TradeUpdate:
    order_data = dict(
        id=str(uuid.uuid4()),
        client_order_id=str(uuid.uuid4()),
        created_at=NOW_ISO,
        updated_at=NOW_ISO,
        submitted_at=NOW_ISO,
        asset_id=str(uuid.uuid4()),
        symbol="AAPL",
        asset_class="us_equity",
        qty=str(qty),
        filled_qty=str(qty) if event == "fill" else "0",
        type="market",
        side="buy",
        time_in_force="day",
        status="filled" if event == "fill" else "new",
        extended_hours=False,
    )
    return TradeUpdate.model_validate(dict(
        event=event,
        order=order_data,
        timestamp=NOW_ISO,
        price=price,
        qty=qty,
    ))


# ---------------------------------------------------------------------------
# timeframe_str_to_alpaca
# ---------------------------------------------------------------------------


class TestTimeframeStrToAlpaca:
    def test_1m(self):
        tf = timeframe_str_to_alpaca("1m")
        assert tf.amount_value == 1
        assert tf.unit_value == TimeFrameUnit.Minute

    def test_15m(self):
        tf = timeframe_str_to_alpaca("15m")
        assert tf.amount_value == 15
        assert tf.unit_value == TimeFrameUnit.Minute

    def test_1h(self):
        tf = timeframe_str_to_alpaca("1h")
        assert tf.amount_value == 1
        assert tf.unit_value == TimeFrameUnit.Hour

    def test_1d(self):
        tf = timeframe_str_to_alpaca("1d")
        assert tf.amount_value == 1
        assert tf.unit_value == TimeFrameUnit.Day

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown timeframe"):
            timeframe_str_to_alpaca("3d")


# ---------------------------------------------------------------------------
# alpaca_order_status_to_domain
# ---------------------------------------------------------------------------


class TestOrderStatusMapping:
    def test_new_maps_to_pending(self):
        assert alpaca_order_status_to_domain(AlpacaOrderStatus.NEW) == OrderStatus.PENDING

    def test_pending_new_maps_to_pending(self):
        assert alpaca_order_status_to_domain(AlpacaOrderStatus.PENDING_NEW) == OrderStatus.PENDING

    def test_canceled_maps_to_cancelled(self):
        result = alpaca_order_status_to_domain(AlpacaOrderStatus.CANCELED)
        assert result == OrderStatus.CANCELLED

    def test_done_for_day_maps_to_cancelled(self):
        result = alpaca_order_status_to_domain(AlpacaOrderStatus.DONE_FOR_DAY)
        assert result == OrderStatus.CANCELLED

    def test_filled_maps_to_filled(self):
        assert alpaca_order_status_to_domain(AlpacaOrderStatus.FILLED) == OrderStatus.FILLED

    def test_partially_filled_maps_to_partially_filled(self):
        result = alpaca_order_status_to_domain(AlpacaOrderStatus.PARTIALLY_FILLED)
        assert result == OrderStatus.PARTIALLY_FILLED

    def test_rejected_maps_to_rejected(self):
        assert alpaca_order_status_to_domain(AlpacaOrderStatus.REJECTED) == OrderStatus.REJECTED

    def test_expired_maps_to_expired(self):
        assert alpaca_order_status_to_domain(AlpacaOrderStatus.EXPIRED) == OrderStatus.EXPIRED


# ---------------------------------------------------------------------------
# account_to_domain
# ---------------------------------------------------------------------------


class TestAccountToDomain:
    def test_decimal_conversion(self):
        acc = _make_account(equity="12345.67", cash="5000.00", buying_power="25000.00")
        domain = account_to_domain(acc)
        assert domain.equity == Decimal("12345.67")
        assert domain.cash == Decimal("5000.00")
        assert domain.buying_power == Decimal("25000.00")

    def test_pattern_day_trader_false(self):
        acc = _make_account(pattern_day_trader=False)
        assert account_to_domain(acc).pattern_day_trader is False

    def test_currency_is_usd(self):
        assert account_to_domain(_make_account()).currency == "USD"


# ---------------------------------------------------------------------------
# position_to_domain
# ---------------------------------------------------------------------------


class TestPositionToDomain:
    def test_long_position(self):
        pos = _make_position(side="long", qty="10", avg_entry_price="450.50")
        domain = position_to_domain(pos)
        assert domain.qty == Decimal("10")
        assert domain.avg_entry_price == Decimal("450.50")
        assert domain.side == PositionSide.LONG

    def test_short_position_has_negative_qty(self):
        pos = _make_position(side="short", qty="5", avg_entry_price="300.00")
        domain = position_to_domain(pos)
        assert domain.qty == Decimal("-5")
        assert domain.side == PositionSide.SHORT

    def test_unrealized_pnl_mapped(self):
        pos = _make_position(unrealized_pl="123.45")
        domain = position_to_domain(pos)
        assert domain.unrealized_pnl == Decimal("123.45")

    def test_current_price_mapped(self):
        pos = _make_position(current_price="460.00")
        domain = position_to_domain(pos)
        assert domain.current_price == Decimal("460.00")

    def test_symbol_preserved(self):
        pos = _make_position(symbol="MSFT")
        assert position_to_domain(pos).symbol == "MSFT"


# ---------------------------------------------------------------------------
# bar_to_candle
# ---------------------------------------------------------------------------


class TestBarToCandle:
    def test_float_to_decimal_precision(self):
        bar = _make_bar(**{"o": 450.1234567, "h": 455.9876543, "l": 449.5000001, "c": 453.2500001})
        candle = bar_to_candle(bar, "SPY", "15m")
        assert isinstance(candle.open, Decimal)
        assert isinstance(candle.close, Decimal)
        assert candle.open == Decimal(str(450.1234567))

    def test_is_closed_true_by_default(self):
        candle = bar_to_candle(_make_bar(), "SPY", "1d")
        assert candle.is_closed is True

    def test_is_closed_false_when_specified(self):
        candle = bar_to_candle(_make_bar(), "SPY", "1m", is_closed=False)
        assert candle.is_closed is False

    def test_volume_decimal(self):
        bar = _make_bar(**{"v": 1234567.0})
        candle = bar_to_candle(bar, "SPY", "1d")
        assert candle.volume == Decimal(str(1234567.0))

    def test_symbol_and_timeframe_set(self):
        candle = bar_to_candle(_make_bar(), "QQQ", "15m")
        assert candle.symbol == "QQQ"
        assert candle.timeframe == "15m"

    def test_timestamp_preserved(self):
        candle = bar_to_candle(_make_bar(), "SPY", "1d")
        assert candle.time == NOW


# ---------------------------------------------------------------------------
# order_to_ack
# ---------------------------------------------------------------------------


class TestOrderToAck:
    def test_status_new_maps_to_pending(self):
        order = _make_order(status="new")
        ack = order_to_ack(order)
        assert ack.status == OrderStatus.PENDING

    def test_status_canceled_maps_to_cancelled(self):
        order = _make_order(status="canceled")
        ack = order_to_ack(order)
        assert ack.status == OrderStatus.CANCELLED

    def test_broker_order_id_is_string(self):
        order = _make_order()
        ack = order_to_ack(order)
        assert isinstance(ack.broker_order_id, str)

    def test_qty_is_decimal(self):
        order = _make_order(qty="7", filled_qty="3")
        ack = order_to_ack(order)
        assert ack.qty == Decimal("7")
        assert ack.filled_qty == Decimal("3")

    def test_avg_fill_price_none_when_not_filled(self):
        order = _make_order(filled_avg_price=None)
        ack = order_to_ack(order)
        assert ack.avg_fill_price is None

    def test_avg_fill_price_decimal_when_filled(self):
        order = _make_order(status="filled", filled_qty="5", filled_avg_price="123.45")
        ack = order_to_ack(order)
        assert ack.avg_fill_price == Decimal("123.45")

    def test_submitted_at_preserved(self):
        order = _make_order()
        ack = order_to_ack(order)
        assert ack.submitted_at is not None


# ---------------------------------------------------------------------------
# order_request_to_alpaca
# ---------------------------------------------------------------------------


class TestOrderRequestToAlpaca:
    def test_market_order(self):
        req = OrderRequest(
            symbol="SPY", side=OrderSide.BUY, type=OrderType.MARKET, qty=Decimal("10")
        )
        alpaca_req = order_request_to_alpaca(req)
        assert isinstance(alpaca_req, MarketOrderRequest)
        assert alpaca_req.symbol == "SPY"

    def test_limit_order(self):
        req = OrderRequest(
            symbol="AAPL", side=OrderSide.SELL, type=OrderType.LIMIT,
            qty=Decimal("5"), limit_price=Decimal("175.00"),
        )
        alpaca_req = order_request_to_alpaca(req)
        assert isinstance(alpaca_req, LimitOrderRequest)
        assert alpaca_req.limit_price == 175.0

    def test_stop_order(self):
        req = OrderRequest(
            symbol="TSLA", side=OrderSide.SELL, type=OrderType.STOP,
            qty=Decimal("2"), stop_price=Decimal("200.00"),
        )
        alpaca_req = order_request_to_alpaca(req)
        assert isinstance(alpaca_req, StopOrderRequest)

    def test_stop_limit_order(self):
        req = OrderRequest(
            symbol="NVDA", side=OrderSide.BUY, type=OrderType.STOP_LIMIT,
            qty=Decimal("1"), stop_price=Decimal("500.00"), limit_price=Decimal("498.00"),
        )
        alpaca_req = order_request_to_alpaca(req)
        assert isinstance(alpaca_req, StopLimitOrderRequest)

    def test_client_order_id_passed_through(self):
        req = OrderRequest(
            symbol="SPY", side=OrderSide.BUY, type=OrderType.MARKET, qty=Decimal("1")
        )
        alpaca_req = order_request_to_alpaca(req)
        assert alpaca_req.client_order_id == req.client_order_id


# ---------------------------------------------------------------------------
# trade_update_to_fill
# ---------------------------------------------------------------------------


class TestTradeUpdateToFill:
    def test_fill_event_returns_fill(self):
        tu = _make_trade_update(event="fill", price=155.50, qty=10.0)
        fill = trade_update_to_fill(tu)
        assert fill is not None
        assert fill.price == Decimal(str(155.50))
        assert fill.qty == Decimal(str(10.0))

    def test_partial_fill_event_returns_fill(self):
        tu = _make_trade_update(event="partial_fill", price=200.0, qty=3.0)
        fill = trade_update_to_fill(tu)
        assert fill is not None

    def test_non_fill_event_returns_none(self):
        for event in ("new", "canceled", "pending_new", "accepted"):
            tu = _make_trade_update(event=event)
            assert trade_update_to_fill(tu) is None, f"Expected None for event={event}"

    def test_fill_has_symbol(self):
        tu = _make_trade_update(event="fill")
        fill = trade_update_to_fill(tu)
        assert fill is not None
        assert fill.symbol == "AAPL"


# ---------------------------------------------------------------------------
# asset_to_instrument
# ---------------------------------------------------------------------------


class TestAssetToInstrument:
    def test_equity_mapping(self):
        asset = _make_asset(**{"class": "us_equity", "symbol": "AAPL"})
        instrument = asset_to_instrument(asset)
        assert instrument.asset_class == AssetClass.EQUITY
        assert instrument.symbol == "AAPL"
        assert instrument.provider_name == "alpaca"

    def test_option_mapping(self):
        asset = _make_asset(**{"class": "us_option", "symbol": "AAPL240119C00150000"})
        instrument = asset_to_instrument(asset)
        assert instrument.asset_class == AssetClass.OPTION

    def test_tradable_sets_active(self):
        asset = _make_asset(tradable=True)
        assert asset_to_instrument(asset).active is True

    def test_tick_size_from_price_increment(self):
        asset = _make_asset(price_increment=0.01)
        instrument = asset_to_instrument(asset)
        assert instrument.tick_size == Decimal(str(0.01))
