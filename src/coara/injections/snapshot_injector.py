"""Post-compression snapshot injector.

After ContextWindowManager compacts message history, active background tasks
and todo state would be lost. We inject lightweight snapshots so the agent
still knows what is in flight.

All snapshots use USER role because they are injected into the
conversation as runtime status notifications.
"""

from __future__ import annotations

from typing import Any

from src.core.types import Message, MessageRole
from src.todos.display import format_todo_line, sort_todos_for_display
from src.todos.open import filter_open_todos
from src.todos.turn_control import TodoLoopState


class SnapshotInjector:
    """Build snapshot messages to inject after context compression."""

    @staticmethod
    def build_todo_snapshot(todo_state: TodoLoopState) -> Message | None:
        """Build a snapshot of current todo state."""
        if todo_state.total == 0 or todo_state.completed >= todo_state.total:
            return None
        lines = [
            "待办清单（压缩后保留）：",
            f"- 总数：{todo_state.total}",
            f"- 已完成：{todo_state.completed}",
        ]
        if todo_state.has_active:
            lines.append("- 有进行中的待办项")
        elif todo_state.has_pending:
            lines.append("- 有待处理的待办项")
        return Message(role=MessageRole.USER, content="\n".join(lines))

    @staticmethod
    def build_todo_snapshot_from_todos(todos: list[dict]) -> Message | None:
        """Build a richer todo snapshot with open item lines."""
        open_todos = filter_open_todos(todos)
        if not open_todos:
            return None

        lines = ["待办清单（压缩后保留）："]
        for todo in sort_todos_for_display(open_todos):
            lines.append(format_todo_line(todo, content_limit=120, notes_limit=80))
        return Message(role=MessageRole.USER, content="\n".join(lines))

    @classmethod
    def build_snapshots(
        cls,
        todo_state: TodoLoopState,
        todos: list[dict] | None = None,
        *,
        coara: Any | None = None,
    ) -> list[Message]:
        """Build all snapshot messages after context compression.

        Returns a list of Messages to append after compressed history.
        """
        snapshots: list[Message] = []

        # 后台任务快照必须读本会话工作空间对应的 store：进程级默认 store
        # （Path.cwd()）在多工作空间后台回合压缩时会串入别空间的任务清单
        if coara is not None:
            from src.background.task_store_paths import task_store_for_coara

            store = task_store_for_coara(coara)
        else:
            from src.background.task_store_paths import default_task_store

            store = default_task_store()
        active_records = store.list_active()
        if active_records:
            lines = [
                "以下后台任务仍在运行（完成后系统会提醒）：",
                "",
            ]
            for rec in active_records:
                icon = "●" if rec.status == "running" else "?"
                if rec.kind == "bash":
                    lines.append(f"[{icon}] {rec.task_id} (bash): {rec.description}")
                else:
                    lines.append(f"[{icon}] {rec.task_id} ({rec.subagent_type}): {rec.description}")
            snapshots.append(Message(role=MessageRole.USER, content="\n".join(lines)))

        todo_snapshot = None
        if todos:
            todo_snapshot = cls.build_todo_snapshot_from_todos(todos)
        if todo_snapshot is None:
            todo_snapshot = cls.build_todo_snapshot(todo_state)
        if todo_snapshot is not None:
            snapshots.append(todo_snapshot)

        return snapshots
