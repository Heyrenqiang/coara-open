"""EventBus task lifecycle and capacity regression tests."""

from __future__ import annotations

import asyncio

import pytest

from src.coara.event_bus import EventBus
from src.core.events import TraceEvent


def _event() -> TraceEvent:
    return TraceEvent(coara_id="test", coara_name="test", event_type="test", message="test")


@pytest.mark.asyncio
async def test_async_subscribers_are_dropped_at_pending_task_capacity() -> None:
    bus = EventBus()
    bus._MAX_PENDING_TASKS = 1
    started = 0
    release = asyncio.Event()

    async def blocking_subscriber(_event: TraceEvent) -> None:
        nonlocal started
        started += 1
        await release.wait()

    bus.subscribe(blocking_subscriber, topic="test")
    bus.publish(_event())
    await asyncio.sleep(0)
    bus.publish(_event())
    await asyncio.sleep(0)

    assert started == 1
    assert len(bus._pending_tasks) == 1

    release.set()
    await asyncio.sleep(0)
    await bus.shutdown()
