"""CLI display for persisted background tasks (bash + agent).

Single read path via TaskStore — observational only; never uses UnifiedScheduler.

Display slots (see ``BackgroundSpinner``):
  - Bottom toolbar badge ``后台 N`` (always when count > 0)
  - Idle prompt line above input (only when no active turn / status block)
  - Message queue hint (``input_queue_display``) is separate — shown only during turns
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.background.task_store import TaskKind, TaskRecord

_PREVIEW_CHARS = 40
_MAX_IDLE_LABELS = 3


@dataclass(frozen=True, slots=True)
class BackgroundTasksSnapshot:
    """Immutable view of running background tasks for one prompt redraw."""

    tasks: tuple[TaskRecord, ...]

    @property
    def count(self) -> int:
        return len(self.tasks)

    def idle_prompt_line(self) -> str | None:
        """One-line summary for the dynamic prompt when idle."""
        if not self.tasks:
            return None
        labels = [format_task_label(record) for record in self.tasks[:_MAX_IDLE_LABELS]]
        line = f"后台 {' / '.join(labels)}"
        overflow = len(self.tasks) - _MAX_IDLE_LABELS
        if overflow > 0:
            line += f" … +{overflow}"
        return line


def snapshot_running_tasks(root: Any) -> BackgroundTasksSnapshot:
    """Return visible tasks with RUNNING status from TaskStore.

    系统维护管家（janitor/daily）与工作流隐藏：
    janitor/daily 是系统派发 运行与否不面向用户；
    工作流 是独立子系统 状态只在 WebUI /workflow 展示。
    aide 与其它后台任务（bash/coaras 概念后台）一样 状态栏后台标识可见。
    """
    if root is None:
        return BackgroundTasksSnapshot(())
    from src.background.task_store_paths import task_store_for_coara

    records = task_store_for_coara(root).list_active()
    # 系统维护管家（janitor/daily）与工作流不显示：
    # janitor/daily 是系统派发 运行与否不面向用户；
    # 工作流 是独立子系统 状态只在 WebUI /workflow 展示。
    visible = tuple(
        record for record in records if record.subagent_type not in ("janitor", "daily")
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
