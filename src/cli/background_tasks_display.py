"""CLI display for persisted background tasks (bash + agent)"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.background.task_store import TaskKind, TaskRecord, TaskStatus

_PREVIEW_CHARS = 40


@dataclass(frozen=True, slots=True)
class BackgroundTasksSnapshot:
    """Immutable view of running background tasks for one prompt redraw."""

    tasks: tuple[TaskRecord, ...]

    @property
    def count(self) -> int:
        return len(self.tasks)

    def labels(self) -> tuple[str, ...]:
        """全部运行中任务的标签（状态栏轮播用）。"""
        return tuple(format_task_label(record) for record in self.tasks)

    def items(self) -> tuple[dict[str, Any], ...]:
        """端上列表/强杀用的任务明细（含 task_id）。"""
        return tuple(serialize_task_item(record) for record in self.tasks)

    def summary(self) -> dict[str, Any]:
        """跨端下发用的轻量摘要：count + 任务标签列表（状态栏轮播用）。"""
        return {"count": self.count, "labels": list(self.labels())}


@dataclass(frozen=True, slots=True)
class BackgroundTasksMirror:
    """attach 客户端的服务端镜像：count + labels（状态栏轮播）"""

    count: int
    labels: tuple[str, ...] = ()


def serialize_task_item(record: TaskRecord) -> dict[str, Any]:
    """单条后台任务的端上可见字段。"""
    return {
        "task_id": str(record.task_id or ""),
        "kind": str(record.kind or ""),
        "label": format_task_label(record),
        "description": str(record.description or ""),
        "subagent_type": str(record.subagent_type or ""),
        "origin_source": str(record.origin_source or ""),
    }


def _manager_tracks_live(task_id: str, *, kind: str) -> bool:
    """True when the in-process runner still tracks this task (incl. start placeholder)."""
    if kind == TaskKind.BASH.value:
        from src.background.bash_runner import BashBackgroundRunner

        tasks = BashBackgroundRunner()._tasks
    else:
        from src.coara.background_agent import BackgroundAgentManager

        tasks = BackgroundAgentManager()._tasks
    if task_id not in tasks:
        return False
    task = tasks.get(task_id)
    return task is None or not task.done()


def _heal_stale_running(record: TaskRecord, root: Any) -> None:
    """TaskStore 仍 RUNNING、进程侧已无跟踪：就地标失败，避免 spinner 永转。"""
    try:
        from src.background.task_store_paths import task_store_for_coara
        from src.core.time import now_iso

        task_store_for_coara(root).update(
            record.task_id,
            status=TaskStatus.FAILED.value,
            error="Background runner no longer tracks this task",
            completed_at=now_iso(),
        )
    except Exception:
        pass


def _record_visible(
    record: TaskRecord,
    *,
    current_ws: str,
    origin_end: str | None,
) -> bool:
    """与 snapshot 同一把尺：排除系统子智能体 / 跨空间 / 跨端族。"""
    if record.subagent_type in ("janitor", "daily"):
        return False
    task_ws = str(getattr(record, "workspace_dir", "") or "").strip()
    if task_ws and current_ws:
        from src.coara.background_agent import _same_workspace_dir

        if not _same_workspace_dir(current_ws, task_ws):
            return False
    if origin_end is not None:
        from src.coara.turn_source import same_turn_family

        origin = str(getattr(record, "origin_source", "") or "").strip()
        if not origin or not same_turn_family(origin, origin_end):
            return False
    return True


def snapshot_running_tasks(
    root: Any,
    *,
    origin_end: str | None = None,
    require_live: bool = False,
) -> BackgroundTasksSnapshot:
    """Return visible tasks with RUNNING status from TaskStore.

    origin_end 给定时只保留该端族启动的任务（出站组帧用：CLI 心跳只带 CLI 族）。
    require_live：与本进程 runner 对账，丢掉僵尸 RUNNING（并尽力回写 FAILED）。
    """
    if root is None:
        return BackgroundTasksSnapshot(())
    from src.background.task_store_paths import task_store_for_coara

    records = task_store_for_coara(root).list_active()
    # workspace 未知时保留（避免真实在跑的任务从状态栏消失）
    current_ws = str(getattr(root, "workspace_dir", "") or "").strip()

    visible: list[TaskRecord] = []
    for record in records:
        if not _record_visible(record, current_ws=current_ws, origin_end=origin_end):
            continue
        if require_live and not _manager_tracks_live(record.task_id, kind=str(record.kind or "")):
            _heal_stale_running(record, root)
            continue
        visible.append(record)
    return BackgroundTasksSnapshot(tuple(visible))


def format_task_label(record: TaskRecord) -> str:
    prefix = "bash" if record.kind == TaskKind.BASH.value else (record.subagent_type or "agent").strip() or "agent"
    description = (record.description or record.task_id).strip()
    # Avoid "工作流: 工作流 xxx" / "janitor: janitor xxx" when description repeats the prefix.
    for marker in (prefix, "工作流"):
        if description.startswith(marker):
            description = description.removeprefix(marker).lstrip(" :：").strip() or record.task_id
            break
    if len(description) > _PREVIEW_CHARS:
        description = description[: _PREVIEW_CHARS - 1] + "…"
    return f"{prefix}: {description}"


async def kill_background_task(
    root: Any,
    task_id: str,
    *,
    origin_end: str | None = None,
) -> dict[str, Any]:
    """强杀一条对本端可见的后台任务；立刻回写 TaskStore，避免 spinner 残留。"""
    tid = str(task_id or "").strip()
    if not tid or root is None:
        return {"ok": False, "killed": False, "reason": "missing_task_id"}

    from src.background.task_store_paths import task_store_for_coara
    from src.core.time import now_iso

    store = task_store_for_coara(root)
    record = store.load(tid)
    if record is None:
        return {"ok": False, "killed": False, "reason": "not_found"}

    current_ws = str(getattr(root, "workspace_dir", "") or "").strip()
    if not _record_visible(record, current_ws=current_ws, origin_end=origin_end):
        return {"ok": False, "killed": False, "reason": "forbidden"}

    if record.status != TaskStatus.RUNNING.value:
        return {"ok": True, "killed": False, "reason": "already_done", "task_id": tid}

    kind = str(record.kind or "")
    cancelled = False
    if kind == TaskKind.BASH.value:
        from src.background.bash_runner import BashBackgroundRunner

        cancelled = bool(await BashBackgroundRunner().stop_task(tid, force=True))
    else:
        from src.coara.background_agent import BackgroundAgentManager

        cancelled = bool(await BackgroundAgentManager().cancel(tid))

    # bash stop_task 已写 KILLED；agent / 僵尸仍 RUNNING 时此处兜底落盘
    fresh = store.load(tid)
    if fresh is not None and fresh.status == TaskStatus.RUNNING.value:
        store.update(
            tid,
            status=TaskStatus.KILLED.value,
            interrupted=True,
            error="Killed by user",
            completed_at=now_iso(),
        )

    return {
        "ok": True,
        "killed": True,
        "cancelled": cancelled,
        "task_id": tid,
        "label": format_task_label(record),
    }
