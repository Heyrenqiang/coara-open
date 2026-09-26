"""Cron event source: fires an InboundEvent on a cron schedule.

第四类事件源——「什么时候」的定时钟。到点产生一条普通事件，
落收件箱 / 触发工作流 / 交 janitor 处置，全部走统一事件管道。
进程停机期间错过的触发不补发（reload / 重启后从当前时间重算下一次）。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from src.core.logger import logger
from src.core.schedule_utils import compute_next_cron
from src.core.tick_scheduler import DEFAULT_TIMEZONE
from src.event_sources.types import EventSourceDefinition, InboundEvent


class CronSource:
    """Single cron schedule → event emitter (one asyncio task per source)."""

    def __init__(
        self,
        defn: EventSourceDefinition,
        *,
        emit: Callable[[InboundEvent], Awaitable[None]],
        timezone: str = DEFAULT_TIMEZONE,
    ) -> None:
        if not defn.cron.strip():
            raise ValueError(f"Cron source '{defn.id}' 缺少 cron 表达式")
        # 构造即校验表达式，非法表达式 fail-fast
        self._tz = ZoneInfo(timezone)
        compute_next_cron(defn.cron, self._tz)
        self._defn = defn
        self._emit = emit
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"cron-source-{self._defn.id}")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _run(self) -> None:
        while True:
            next_fire = compute_next_cron(self._defn.cron, self._tz)
            delay = max(0.0, (next_fire - datetime.now(self._tz)).total_seconds())
            await asyncio.sleep(delay)
            fired_at = datetime.now(self._tz)
            event = InboundEvent(
                source_id=self._defn.id,
                workspace=self._defn.workspace,
                event_type="cron.tick",
                payload={
                    "cron": self._defn.cron,
                    "fired_at": fired_at.isoformat(timespec="seconds"),
                },
                # 同一分钟只发一次：reload 双开窗口的重复触发由去重吸收
                dedupe_key=f"cron:{self._defn.id}:{fired_at.strftime('%Y%m%d%H%M')}",
                occurred_at=fired_at.replace(tzinfo=None).isoformat(),
            )
            try:
                await self._emit(event)
            except Exception as exc:
                logger.warning(f"Cron source '{self._defn.id}' emit failed: {exc}")
