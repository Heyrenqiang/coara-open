from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.core.schedule_utils import compute_next_cron
from src.core.types import UnifiedMessage
from src.reminders.service import ReminderService
from src.reminders.types import ReminderKind, ReminderRecord


@pytest.mark.asyncio
async def test_add_one_time_reminder_persists(tmp_path):
    enqueued: list[UnifiedMessage] = []

    def enqueue(msg: UnifiedMessage, priority: str) -> None:
        enqueued.append(msg)

    service = ReminderService(
        tmp_path,
        enqueue=enqueue,
        coara_id="root-test",
        timezone="Asia/Shanghai",
        tick_seconds=0.05,
    )
    await service.start()
    try:
        text = await service.add_one_time_reminder("喝水", minutes=30)
        assert "一次性提醒已创建" in text
        rows = await service.list_reminders()
        assert len(rows) == 1
        assert rows[0]["message"] == "喝水"
        assert (tmp_path / "reminders" / "store.json").exists()
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_due_reminder_fires_on_fire_callback(tmp_path):
    fired: list[ReminderRecord] = []

    async def on_fire(record: ReminderRecord) -> None:
        fired.append(record)

    service = ReminderService(
        tmp_path,
        enqueue=lambda *_: None,
        coara_id="root-test",
        timezone="UTC",
        tick_seconds=0.01,
        on_fire=on_fire,
    )
    tz = ZoneInfo("UTC")
    past = datetime.now(tz) - timedelta(seconds=1)
    service._records["once_test_1"] = ReminderRecord(
        id="once_test_1",
        kind=ReminderKind.ONE_TIME,
        message="time to stretch",
        next_run_at=past.isoformat(),
    )
    service._store.save(service._records)
    await service.start()
    try:
        await asyncio.sleep(0.05)
        assert len(fired) == 1
        assert fired[0].message == "time to stretch"
        assert await service.list_reminders() == []
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_interval_reminder_reschedules(tmp_path):
    fired: list[ReminderRecord] = []

    async def on_fire(record: ReminderRecord) -> None:
        fired.append(record)

    service = ReminderService(
        tmp_path,
        enqueue=lambda *_: None,
        coara_id="root-test",
        timezone="UTC",
        tick_seconds=0.01,
        on_fire=on_fire,
    )
    tz = ZoneInfo("UTC")
    past = datetime.now(tz) - timedelta(seconds=1)
    service._records["interval_test_1"] = ReminderRecord(
        id="interval_test_1",
        kind=ReminderKind.INTERVAL,
        message="ping",
        next_run_at=past.isoformat(),
        interval_seconds=3600,
    )
    service._store.save(service._records)
    await service.start()
    try:
        await asyncio.sleep(0.05)
        assert len(fired) == 1
        rows = await service.list_reminders()
        assert len(rows) == 1
        assert rows[0]["id"] == "interval_test_1"
    finally:
        await service.stop()


def test_compute_next_cron_validates_fields():
    with pytest.raises(ValueError):
        compute_next_cron("0 8 * *", ZoneInfo("UTC"))


@pytest.mark.asyncio
async def test_add_without_start_preserves_existing_records(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    existing = ReminderService(home, enqueue=lambda *_: None, coara_id="seed")
    await existing.start()
    await existing.add_one_time_reminder("existing", minutes=30)
    await existing.stop()

    fresh = ReminderService(home, enqueue=lambda *_: None, coara_id="cli")
    await fresh.add_one_time_reminder("new", minutes=10)
    rows = await fresh.list_reminders()
    assert len(rows) == 2
    texts = {row["message"] for row in rows}
    assert texts == {"existing", "new"}


@pytest.mark.asyncio
async def test_set_reminder_enabled_toggles_memory_and_persists(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    service = ReminderService(home, enqueue=lambda *_: None, coara_id="root-test")
    await service.add_one_time_reminder("喝水", minutes=30)
    rows = await service.list_reminders()
    rid = rows[0]["id"]
    assert rows[0]["enabled"] is True

    msg = await service.set_reminder_enabled(rid, False)
    assert "停用" in msg
    rows = await service.list_reminders()
    assert rows[0]["enabled"] is False

    fresh = ReminderService(home, enqueue=lambda *_: None, coara_id="cli")
    rows = await fresh.list_reminders()
    assert rows[0]["enabled"] is False

    with pytest.raises(ValueError, match="找不到"):
        await service.set_reminder_enabled("missing-id", True)


@pytest.mark.asyncio
async def test_remove_reminder(tmp_path):
    service = ReminderService(
        tmp_path,
        enqueue=lambda *_args: None,
        coara_id="root-test",
    )
    await service.add_one_time_reminder("x", minutes=1)
    rows = await service.list_reminders()
    job_id = rows[0]["id"]
    text = await service.remove_reminder(job_id)
    assert "已取消" in text
    assert await service.list_reminders() == []


@pytest.mark.asyncio
async def test_one_time_reminder_delivering_then_removed_after_fire(tmp_path):
    """一次性提醒：先持久化 delivering 再投递；投递成功后移除并落盘。"""
    fired: list[ReminderRecord] = []

    async def on_fire(record: ReminderRecord) -> None:
        # 投递发生时，盘上仍是 delivering=true（未删）——崩溃窗口可恢复语义
        disk = service._store.load()
        assert "once_d1" in disk
        assert disk["once_d1"].delivering is True
        fired.append(record)

    service = ReminderService(
        tmp_path,
        enqueue=lambda *_: None,
        coara_id="root-test",
        timezone="UTC",
        tick_seconds=0.01,
        on_fire=on_fire,
    )
    tz = ZoneInfo("UTC")
    past = datetime.now(tz) - timedelta(seconds=1)
    service._records["once_d1"] = ReminderRecord(
        id="once_d1",
        kind=ReminderKind.ONE_TIME,
        message="投递窗口验证",
        next_run_at=past.isoformat(),
    )
    service._store.save(service._records)
    await service.start()
    try:
        await asyncio.sleep(0.05)
        assert len(fired) == 1
        # 投递完成后：内存与盘面都已移除
        assert await service.list_reminders() == []
        assert "once_d1" not in service._store.load()
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_delivering_records_resent_on_restart(tmp_path):
    """崩溃重启：盘面遗留 delivering 记录清除标记并重发（dedupe 兜底不重复）。"""
    home = tmp_path
    tz = ZoneInfo("UTC")
    past = datetime.now(tz) - timedelta(seconds=1)
    store_dict = {
        "once_c1": ReminderRecord(
            id="once_c1",
            kind=ReminderKind.ONE_TIME,
            message="崩溃前投递中",
            next_run_at=past.isoformat(),
            delivering=True,
        )
    }
    from src.reminders.store import ReminderStore

    ReminderStore(home / "reminders" / "store.json").save(store_dict)

    fired: list[ReminderRecord] = []

    async def on_fire(record: ReminderRecord) -> None:
        fired.append(record)

    service = ReminderService(
        home,
        enqueue=lambda *_: None,
        coara_id="root-test",
        timezone="UTC",
        tick_seconds=0.01,
        on_fire=on_fire,
    )
    await service.start()
    try:
        # normalize 只清除 delivering 不删记录；首个 tick 视为到期重发
        await asyncio.sleep(0.05)
        assert [r.id for r in fired] == ["once_c1"]
        # 投递成功后移除（与正常一次性提醒口径一致）
        assert await service.list_reminders() == []
        assert "once_c1" not in service._store.load()
    finally:
        await service.stop()
