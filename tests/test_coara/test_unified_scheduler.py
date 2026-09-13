"""UnifiedScheduler queue cap and shutdown behavior."""

from __future__ import annotations

import asyncio

import pytest

from src.coara.scheduler import MessagePriority, UnifiedScheduler
from src.core.types import UnifiedMessage


def _msg(task_id: str) -> UnifiedMessage:
    return UnifiedMessage(
        source_id="test",
        target_id="root",
        task_id=task_id,
        content=f"msg-{task_id}",
        msg_type="user_input",
    )


@pytest.mark.asyncio
async def test_scheduler_drops_when_queue_full() -> None:
    scheduler = UnifiedScheduler(max_queue_size=1)
    scheduler.put_nowait(_msg("first"))
    scheduler.put_nowait(_msg("overflow"))
    assert scheduler.queue_size() == 1


@pytest.mark.asyncio
async def test_scheduler_shutdown_drops_new_messages() -> None:
    scheduler = UnifiedScheduler(max_queue_size=4)
    await scheduler.shutdown()
    scheduler.put_nowait(_msg("after-shutdown"))
    assert scheduler.queue_size() == 0
    assert scheduler.is_alive() is False


@pytest.mark.asyncio
async def test_scheduler_shutdown_drains_pending_messages() -> None:
    scheduler = UnifiedScheduler(max_queue_size=8)
    scheduler.put_nowait(_msg("pending-1"))
    scheduler.put_nowait(_msg("pending-2"))

    drained: list[str] = []

    async def consumer() -> None:
        async for msg in scheduler.listen():
            drained.append(msg.task_id)

    consumer_task = asyncio.create_task(consumer())
    await asyncio.sleep(0)
    await scheduler.shutdown()
    await asyncio.wait_for(consumer_task, timeout=2.0)

    assert drained == ["pending-1", "pending-2"]
    assert scheduler.queue_size() == 0


@pytest.mark.asyncio
async def test_scheduler_async_put_drops_after_shutdown() -> None:
    scheduler = UnifiedScheduler(max_queue_size=4)
    await scheduler.shutdown()
    await scheduler.put(_msg("async-after-shutdown"))
    assert scheduler.queue_size() == 0


@pytest.mark.asyncio
async def test_scheduler_async_put_drops_when_queue_full() -> None:
    scheduler = UnifiedScheduler(max_queue_size=1)
    scheduler.put_nowait(_msg("first"))
    await scheduler.put(_msg("async-overflow"))
    assert scheduler.queue_size() == 1


@pytest.mark.asyncio
async def test_scheduler_urgent_preempts_normal() -> None:
    scheduler = UnifiedScheduler(max_queue_size=8)
    scheduler.put_nowait(_msg("normal-1"))
    scheduler.put_nowait(_msg("urgent-1"), priority=MessagePriority.URGENT)

    async def drain() -> list[str]:
        ids: list[str] = []
        async for msg in scheduler.listen():
            ids.append(msg.task_id)
            if len(ids) == 2:
                await scheduler.shutdown()
                break
        return ids

    ids = await asyncio.wait_for(drain(), timeout=2.0)
    assert ids[0] == "urgent-1"
    assert ids[1] == "normal-1"


@pytest.mark.asyncio
async def test_scheduler_listen_yields_both_queues_completed_in_one_batch() -> None:
    """Both queues' get tasks completing in the same wait batch must all be yielded."""
    scheduler = UnifiedScheduler(max_queue_size=8)

    received: list[str] = []

    async def consumer() -> None:
        async for msg in scheduler.listen():
            received.append(msg.task_id)
            if len(received) == 2:
                return

    consumer_task = asyncio.create_task(consumer())
    await asyncio.sleep(0)  # let the consumer block in asyncio.wait
    scheduler.put_nowait(_msg("normal-1"))
    scheduler.put_nowait(_msg("urgent-1"), priority=MessagePriority.URGENT)
    await asyncio.wait_for(consumer_task, timeout=2.0)

    assert received == ["urgent-1", "normal-1"]
    await scheduler.shutdown()


@pytest.mark.asyncio
async def test_scheduler_listen_drains_message_taken_in_shutdown_batch() -> None:
    """A message dequeued by a get task in the same batch as shutdown is not lost."""
    scheduler = UnifiedScheduler(max_queue_size=8)

    received: list[str] = []

    async def consumer() -> None:
        async for msg in scheduler.listen():
            received.append(msg.task_id)

    consumer_task = asyncio.create_task(consumer())
    await asyncio.sleep(0)  # let the consumer block in asyncio.wait
    scheduler.put_nowait(_msg("raced-1"))
    await scheduler.shutdown()
    await asyncio.wait_for(consumer_task, timeout=2.0)

    assert received == ["raced-1"]
    assert scheduler.queue_size() == 0
