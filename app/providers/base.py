"""BrokerProvider — abstract base class for all broker integrations.

Implementations live in app/providers/<name>/provider.py.
No broker-specific type may appear outside app/providers/<name>/.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from datetime import datetime

    from app.core.domain import (
        Account,
        Candle,
        Fill,
        Instrument,
        OrderAck,
        OrderRequest,
        Position,
    )
    from app.providers.capabilities import BrokerCapabilities


class BrokerProvider(ABC):
    """Interface neutre broker. Toute implémentation doit être dans app/providers/<name>/."""

    name: str
    capabilities: BrokerCapabilities

    @abstractmethod
    async def connect(self) -> None:
        """Establish connection and authenticate with the broker."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Gracefully disconnect from the broker."""

    @abstractmethod
    async def get_account(self) -> Account:
        """Return current account summary."""

    @abstractmethod
    async def get_positions(self) -> list[Position]:
        """Return all current open positions."""

    @abstractmethod
    async def get_position(self, symbol: str) -> Position | None:
        """Return the current position for a symbol, or None if flat."""

    @abstractmethod
    async def submit_order(self, order: OrderRequest) -> OrderAck:
        """Submit an order to the broker and return the acknowledgement."""

    @abstractmethod
    async def cancel_order(self, broker_order_id: str) -> None:
        """Cancel an order by broker order ID."""

    @abstractmethod
    async def get_order(self, broker_order_id: str) -> OrderAck:
        """Retrieve the current state of an order."""

    @abstractmethod
    async def list_open_orders(self, symbol: str | None = None) -> list[OrderAck]:
        """Return all pending/open orders, optionally filtered by symbol."""

    @abstractmethod
    async def list_closed_orders(
        self,
        since: datetime,
        symbol: str | None = None,
    ) -> list[OrderAck]:
        """Return filled/cancelled orders since the given timestamp.

        Used for startup reconciliation: any fills that arrived while the
        process was down are backfilled from this endpoint.
        """

    @abstractmethod
    async def get_historical_candles(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> list[Candle]:
        """Fetch historical OHLCV bars for a symbol."""

    @abstractmethod
    async def stream_fills(self) -> AsyncIterator[Fill]:
        """Yield fill events as they arrive from the broker stream."""

    @abstractmethod
    async def stream_candles(
        self,
        symbols: list[str],
        timeframe: str,
    ) -> AsyncIterator[Candle]:
        """Yield candle close events from the broker market data stream."""

    @abstractmethod
    async def list_tradable_instruments(self) -> list[Instrument]:
        """Return all instruments tradable on this broker for the current account."""

    @abstractmethod
    async def healthcheck(self) -> bool:
        """Return True if the broker connection is healthy."""
