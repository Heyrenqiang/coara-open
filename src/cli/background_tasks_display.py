"""CLI display for persisted background tasks (bash + agent)"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.background.task_store import TaskKind, TaskRecord

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

    def summary(self) -> dict[str, Any]:
        """跨端下发用的轻量摘要：count + 任务标签列表（状态栏轮播用）。"""
        return {"count": self.count, "labels": list(self.labels())}


@dataclass(frozen=True, slots=True)
class BackgroundTasksMirror:
    """attach 客户端的服务端镜像：count + labels（状态栏轮播）"""

    count: int
    labels: tuple[str, ...] = ()


def snapshot_running_tasks(
    root: Any,
    *,
    origin_end: str | None = None,
) -> BackgroundTasksSnapshot:
    """Return visible tasks with RUNNING status from TaskStore.

    origin_end 给定时只保留该端族启动的任务（出站组帧用：CLI 心跳只带 CLI 族）。
    """
    if root is None:
        return BackgroundTasksSnapshot(())
    from src.background.task_store_paths import task_store_for_coara

    records = task_store_for_coara(root).list_active()
    # 避免真实在跑的任务从状态栏消失）。
    current_ws = str(getattr(root, "workspace_dir", "") or "").strip()

    def _same_workspace(record: TaskRecord) -> bool:
        task_ws = str(getattr(record, "workspace_dir", "") or "").strip()
        if not task_ws or not current_ws:
            return True
        from src.coara.background_agent import _same_workspace_dir

        return _same_workspace_dir(current_ws, task_ws)

    def _origin_ok(record: TaskRecord) -> bool:
        if origin_end is None:
            return True
        from src.coara.turn_source import same_turn_family

        # 出站主门：无启动端记录不投（宁缺勿错灌进它端）
        origin = str(getattr(record, "origin_source", "") or "").strip()
        if not origin:
            return False
        return same_turn_family(origin, origin_end)

    visible = tuple(
        record
        for record in records
        if record.subagent_type not in ("janitor", "daily") and _same_workspace(record) and _origin_ok(record)
    )
    return BackgroundTasksSnapshot(visible)


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
