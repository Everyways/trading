"""Unit tests for the ATR trailing stop ratchet in TradingRunner._check_bracket_health.

Verifies:
- The ratchet never loosens (never moves stop DOWN).
- cancel_order + submit_order is called exactly once per upward ratchet.
- No action when proposed_stop <= existing_stop.
- No action when trailing_stop_atr_multiplier is absent.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from app.core.domain import (
    OrderAck,
    OrderRequest,
    Position,
)
from app.core.enums import OrderSide, OrderStatus, OrderType
from app.execution.strategy_loader import StrategyConfig, UniverseEntry


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _make_cfg(
    atr_mult: float | None = None,
    stop_loss_pct: float = 2.0,
) -> StrategyConfig:
    params: dict = {"stop_loss_pct": stop_loss_pct}
    if atr_mult is not None:
        params["trailing_stop_atr_multiplier"] = atr_mult
    return StrategyConfig(
        name="test_strategy",
        version="1.0",
        enabled=True,
        mode="paper",
        provider="dummy",
        timeframe="15m",
        lookback=50,
        universe=[UniverseEntry(symbol="SPY", asset_class="equity")],
        params=params,
        risk={},
    )


def _make_position(
    symbol: str = "SPY",
    avg_entry: float = 100.0,
    current_price: float = 110.0,
    qty: float = 10.0,
) -> Position:
    return Position(
        symbol=symbol,
        qty=Decimal(str(qty)),
        avg_entry_price=Decimal(str(avg_entry)),
        current_price=Decimal(str(current_price)),
    )


def _make_stop_order(
    symbol: str = "SPY",
    stop_price: float = 98.0,
    broker_id: str = "order-001",
) -> OrderAck:
    return OrderAck(
        client_order_id="cl-001",
        broker_order_id=broker_id,
        status=OrderStatus.SUBMITTED,
        symbol=symbol,
        side=OrderSide.SELL,
        type=OrderType.STOP,
        qty=Decimal("10"),
        stop_price=Decimal(str(stop_price)),
    )


def _make_submit_ack(symbol: str = "SPY") -> OrderAck:
    return OrderAck(
        client_order_id="cl-new",
        broker_order_id="order-new",
        status=OrderStatus.SUBMITTED,
        symbol=symbol,
        side=OrderSide.SELL,
        type=OrderType.STOP,
        qty=Decimal("10"),
    )


def _build_runner(
    cfg: StrategyConfig,
    positions: list[Position],
    stop_orders: list[OrderAck],
    atr_value: float = 1.5,
) -> tuple[object, AsyncMock, AsyncMock]:
    """Return (runner, cancel_mock, submit_mock)."""
    from app.core.enums import SignalSide
    from app.core.domain import Signal, Instrument
    from app.core.enums import AssetClass
    from app.execution.runner import TradingRunner
    from app.strategies.base import Strategy, StrategyContext

    class _TestStrategy(Strategy):
        name = "test_strategy"
        version = "1.0"
        description = "test"
        required_timeframe = "15m"
        required_lookback = 1

        def generate_signal(self, candles: object, ctx: StrategyContext) -> None:
            return None

    provider = AsyncMock()
    provider.name = "dummy"
    provider.get_positions = AsyncMock(return_value=positions)
    provider.list_open_orders = AsyncMock(return_value=stop_orders)
    provider.cancel_order = AsyncMock()
    provider.submit_order = AsyncMock(return_value=_make_submit_ack())

    risk_manager = MagicMock()
    risk_manager.is_halted.return_value = False
    risk_manager.global_kill_engaged = False

    session = MagicMock()

    # Patch registry so TradingRunner.__init__ can find the strategy
    with patch("app.execution.runner.strategy_registry") as mock_reg:
        mock_reg.get.return_value = _TestStrategy
        runner = TradingRunner(
            provider=provider,
            strategy_configs=[cfg],
            risk_manager=risk_manager,
            session=session,
        )

    # Patch _compute_atr to return a deterministic value
    async def _fake_atr(symbol: str, timeframe: str, period: int = 14) -> Decimal:
        return Decimal(str(atr_value))

    runner._compute_atr = _fake_atr  # type: ignore[method-assign]

    return runner, provider.cancel_order, provider.submit_order


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestTrailingStopRatchet:
    def test_ratchet_up_when_proposed_higher(self) -> None:
        """Stop moves up when current_price - ATR×N > existing_stop."""
        cfg = _make_cfg(atr_mult=2.0)
        # existing_stop=98, proposed=110 - 1.5*2 = 107 → should ratchet
        pos = _make_position(current_price=110.0)
        stop_order = _make_stop_order(stop_price=98.0)
        runner, cancel_mock, submit_mock = _build_runner(cfg, [pos], [stop_order], atr_value=1.5)

        asyncio.get_event_loop().run_until_complete(runner._check_bracket_health())

        cancel_mock.assert_called_once_with("order-001")
        submit_mock.assert_called_once()
        submitted: OrderRequest = submit_mock.call_args[0][0]
        assert submitted.stop_price == Decimal("107.0")
        assert submitted.side == OrderSide.SELL

    def test_no_ratchet_when_proposed_lower(self) -> None:
        """Stop does NOT move down: no cancel/submit when proposed < existing."""
        cfg = _make_cfg(atr_mult=2.0)
        # existing_stop=109, proposed=110 - 1.5*2 = 107 → proposed < existing
        pos = _make_position(current_price=110.0)
        stop_order = _make_stop_order(stop_price=109.0)
        runner, cancel_mock, submit_mock = _build_runner(cfg, [pos], [stop_order], atr_value=1.5)

        asyncio.get_event_loop().run_until_complete(runner._check_bracket_health())

        cancel_mock.assert_not_called()
        submit_mock.assert_not_called()

    def test_no_ratchet_when_proposed_equal(self) -> None:
        """Stop does NOT change when proposed == existing."""
        cfg = _make_cfg(atr_mult=2.0)
        # proposed = 110 - 1.5*2 = 107.0 = existing_stop
        pos = _make_position(current_price=110.0)
        stop_order = _make_stop_order(stop_price=107.0)
        runner, cancel_mock, submit_mock = _build_runner(cfg, [pos], [stop_order], atr_value=1.5)

        asyncio.get_event_loop().run_until_complete(runner._check_bracket_health())

        cancel_mock.assert_not_called()
        submit_mock.assert_not_called()

    def test_no_trailing_stop_when_multiplier_absent(self) -> None:
        """No ratchet action when trailing_stop_atr_multiplier is not set."""
        cfg = _make_cfg(atr_mult=None)
        pos = _make_position(current_price=115.0)
        stop_order = _make_stop_order(stop_price=98.0)
        runner, cancel_mock, submit_mock = _build_runner(cfg, [pos], [stop_order], atr_value=1.5)

        asyncio.get_event_loop().run_until_complete(runner._check_bracket_health())

        cancel_mock.assert_not_called()
        submit_mock.assert_not_called()

    def test_no_ratchet_when_no_current_price(self) -> None:
        """Trailing stop skipped if position has no current_price."""
        cfg = _make_cfg(atr_mult=2.0)
        pos = Position(
            symbol="SPY",
            qty=Decimal("10"),
            avg_entry_price=Decimal("100"),
            current_price=None,
        )
        stop_order = _make_stop_order(stop_price=95.0)
        runner, cancel_mock, submit_mock = _build_runner(cfg, [pos], [stop_order], atr_value=1.5)

        asyncio.get_event_loop().run_until_complete(runner._check_bracket_health())

        cancel_mock.assert_not_called()
        submit_mock.assert_not_called()

    def test_cancel_called_once_per_stop_order(self) -> None:
        """Each existing stop order is cancelled exactly once."""
        cfg = _make_cfg(atr_mult=2.0)
        pos = _make_position(current_price=120.0)
        stop_orders = [
            _make_stop_order(stop_price=95.0, broker_id="ord-1"),
            _make_stop_order(stop_price=96.0, broker_id="ord-2"),
        ]
        runner, cancel_mock, submit_mock = _build_runner(
            cfg, [pos], stop_orders, atr_value=1.0
        )
        # proposed = 120 - 1.0*2 = 118 > max(95, 96) → ratchet

        asyncio.get_event_loop().run_until_complete(runner._check_bracket_health())

        assert cancel_mock.call_count == 2
        submit_mock.assert_called_once()
