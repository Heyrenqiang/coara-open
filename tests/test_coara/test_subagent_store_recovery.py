"""SubagentStore stale-running recovery after a process restart.

A kill/power-loss leaves records stuck in running_* forever while
``delegate(action=resume)`` only accepts cancelled/failed — the startup
reconciliation converges them so breakpoints become resumable again.
"""

from __future__ import annotations

from pathlib import Path

from src.coara.subagent_store import SubagentRecord, SubagentStore
from src.core.time import now_iso
from src.core.types import SubagentStatus


def _record(agent_id: str, status: str) -> SubagentRecord:
    now = now_iso()
    return SubagentRecord(
        agent_id=agent_id,
        subagent_type="coaras",
        description=f"task {agent_id}",
        message_history=[{"role": "user", "content": "干活"}],
        status=status,
        created_at=now,
        updated_at=now,
        session_id="s1",
        child_coara_id="c1",
    )


def test_recover_stale_running_marks_both_running_states_failed(tmp_path: Path) -> None:
    store = SubagentStore(tmp_path)
    store.save(_record("sa-fg", SubagentStatus.RUNNING_FOREGROUND.value))
    store.save(_record("sa-bg", SubagentStatus.RUNNING_BACKGROUND.value))
    store.save(_record("sa-idle", SubagentStatus.IDLE.value))
    store.save(_record("sa-cancelled", SubagentStatus.CANCELLED.value))

    recovered = store.recover_stale_running()
    assert recovered == 2

    fg = store.load("sa-fg")
    bg = store.load("sa-bg")
    assert fg is not None and bg is not None
    assert fg.status == SubagentStatus.FAILED.value
    assert bg.status == SubagentStatus.FAILED.value
    assert fg.error and "restarted" in fg.error
    # 终态与空闲记录保持原样
    idle = store.load("sa-idle")
    cancelled = store.load("sa-cancelled")
    assert idle is not None and idle.status == SubagentStatus.IDLE.value
    assert cancelled is not None and cancelled.status == SubagentStatus.CANCELLED.value


def test_recovered_record_is_resumable_and_keeps_history(tmp_path: Path) -> None:
    """resume 只认 cancelled/failed：对账后的记录带着完整中间历史可恢复"""
    store = SubagentStore(tmp_path)
    store.save(_record("sa-run", SubagentStatus.RUNNING_FOREGROUND.value))

    store.recover_stale_running()

    record = store.load("sa-run")
    assert record is not None
    assert record.status in (SubagentStatus.CANCELLED.value, SubagentStatus.FAILED.value)
    assert record.message_history == [{"role": "user", "content": "干活"}]


def test_recover_noop_when_nothing_running(tmp_path: Path) -> None:
    store = SubagentStore(tmp_path)
    store.save(_record("sa-idle", SubagentStatus.IDLE.value))
    assert store.recover_stale_running() == 0
    record = store.load("sa-idle")
    assert record is not None and record.status == SubagentStatus.IDLE.value


def test_constructor_and_readonly_reconcile_create_no_directory(tmp_path: Path) -> None:
    """目录惰性创建：构造与只读对账不落盘 从未跑过子智能体的空间不留空 subagents/"""
    store = SubagentStore(tmp_path)
    storage_dir = store._storage_dir
    assert not storage_dir.exists()

    assert store.list_all() == []
    assert store.load("sa-none") is None
    assert store.delete("sa-none") is False
    assert store.recover_stale_running() == 0
    assert not storage_dir.exists()


def test_first_save_creates_storage_directory(tmp_path: Path) -> None:
    store = SubagentStore(tmp_path)
    store.save(_record("sa-1", SubagentStatus.IDLE.value))
    assert store._storage_dir.is_dir()
    assert store.load("sa-1") is not None
