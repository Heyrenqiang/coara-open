"""#16：压缩后快照注入读会话工作空间对应的 TaskStore——多工作空间后台回合
压缩时，A 空间上下文不得串入进程级默认 store（Path.cwd()）里的别空间任务清单
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from src.background.task_store import TaskRecord, TaskStatus
from src.background.task_store_paths import default_task_store, task_store_for_coara
from src.coara.injections.snapshot_injector import SnapshotInjector
from src.todos.turn_control import TodoLoopState
from tests.helpers import FakeProvider, make_test_coara


def _running_record(task_id: str, description: str) -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        kind="bash",
        description=description,
        status=TaskStatus.RUNNING.value,
        created_at="2026-08-20T00:00:00",
        updated_at="2026-08-20T00:00:00",
    )


def test_build_snapshots_reads_workspace_scoped_store(tmp_path: Path) -> None:
    coara = make_test_coara(tmp_path / "ws_a", provider=FakeProvider([]))
    # 模拟多工作空间运行时：本会话 home 独立于进程级默认 home
    coara.workspace_manager = SimpleNamespace(coara_home=tmp_path / "home_a")

    # 本会话工作空间 store 里的运行中任务：应出现在快照里
    task_store_for_coara(coara).save(_running_record("task-a", "A 空间任务"))
    # 进程级默认 store（Path.cwd()）里的别空间任务：不得串入本会话快照
    default_task_store().save(_running_record("task-b", "别空间任务"))

    snapshots = SnapshotInjector.build_snapshots(TodoLoopState(), [], coara=coara)
    contents = "\n".join(str(m.content) for m in snapshots)

    assert "task-a" in contents
    assert "A 空间任务" in contents
    assert "task-b" not in contents
    assert "别空间任务" not in contents
