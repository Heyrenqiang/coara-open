"""Per-connection bounded WS outbox (server → client broadcast backpressure).

TurnStream and similar sync emitters used to ``ensure_future`` one send per frame.
A slow browser then piled unbounded tasks on the event loop. This outbox keeps
exactly one drain task per connection, bounds the pending queue, drops the oldest
frame when full, and emits ``need_topup`` so the client can heal via tape hydrate
(``after_view_seq`` / seq−50) — same path as reconnect.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from src.core.logger import logger

# Pending frames per connection. Full → drop oldest + mark need_topup.
DEFAULT_OUTBOX_MAX = 256

NEED_TOPUP_TYPE = "need_topup"

SendFn = Callable[[dict[str, Any]], Awaitable[bool]]


@dataclass(slots=True)
class _ConnOutbox:
    queue: deque[dict[str, Any]] = field(default_factory=deque)
    gap: bool = False
    draining: bool = False
    task: asyncio.Task[None] | None = None


class WsOutbox:
    """Bounded send queue keyed by connection id."""

    def __init__(self, *, max_size: int = DEFAULT_OUTBOX_MAX) -> None:
        self._max = max(1, int(max_size))
        self._conns: dict[str, _ConnOutbox] = {}

    def clear(self, conn_id: str) -> None:
        """Drop queue and cancel drain for a disconnected connection."""
        state = self._conns.pop(conn_id, None)
        if state is None:
            return
        if state.task is not None and not state.task.done():
            state.task.cancel()

    def enqueue(self, conn_id: str, message: dict[str, Any], send: SendFn) -> None:
        """Queue ``message`` for ``conn_id``; start drain if idle."""
        state = self._conns.get(conn_id)
        if state is None:
            state = _ConnOutbox()
            self._conns[conn_id] = state
        if len(state.queue) >= self._max:
            dropped = state.queue.popleft()
            state.gap = True
            logger.debug(f"WS outbox full ({conn_id}): drop oldest type={dropped.get('type')!r}; need_topup")
        state.queue.append(message)
        self._ensure_drain(conn_id, send)

    def pending(self, conn_id: str) -> int:
        """Queued frame count (tests / diagnostics)."""
        state = self._conns.get(conn_id)
        return 0 if state is None else len(state.queue)

    def has_gap(self, conn_id: str) -> bool:
        state = self._conns.get(conn_id)
        return bool(state and state.gap)

    def _ensure_drain(self, conn_id: str, send: SendFn) -> None:
        state = self._conns.get(conn_id)
        if state is None or state.draining:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug(f"WS outbox: no running loop; drop enqueue for {conn_id}")
            self._conns.pop(conn_id, None)
            return
        state.draining = True
        state.task = loop.create_task(self._drain(conn_id, send))

    async def _drain(self, conn_id: str, send: SendFn) -> None:
        state = self._conns.get(conn_id)
        if state is None:
            return
        try:
            while True:
                if state.queue:
                    msg = state.queue.popleft()
                    ok = await send(msg)
                    if not ok:
                        self.clear(conn_id)
                        return
                    continue
                if state.gap:
                    # Coalesce: one control frame after the queue drains.
                    state.gap = False
                    ok = await send({"type": NEED_TOPUP_TYPE})
                    if not ok:
                        self.clear(conn_id)
                        return
                    continue
                break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug(f"WS outbox drain failed ({conn_id}): {exc}")
        finally:
            state = self._conns.get(conn_id)
            if state is not None:
                state.draining = False
                state.task = None
                if state.queue or state.gap:
                    self._ensure_drain(conn_id, send)
