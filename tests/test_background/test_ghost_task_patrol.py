"""幽灵 running 根治：子进程已退、监控协程卡死时由巡检强制收官（bash-ede88b97 形态）"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import src.background.bash_runner as bash_runner
from src.background.bash_runner import BashBackgroundRunner
from src.background.task_store import TaskStatus
from src.background.task_store_paths import reset_task_store_cache


@pytest.fixture
def fresh_runner():
    reset_task_store_cache()
    runner = BashBackgroundRunner()
    runner._tasks.clear()
    runner._task_stores.clear()
    runner._task_workspaces.clear()
    runner._task_sessions.clear()
    runner._task_origins.clear()
    runner._cancel_requested.clear()
    yield runner
    for task in list(runner._tasks.values()):
        if task is not None and not task.done():
            task.cancel()
    patrol = runner.__dict__.get("_ghost_patrol")
    if patrol is not None and not patrol.done():
        patrol.cancel()


class _ExitedFakeProcess:
    """模拟「已退出但 wait() 永不返回」的子进程（Windows 孙进程握管事故形态）"""

    def __init__(self) -> None:
        self.pid = 424242
        self.returncode = 0
        self.stdout = None
        self.stderr = None
        self._transport = None

    async def wait(self) -> int:
        await asyncio.Future()
        return self.returncode


@pytest.mark.asyncio
async def test_patrol_cancels_stuck_monitor_and_publishes_completion(
    fresh_runner: BashBackgroundRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bash_runner, "_GHOST_PATROL_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(bash_runner, "GHOST_TASK_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(bash_runner, "resolve_background_task_timeout", lambda: None)
    published: list = []

    class _Bus:
        def publish(self, event) -> None:
            published.append(event)

    fresh_runner.set_event_bus(_Bus())
    task_id = await fresh_runner.create_task(
        "echo ghost",
        "ghost demo",
        workspace_dir=tmp_path,
        _process=_ExitedFakeProcess(),
    )
    store = fresh_runner._task_stores[task_id]

    deadline = asyncio.get_running_loop().time() + 10.0
    status = ""
    while asyncio.get_running_loop().time() < deadline:
        record = store.load(task_id)
        status = record.status if record is not None else ""
        monitor = fresh_runner._tasks.get(task_id)
        # KILLED 写在 except CancelledError，完成事件与内存表清理在外层 finally——
        # 必须等 finally 落定再断言，否则与巡检 cancel 竞态
        if status == TaskStatus.KILLED.value and task_id not in fresh_runner._tasks and (
            monitor is None or monitor.done()
        ):
            break
        await asyncio.sleep(0.02)

    record = store.load(task_id)
    assert record is not None
    assert record.status == TaskStatus.KILLED.value, f"stuck task not force-settled (status={status!r})"
    assert record.completed_at
    assert task_id not in fresh_runner._tasks
    completions = [e for e in published if e.event_type == "background_task_complete"]
    assert len(completions) == 1
    assert completions[0].payload["task_id"] == task_id


@pytest.mark.asyncio
async def test_patrol_leaves_active_process_alone(
    fresh_runner: BashBackgroundRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bash_runner, "_GHOST_PATROL_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(bash_runner, "GHOST_TASK_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(bash_runner, "resolve_background_task_timeout", lambda: None)

    class _RunningFakeProcess(_ExitedFakeProcess):
        def __init__(self) -> None:
            super().__init__()
            self.returncode = None

    fresh_runner.set_event_bus(None)
    task_id = await fresh_runner.create_task(
        "sleep 999",
        "running demo",
        workspace_dir=tmp_path,
        _process=_RunningFakeProcess(),
    )
    await asyncio.sleep(0.2)
    monitor = fresh_runner._tasks.get(task_id)
    assert monitor is not None and not monitor.done(), "patrol must not touch tasks whose process is still running"
    monitor.cancel()
    await asyncio.wait_for(asyncio.gather(monitor, return_exceptions=True), timeout=5)
