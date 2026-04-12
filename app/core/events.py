"""Internal event bus — asyncio.Queue-based pub/sub.

Subscribers receive events by type. The bus buffers events so that a slow
subscriber does not block publishers (bounded queue per subscriber).
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@dataclass
class Event:
    """Base event type published on the bus."""

    topic: str                          # e.g. "candle.closed", "signal.generated"
    payload: dict[str, Any]
    timestamp: datetime = field(default_factory=datetime.utcnow)
    source: str = "system"              # e.g. strategy name, "market_data"


class EventBus:
    """Simple asyncio pub/sub event bus.

    Publishers call publish(event). Subscribers call subscribe(topics) and
    iterate over the returned async generator. Each subscriber gets its own
    queue so slow consumers don't affect others.

    Buffer size is configurable (default 1000) to absorb bursts.
    """

    def __init__(self, buffer_size: int = 1_000) -> None:
        self._buffer_size = buffer_size
        self._subscribers: list[tuple[set[str], asyncio.Queue[Event | None]]] = []

    async def publish(self, event: Event) -> None:
        """Publish an event to all matching subscribers (non-blocking)."""
        for topics, queue in self._subscribers:
            if not topics or event.topic in topics:
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    # Drop the oldest item to make room (prefer freshness)
                    with contextlib.suppress(asyncio.QueueEmpty):
                        queue.get_nowait()
                    queue.put_nowait(event)

    def subscribe(
        self, topics: set[str] | None = None
    ) -> AsyncIterator[Event]:
        """Return an async iterator that yields events for the given topics.

        Pass topics=None to receive all events.
        """
        queue: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=self._buffer_size)
        filter_topics: set[str] = topics or set()
        self._subscribers.append((filter_topics, queue))

        async def _iter() -> AsyncIterator[Event]:
            try:
                while True:
                    item = await queue.get()
                    if item is None:
                        break
                    yield item
            finally:
                self._subscribers.remove((filter_topics, queue))

        return _iter()

    async def shutdown(self) -> None:
        """Signal all subscribers to stop."""
        for _, queue in self._subscribers:
            await queue.put(None)


# Common event topics
class Topics:
    CANDLE_CLOSED = "candle.closed"
    SIGNAL_GENERATED = "signal.generated"
    ORDER_SUBMITTED = "order.submitted"
    ORDER_FILLED = "order.filled"
    ORDER_REJECTED = "order.rejected"
    POSITION_UPDATED = "position.updated"
    KILL_SWITCH_ENGAGED = "kill_switch.engaged"
    KILL_SWITCH_RELEASED = "kill_switch.released"
    STRATEGY_STARTED = "strategy.started"
    STRATEGY_STOPPED = "strategy.stopped"
    RISK_EVENT = "risk.event"
    BROKER_CONNECTED = "broker.connected"
    BROKER_DISCONNECTED = "broker.disconnected"
    RECONCILIATION_MISMATCH = "reconciliation.mismatch"
