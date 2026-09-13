"""Unified FIFO message scheduler for Root Coara CEO."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from src.core.logger import logger
from src.core.types import UnifiedMessage


class MessagePriority:
    URGENT = "urgent"
    NORMAL = "normal"


_MAX_QUEUE_SIZE = 512


class UnifiedScheduler:
    """CEO 的唯一收件篮子。严格 FIFO，支持安全中断。

    只接受 UnifiedMessage。
    """

    def __init__(self, *, max_queue_size: int = _MAX_QUEUE_SIZE):
        self._max_queue_size = max_queue_size
        self._urgent_queue: asyncio.Queue[UnifiedMessage] = asyncio.Queue(maxsize=max_queue_size)
        self._normal_queue: asyncio.Queue[UnifiedMessage] = asyncio.Queue(maxsize=max_queue_size)
        self._shutdown_event = asyncio.Event()

    def put_nowait(self, msg: UnifiedMessage, *, priority: str = MessagePriority.NORMAL) -> None:
        if self._shutdown_event.is_set():
            task_id = getattr(msg, "task_id", None) or "unknown"
            logger.warning(f"Scheduler is shutting down, dropping msg: {task_id}")
            return
        queue = self._urgent_queue if priority == MessagePriority.URGENT else self._normal_queue
        try:
            queue.put_nowait(msg)
        except asyncio.QueueFull:
            task_id = getattr(msg, "task_id", None) or "unknown"
            logger.warning(f"Scheduler queue full (max={self._max_queue_size}), dropping msg: {task_id}")

    async def put(self, msg: UnifiedMessage, *, priority: str = MessagePriority.NORMAL) -> None:
        if self._shutdown_event.is_set():
            task_id = getattr(msg, "task_id", None) or "unknown"
            logger.warning(f"Scheduler is shutting down, dropping msg: {task_id}")
            return
        queue = self._urgent_queue if priority == MessagePriority.URGENT else self._normal_queue
        if queue.full():
            task_id = getattr(msg, "task_id", None) or "unknown"
            logger.warning(f"Scheduler queue full (max={self._max_queue_size}), dropping msg: {task_id}")
            return
        await queue.put(msg)

    async def listen(self) -> AsyncIterator[UnifiedMessage]:
        while not self._shutdown_event.is_set():
            msg = self._urgent_queue.get_nowait() if not self._urgent_queue.empty() else None
            source_queue = self._urgent_queue if msg is not None else None
            if msg is None and not self._normal_queue.empty():
                msg = self._normal_queue.get_nowait()
                source_queue = self._normal_queue
            if msg is not None:
                if source_queue is not None:
                    source_queue.task_done()
                yield msg
                continue

            urgent_get = asyncio.create_task(self._urgent_queue.get())
            normal_get = asyncio.create_task(self._normal_queue.get())
            shutdown_task = asyncio.create_task(self._shutdown_event.wait())
            done, pending = await asyncio.wait(
                {urgent_get, normal_get, shutdown_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()

            # A completed get task has already dequeued its message, so every
            # completed result must be yielded — dropping one would lose it.
            # Iterate (urgent, normal) so urgent results preempt normal ones.
            batch: list[UnifiedMessage] = []
            for t in (urgent_get, normal_get):
                if t not in done:
                    continue
                try:
                    result = t.result()
                except asyncio.CancelledError:
                    continue
                except Exception as exc:
                    logger.warning(f"Scheduler task error: {exc}")
                    continue
                if isinstance(result, UnifiedMessage):
                    if t is urgent_get:
                        self._urgent_queue.task_done()
                    else:
                        self._normal_queue.task_done()
                    batch.append(result)

            if self._shutdown_event.is_set():
                # Messages taken by get tasks that completed in the same batch
                # as shutdown are no longer in the queues — yield them first.
                for result in batch:
                    yield result
                # 被取消的 get 任务也可能已完成出队（cancel 落在 get 返回之后）：
                # 结果不在队列里，必须取出补投，否则消息凭空丢失
                for t in (urgent_get, normal_get):
                    if t in done:
                        continue
                    try:
                        result = t.result()
                    except (asyncio.CancelledError, Exception):
                        # 未完成或被取消：正常路径；个别已出队的情况在 shutdown 分支单独补投
                        continue
                    if isinstance(result, UnifiedMessage):
                        if t is urgent_get:
                            self._urgent_queue.task_done()
                        else:
                            self._normal_queue.task_done()
                        yield result
                for q in (self._urgent_queue, self._normal_queue):
                    while not q.empty():
                        try:
                            yield q.get_nowait()
                            q.task_done()
                        except asyncio.QueueEmpty:
                            break
                break

            for result in batch:
                yield result

    async def shutdown(self) -> None:
        logger.info("Scheduler shutdown triggered")
        self._shutdown_event.set()

    def is_alive(self) -> bool:
        return not self._shutdown_event.is_set()

    def queue_size(self) -> int:
        return self._urgent_queue.qsize() + self._normal_queue.qsize()
