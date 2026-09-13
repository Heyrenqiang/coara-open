"""Shared asyncio tick loop for in-process schedulers (reminders)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

from src.core.logger import logger


class TickLoop:
    """Runs ``tick()`` on a fixed interval until ``stop()``."""

    def __init__(self, tick_seconds: float = 1.0, *, label: str = "TickLoop") -> None:
        self._tick_seconds = tick_seconds
        self._label = label
        self._task: asyncio.Task[None] | None = None
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self, tick: Callable[[], Awaitable[None]]) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(tick))

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None

    async def _run(self, tick: Callable[[], Awaitable[None]]) -> None:
        while self._running:
            try:
                await tick()
            except asyncio.CancelledError:
                # 外部取消（task.cancel）不应被当作 tick 异常吞掉：置 _running
                # 为 False 让 is_running 不再谎报，然后原样上抛取消
                self._running = False
                raise
            except Exception as exc:
                logger.warning(f"{self._label} tick error: {exc}")
            try:
                await asyncio.sleep(self._tick_seconds)
            except asyncio.CancelledError:
                break
