"""Tests for AlpacaProvider — TradingClient is fully mocked, 0 network calls."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from app.core.domain import OrderRequest
from app.core.enums import OrderSide, OrderStatus, OrderType
from app.providers.alpaca.config import AlpacaConfig
from app.providers.alpaca.provider import AlpacaProvider

NOW = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)


def _make_config(paper: bool = True) -> AlpacaConfig:
    base_url = (
        "https://paper-api.alpaca.markets"
        if paper
        else "https://api.alpaca.markets"
    )
    return AlpacaConfig(
        alpaca_api_key="PKTEST123",
        alpaca_api_secret="SECRET456",
        alpaca_base_url=base_url,
    )


def _make_provider(paper: bool = True) -> AlpacaProvider:
    return AlpacaProvider(config=_make_config(paper))


def _mock_trading_client():
    """Return a MagicMock that mimics TradingClient."""
    client = MagicMock()
    # get_account
    acc = MagicMock()
    acc.equity = "10000.00"
    acc.cash = "5000.00"
    acc.buying_power = "20000.00"
    acc.pattern_day_trader = False
    acc.daytrade_count = 0
    client.get_account.return_value = acc
    # get_all_positions
    client.get_all_positions.return_value = []
    # get_clock
    clock = MagicMock()
    client.get_clock.return_value = clock
    return client


def _mock_submitted_order(client_order_id: str):
    order = MagicMock()
    order.id = uuid.uuid4()
    order.client_order_id = client_order_id
    order.status = MagicMock()
    order.status.value = "new"

    from alpaca.trading.enums import OrderSide as AlpacaOrderSide
    from alpaca.trading.enums import OrderStatus as AlpacaOrderStatus
    from alpaca.trading.enums import OrderType as AlpacaOrderType
    order.status = AlpacaOrderStatus.NEW
    order.side = AlpacaOrderSide.BUY
    order.type = AlpacaOrderType.MARKET
    order.symbol = "SPY"
    order.qty = "5"
    order.filled_qty = "0"
    order.filled_avg_price = None
    order.submitted_at = NOW
    order.filled_at = None
    order.time_in_force = MagicMock()

    from alpaca.trading.enums import TimeInForce as AlpacaTIF
    order.time_in_force = AlpacaTIF.DAY
    return order


# ---------------------------------------------------------------------------
# connect / disconnect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_creates_clients():
    provider = _make_provider()
    with (
        patch("app.providers.alpaca.provider.TradingClient") as mock_tc,
        patch("app.providers.alpaca.provider.StockHistoricalDataClient") as mock_dc,
    ):
        await provider.connect()
        mock_tc.assert_called_once_with(
            api_key="PKTEST123",
            secret_key="SECRET456",
            paper=True,
        )
        mock_dc.assert_called_once()
        assert provider._client is not None
        assert provider._data_client is not None


@pytest.mark.asyncio
async def test_disconnect_clears_clients():
    provider = _make_provider()
    with (
        patch("app.providers.alpaca.provider.TradingClient"),
        patch("app.providers.alpaca.provider.StockHistoricalDataClient"),
    ):
        await provider.connect()
        await provider.disconnect()
    assert provider._client is None
    assert provider._data_client is None


# ---------------------------------------------------------------------------
# get_account
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_account_calls_to_thread():
    provider = _make_provider()
    mock_client = _mock_trading_client()
    provider._client = mock_client

    account = await provider.get_account()

    mock_client.get_account.assert_called_once()
    assert account.equity == Decimal("10000.00")
    assert account.cash == Decimal("5000.00")
    assert account.buying_power == Decimal("20000.00")


# ---------------------------------------------------------------------------
# get_positions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_positions_returns_empty_list():
    provider = _make_provider()
    mock_client = _mock_trading_client()
    provider._client = mock_client

    positions = await provider.get_positions()

    mock_client.get_all_positions.assert_called_once()
    assert positions == []


# ---------------------------------------------------------------------------
# submit_order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_order_uses_client_order_id_from_request():
    """client_order_id must be generated in OrderRequest (before broker call)."""
    provider = _make_provider()
    mock_client = _mock_trading_client()
    provider._client = mock_client

    req = OrderRequest(
        symbol="SPY",
        side=OrderSide.BUY,
        type=OrderType.MARKET,
        qty=Decimal("5"),
    )
    expected_coid = req.client_order_id  # captured before submit

    mock_order = _mock_submitted_order(client_order_id=expected_coid)
    mock_client.submit_order.return_value = mock_order

    ack = await provider.submit_order(req)

    mock_client.submit_order.assert_called_once()
    assert ack.client_order_id == expected_coid
    assert ack.status == OrderStatus.PENDING


@pytest.mark.asyncio
async def test_submit_order_client_order_id_set_before_broker_call():
    """Verify client_order_id is on the alpaca request object passed to the client."""
    provider = _make_provider()
    mock_client = _mock_trading_client()
    provider._client = mock_client

    captured_requests = []

    def capture_submit(alpaca_req):
        captured_requests.append(alpaca_req)
        return _mock_submitted_order(client_order_id=alpaca_req.client_order_id)

    mock_client.submit_order.side_effect = capture_submit

    req = OrderRequest(
        symbol="AAPL",
        side=OrderSide.BUY,
        type=OrderType.MARKET,
        qty=Decimal("1"),
    )
    await provider.submit_order(req)

    assert len(captured_requests) == 1
    assert captured_requests[0].client_order_id == req.client_order_id


# ---------------------------------------------------------------------------
# healthcheck
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_healthcheck_true_when_clock_succeeds():
    provider = _make_provider()
    mock_client = _mock_trading_client()
    provider._client = mock_client

    result = await provider.healthcheck()

    mock_client.get_clock.assert_called_once()
    assert result is True


@pytest.mark.asyncio
async def test_healthcheck_false_when_exception():
    provider = _make_provider()
    mock_client = _mock_trading_client()
    mock_client.get_clock.side_effect = Exception("connection refused")
    provider._client = mock_client

    result = await provider.healthcheck()
    assert result is False


@pytest.mark.asyncio
async def test_healthcheck_false_when_not_connected():
    provider = _make_provider()
    assert provider._client is None
    result = await provider.healthcheck()
    assert result is False


# ---------------------------------------------------------------------------
# is_paper property
# ---------------------------------------------------------------------------


def test_is_paper_true_for_paper_url():
    provider = _make_provider(paper=True)
    assert provider._config.is_paper is True


def test_is_paper_false_for_live_url():
    provider = _make_provider(paper=False)
    assert provider._config.is_paper is False


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_alpaca_registered_in_broker_registry():
    import app.providers  # noqa: F401 — triggers registration
    from app.core.registry import broker_registry
    assert "alpaca" in broker_registry
    assert broker_registry.get("alpaca") is AlpacaProvider
