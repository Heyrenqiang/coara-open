"""Root-process reminder scheduler with persistence and inbox delivery."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path

from src.core.logger import logger
from src.core.schedule_ids import generate_scheduled_id
from src.core.schedule_utils import compute_next_cron
from src.core.schedule_utils import parse_cron_fields as _parse_cron_fields
from src.core.tick_scheduler import DEFAULT_TICK_SECONDS, DEFAULT_TIMEZONE, TickSchedulerBase
from src.core.time import parse_iso_to_datetime
from src.reminders.store import ReminderStore
from src.reminders.types import ReminderKind, ReminderRecord


def _generate_job_id(prefix: str, message: str) -> str:
    return generate_scheduled_id(prefix, message)


def _duration_seconds(*, minutes: int = 0, hours: int = 0, days: int = 0) -> float:
    return float(minutes * 60 + hours * 3600 + days * 86400)


def format_reminder_fire_text(record: ReminderRecord) -> str:
    """提醒到点的内容正文（用户自己写的提醒文本）。"""
    return record.message.strip() or "提醒"


class ReminderService(TickSchedulerBase[ReminderRecord]):
    """Persisted reminders executed inside the coara main process.

    到点不自动跑回合，只通过 on_fire 回调产出内容（落收件箱，high 显著）。
    """

    def __init__(
        self,
        coara_home: Path,
        *,
        enqueue: Callable,
        coara_id: str,
        timezone: str = DEFAULT_TIMEZONE,
        tick_seconds: float = DEFAULT_TICK_SECONDS,
        on_fire: Callable[[ReminderRecord], Awaitable[None]] | None = None,
    ):
        self._store = ReminderStore(coara_home / "reminders" / "store.json")
        super().__init__(
            enqueue=enqueue,
            coara_id=coara_id,
            timezone=timezone,
            tick_seconds=tick_seconds,
            scheduler_label="ReminderService",
        )
        self._on_fire = on_fire
        self._normalize_dirty = False
        self._init_records(self._store.load())

    def _load_persisted_records(self) -> dict[str, ReminderRecord]:
        return self._store.load()

    def _persist_records(self) -> None:
        self._store.save(self._records)

    def _normalize_loaded_records(self) -> None:
        now = self._now()
        changed = False
        for record in self._records.values():
            if not getattr(record, "delivering", False):
                continue
            # 上次进程在投递中崩溃：清除 delivering 保留记录重发（next_run_at
            # 已到点，首个 tick 视为到期）。收件箱 append 有 dedupe_key
            # （reminder_due:{id}:{next_run_at}）兜底，若崩溃前已写入收件箱
            # 则重发被去重吸收，绝不产生重复条目；与进程内失败重试口径一致。
            record.delivering = False
            changed = True
        for record in self._records.values():
            if not record.enabled:
                continue
            next_run = parse_iso_to_datetime(record.next_run_at)
            if next_run is None:
                continue
            if next_run.tzinfo is None:
                record.next_run_at = next_run.replace(tzinfo=self._tz).isoformat()
                changed = True
            if record.kind == ReminderKind.CRON and record.cron:
                due = parse_iso_to_datetime(record.next_run_at)
                if due is not None and due <= now:
                    record.next_run_at = compute_next_cron(record.cron, self._tz, now).isoformat()
                    changed = True
        if changed:
            # Defer the write to the first tick so startup performs at most
            # one save even when the first tick also fires due records.
            self._normalize_dirty = True

    async def _tick(self) -> None:
        now = self._now()
        due_records: list[ReminderRecord] = []
        async with self._lock:
            for record in self._records.values():
                if not record.enabled:
                    continue
                next_run = parse_iso_to_datetime(record.next_run_at)
                if next_run is None:
                    continue
                if next_run.tzinfo is None:
                    next_run = next_run.replace(tzinfo=self._tz)
                if next_run <= now:
                    due_records.append(record.model_copy(deep=True))
            for record in due_records:
                if record.kind == ReminderKind.ONE_TIME:
                    # 一次性提醒：pop 前先把 delivering 状态落盘，投递成功后才真正移除。
                    # 盘面不存在「已删未投」状态；崩溃重启时 delivering 按已投处理
                    # （丢而不重取舍不变，只消除先删后投的丢失窗口）。
                    stored = self._records.get(record.id)
                    if stored is not None:
                        stored.delivering = True
                elif record.kind == ReminderKind.INTERVAL:
                    stored = self._records.get(record.id)
                    if stored is not None and stored.interval_seconds:
                        stored.next_run_at = (now + timedelta(seconds=stored.interval_seconds)).isoformat()
                elif record.kind == ReminderKind.CRON and record.cron:
                    stored = self._records.get(record.id)
                    if stored is not None:
                        stored.next_run_at = compute_next_cron(stored.cron, self._tz, now).isoformat()
            if due_records or self._normalize_dirty:
                await asyncio.to_thread(self._store.save, self._records)
                self._normalize_dirty = False

        failed_ids: set[str] = set()
        for record in due_records:
            try:
                await self._fire(record)
            except Exception:
                # 投递失败（进程未崩）：保留记录下轮重试，不静默丢弃
                failed_ids.add(record.id)
                logger.exception(f"Reminder fire failed, will retry ({record.id})")

        # 投递成功后再移除一次性提醒并落盘
        fired_ids = [r.id for r in due_records if r.kind == ReminderKind.ONE_TIME and r.id not in failed_ids]
        if fired_ids or failed_ids:
            async with self._lock:
                for record_id in fired_ids:
                    self._records.pop(record_id, None)
                if failed_ids:
                    # 失败的一次性提醒：清除 delivering 保留原记录（next_run_at
                    # 已到点，下个 tick 视为到期重试）；周期类无需处理
                    for record_id in failed_ids:
                        stored = self._records.get(record_id)
                        if stored is not None:
                            stored.delivering = False
                await asyncio.to_thread(self._store.save, self._records)

    async def _fire(self, record: ReminderRecord) -> None:
        if self._on_fire is not None:
            # 投递异常向上抛：调用方据此保留记录重试（不让 on_fire 内部
            # 的瞬时故障演变为一次性提醒的静默丢失）
            await self._on_fire(record)
        logger.info(f"Reminder fired: {record.id}")

    async def add_one_time_reminder(
        self,
        message: str = "提醒",
        *,
        minutes: int = 0,
        hours: int = 0,
        days: int = 0,
    ) -> str:
        delay = _duration_seconds(minutes=minutes, hours=hours, days=days)
        if delay <= 0:
            raise ValueError("一次性提醒至少需要指定 minutes、hours 或 days 之一且大于 0")
        run_at = self._now() + timedelta(seconds=delay)
        job_id = _generate_job_id("once", message)
        record = ReminderRecord(
            id=job_id,
            kind=ReminderKind.ONE_TIME,
            message=message,
            next_run_at=run_at.isoformat(),
        )
        async with self._lock:
            self._records[job_id] = record
            await asyncio.to_thread(self._store.save, self._records)
        return f"一次性提醒已创建，ID: {job_id}，执行时间: {run_at.strftime('%Y-%m-%d %H:%M:%S %Z')}"

    async def add_interval_reminder(
        self,
        message: str = "提醒",
        *,
        minutes: int = 0,
        hours: int = 0,
        days: int = 0,
    ) -> str:
        interval = _duration_seconds(minutes=minutes, hours=hours, days=days)
        if interval <= 0:
            raise ValueError("周期性提醒至少需要指定 minutes、hours 或 days 之一且大于 0")
        run_at = self._now() + timedelta(seconds=interval)
        job_id = _generate_job_id("interval", message)
        record = ReminderRecord(
            id=job_id,
            kind=ReminderKind.INTERVAL,
            message=message,
            next_run_at=run_at.isoformat(),
            interval_seconds=interval,
        )
        async with self._lock:
            self._records[job_id] = record
            await asyncio.to_thread(self._store.save, self._records)
        return f"周期性提醒已创建，ID: {job_id}，间隔: {int(interval)} 秒"

    async def add_cron_reminder(self, cron: str, message: str = "提醒") -> str:
        _parse_cron_fields(cron)
        run_at = compute_next_cron(cron, self._tz)
        job_id = _generate_job_id("cron", message)
        record = ReminderRecord(
            id=job_id,
            kind=ReminderKind.CRON,
            message=message,
            next_run_at=run_at.isoformat(),
            cron=cron.strip(),
        )
        async with self._lock:
            self._records[job_id] = record
            await asyncio.to_thread(self._store.save, self._records)
        return f"Cron 提醒已创建，ID: {job_id}，下次执行: {run_at.strftime('%Y-%m-%d %H:%M:%S %Z')}"

    async def list_reminders(self) -> list[dict[str, object]]:
        async with self._lock:
            return [record.to_public_dict() for record in self._records.values()]

    async def remove_reminder(self, job_id: str) -> str:
        async with self._lock:
            if job_id not in self._records:
                return f"任务 {job_id} 不存在"
            del self._records[job_id]
            await asyncio.to_thread(self._store.save, self._records)
        return f"任务 {job_id} 已取消"

    async def set_reminder_enabled(self, job_id: str, enabled: bool) -> str:
        """Toggle a reminder's enabled flag (内存即时生效 + 落盘)."""
        rid = job_id.strip()
        async with self._lock:
            record = self._records.get(rid)
            if record is None:
                raise ValueError(f"找不到 Reminder: {rid}")
            self._records[rid] = record.model_copy(update={"enabled": enabled})
            await asyncio.to_thread(self._store.save, self._records)
        return f"已 {'启用' if enabled else '停用'} Reminder: {rid}"
