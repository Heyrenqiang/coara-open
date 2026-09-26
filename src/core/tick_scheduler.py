"""Shared lifecycle for in-process tick schedulers (reminders)."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import datetime
from typing import Generic, TypeVar
from zoneinfo import ZoneInfo

from src.core.logger import logger
from src.core.tick_loop import TickLoop
from src.core.types import UnifiedMessage

TRecord = TypeVar("TRecord")

DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_TICK_SECONDS = 1.0


class TickSchedulerBase(ABC, Generic[TRecord]):
    """Persisted record scheduler: load → tick loop → save on stop."""

    def __init__(
        self,
        *,
        enqueue: Callable[[UnifiedMessage, str], None],
        coara_id: str,
        timezone: str = DEFAULT_TIMEZONE,
        tick_seconds: float = DEFAULT_TICK_SECONDS,
        scheduler_label: str = "TickScheduler",
    ) -> None:
        self._enqueue = enqueue
        self._coara_id = coara_id
        self._tz = ZoneInfo(timezone)
        self._tick_seconds = tick_seconds
        self._scheduler_label = scheduler_label
        self._records: dict[str, TRecord] = {}
        self._lock = asyncio.Lock()
        self._tick_loop = TickLoop(tick_seconds, label=scheduler_label)

    @property
    def timezone(self) -> str:
        return str(self._tz)

    def _init_records(self, records: dict[str, TRecord]) -> None:
        """Bind in-memory records after construction; subclasses may normalize."""
        self._records = records
        self._on_records_loaded()

    def _on_records_loaded(self) -> None:
        """Hook after records are loaded (override for eager normalization)."""

    @abstractmethod
    def _load_persisted_records(self) -> dict[str, TRecord]: ...

    @abstractmethod
    def _persist_records(self) -> None: ...

    @abstractmethod
    def _normalize_loaded_records(self) -> None: ...

    @abstractmethod
    async def _tick(self) -> None: ...

    async def start(self) -> None:
        async with self._lock:
            self._records = self._load_persisted_records()
            self._normalize_loaded_records()
            if self._tick_loop.is_running:
                return
            await self._tick_loop.start(self._tick)
        logger.info(f"{self._scheduler_label} started ({len(self._records)} scheduled)")

    async def stop(self) -> None:
        await self._tick_loop.stop()
        async with self._lock:
            self._persist_records()

    def _now(self) -> datetime:
        return datetime.now(self._tz)
