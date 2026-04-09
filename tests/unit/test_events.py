"""Tests for the asyncio event bus."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from app.core.events import Event, EventBus, Topics


def _event(topic: str = "test.topic", source: str = "test") -> Event:
    return Event(
        topic=topic,
        payload={"key": "value"},
        timestamp=datetime(2026, 4, 9, 12, 0, 0, tzinfo=timezone.utc),
        source=source,
    )


@pytest.mark.asyncio
async def test_publish_and_receive() -> None:
    bus = EventBus()
    received: list[Event] = []

    async def consume() -> None:
        async for evt in bus.subscribe():
            received.append(evt)
            break  # receive exactly one

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)  # let consumer start
    await bus.publish(_event())
    await asyncio.wait_for(task, timeout=1.0)

    assert len(received) == 1
    assert received[0].topic == "test.topic"


@pytest.mark.asyncio
async def test_topic_filter() -> None:
    """Subscriber for topic A must not receive topic B events."""
    bus = EventBus()
    received: list[Event] = []

    async def consume() -> None:
        async for evt in bus.subscribe(topics={"candle.closed"}):
            received.append(evt)
            break

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)

    await bus.publish(_event(topic="signal.generated"))    # should be filtered
    await bus.publish(_event(topic="candle.closed"))       # should arrive

    await asyncio.wait_for(task, timeout=1.0)
    assert len(received) == 1
    assert received[0].topic == "candle.closed"


@pytest.mark.asyncio
async def test_multiple_subscribers() -> None:
    bus = EventBus()
    results: dict[str, list[Event]] = {"a": [], "b": []}

    async def consume_a() -> None:
        async for evt in bus.subscribe():
            results["a"].append(evt)
            break

    async def consume_b() -> None:
        async for evt in bus.subscribe():
            results["b"].append(evt)
            break

    task_a = asyncio.create_task(consume_a())
    task_b = asyncio.create_task(consume_b())
    await asyncio.sleep(0)

    await bus.publish(_event())

    await asyncio.wait_for(task_a, timeout=1.0)
    await asyncio.wait_for(task_b, timeout=1.0)

    assert len(results["a"]) == 1
    assert len(results["b"]) == 1


@pytest.mark.asyncio
async def test_shutdown_stops_all_subscribers() -> None:
    bus = EventBus()
    stopped = asyncio.Event()

    async def consume() -> None:
        async for _ in bus.subscribe():
            pass
        stopped.set()

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    await bus.shutdown()
    await asyncio.wait_for(task, timeout=1.0)

    assert stopped.is_set()


def test_event_topics_constants() -> None:
    """Ensure topic constants are non-empty strings."""
    for attr in dir(Topics):
        if not attr.startswith("_"):
            value = getattr(Topics, attr)
            assert isinstance(value, str) and value
