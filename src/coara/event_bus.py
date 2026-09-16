"""Event Bus: multi-subscriber event dispatch with topic filtering."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from src.core.events import TraceEvent
from src.core.logger import logger

# --- Subscriber Types ---

SyncSubscriber = Callable[[TraceEvent], None]
AsyncSubscriber = Callable[[TraceEvent], Any]  # Any because coroutine


# --- Subscription Handle ---


class Subscription:
    """Unsubscribe token returned by EventBus.subscribe()."""

    def __init__(self, bus: EventBus, subscriber_id: str, topic: str | None):
        self._bus = bus
        self._subscriber_id = subscriber_id
        self._topic = topic

    def unsubscribe(self) -> None:
        self._bus._remove_subscription(self._subscriber_id, self._topic)


# --- Event Bus ---


class EventBus:
    """Multi-subscriber event dispatch with topic filtering.

    Usage:
        bus = EventBus()

        # Subscribe to specific event types
        bus.subscribe(on_tool_call, topic="tool_call")

        # Subscribe to all events (wildcard)
        bus.subscribe(on_any_event, topic=None)

        # Publish events to matching subscribers
        bus.publish(event)
    """

    # Hard cap on tracked pending tasks to prevent unbounded growth if
    # async subscribers hang or the event loop is saturated.
    _MAX_PENDING_TASKS = 256

    def __init__(self) -> None:
        # topic -> list of subscribers (None = wildcard)
        self._sync_subscribers: dict[str | None, list[tuple[str, SyncSubscriber]]] = defaultdict(list)
        self._async_subscribers: dict[str | None, list[tuple[str, AsyncSubscriber]]] = defaultdict(list)
        self._counter = 0
        # Track async tasks to prevent silent exception loss
        self._pending_tasks: set[asyncio.Task] = set()
        # Tasks beyond _MAX_PENDING_TASKS still run but live here so shutdown()
        # can cancel/await them instead of losing track of them entirely.
        self._overflow_tasks: set[asyncio.Task] = set()

    # --- Core API ---

    def publish(self, event: TraceEvent) -> None:
        """Publish event to all matching subscribers.

        Sync subscribers are called immediately (in publish call stack).
        Async subscribers are scheduled via asyncio.create_task().
        """
        topic = event.event_type

        # 1. Topic-specific sync subscribers
        for sub_id, sub in self._sync_subscribers.get(topic, []):
            try:
                sub(event)
            except Exception as exc:
                logger.warning(f"EventBus: sync subscriber '{sub_id}' failed for event '{topic}': {exc}")

        # 2. Wildcard sync subscribers
        for sub_id, sub in self._sync_subscribers.get(None, []):
            try:
                sub(event)
            except Exception as exc:
                logger.warning(f"EventBus: sync subscriber '{sub_id}' failed for event '{topic}': {exc}")

        # 3. Topic-specific async subscribers
        if len(self._pending_tasks) > self._MAX_PENDING_TASKS // 2:
            self._cleanup_pending_tasks()
        for sub_id, sub in self._async_subscribers.get(topic, []):
            try:
                task = asyncio.create_task(self._safe_async_call(sub, event, sub_id, topic))
                if len(self._pending_tasks) < self._MAX_PENDING_TASKS:
                    self._pending_tasks.add(task)
                    task.add_done_callback(self._pending_tasks.discard)
                else:
                    self._overflow_tasks.add(task)
                    task.add_done_callback(self._overflow_tasks.discard)
                    logger.warning(
                        f"EventBus: pending task cap ({self._MAX_PENDING_TASKS}) reached; "
                        f"async subscriber '{sub_id}' for event '{topic}' tracked in overflow set"
                    )
            except RuntimeError:
                logger.warning(
                    f"EventBus: no running event loop, async subscriber '{sub_id}' for event '{topic}' skipped"
                )
            except Exception as exc:
                logger.warning(f"EventBus: failed to schedule async subscriber '{sub_id}' for event '{topic}': {exc}")

        # 4. Wildcard async subscribers
        for sub_id, sub in self._async_subscribers.get(None, []):
            try:
                task = asyncio.create_task(self._safe_async_call(sub, event, sub_id, topic))
                if len(self._pending_tasks) < self._MAX_PENDING_TASKS:
                    self._pending_tasks.add(task)
                    task.add_done_callback(self._pending_tasks.discard)
                else:
                    self._overflow_tasks.add(task)
                    task.add_done_callback(self._overflow_tasks.discard)
                    logger.warning(
                        f"EventBus: pending task cap ({self._MAX_PENDING_TASKS}) reached; "
                        f"async subscriber '{sub_id}' for event '{topic}' tracked in overflow set"
                    )
            except RuntimeError:
                logger.warning(
                    f"EventBus: no running event loop, async subscriber '{sub_id}' for event '{topic}' skipped"
                )
            except Exception as exc:
                logger.warning(f"EventBus: failed to schedule async subscriber '{sub_id}' for event '{topic}': {exc}")

    def subscribe(
        self,
        callback: SyncSubscriber | AsyncSubscriber,
        topic: str | None = None,
    ) -> Subscription:
        """Subscribe to events. topic=None means all events.

        Returns a Subscription handle for unsubscription.
        """
        self._counter += 1
        subscriber_id = f"sub_{self._counter}"

        if asyncio.iscoroutinefunction(callback):
            self._async_subscribers[topic].append((subscriber_id, callback))
        else:
            self._sync_subscribers[topic].append((subscriber_id, callback))

        return Subscription(bus=self, subscriber_id=subscriber_id, topic=topic)

    def _remove_subscription(self, subscriber_id: str, topic: str | None) -> None:
        """Remove subscription by subscriber_id; empty topic buckets are dropped."""
        for store in (self._sync_subscribers, self._async_subscribers):
            if topic in store:
                remaining = [(sid, cb) for sid, cb in store[topic] if sid != subscriber_id]
                if remaining:
                    store[topic] = remaining
                else:
                    del store[topic]

    @staticmethod
    async def _safe_async_call(
        fn: AsyncSubscriber,
        event: TraceEvent,
        sub_id: str,
        topic: str,
    ) -> None:
        try:
            await fn(event)
        except Exception as exc:
            logger.warning(f"EventBus: async subscriber '{sub_id}' failed for event '{topic}': {exc}")

    def _cleanup_pending_tasks(self) -> None:
        """Remove completed tasks from the pending set to prevent unbounded growth."""
        done_tasks = {task for task in self._pending_tasks if task.done()}
        self._pending_tasks.difference_update(done_tasks)

    async def shutdown(self) -> None:
        """Cancel all tracked pending async-subscriber tasks and await them."""
        pending = [task for task in self._pending_tasks if not task.done()]
        pending += [task for task in self._overflow_tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._pending_tasks.clear()
        self._overflow_tasks.clear()
