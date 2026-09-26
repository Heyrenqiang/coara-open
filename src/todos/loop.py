"""Todo loop state helpers for Coara runtime (turn control, CLI, injectors)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.core.message_tags import system_reminder
from src.todos.display import format_todo_line, sort_todos_for_display
from src.todos.open import filter_open_todos
from src.todos.registry import get_todo_store
from src.todos.turn_control import TodoLoopState
from src.todos.types import TodoStatus


def load_session_todos(workspace_dir: Path, session_id: str) -> list[dict[str, Any]]:
    """Load all todos for a session as plain dicts."""
    store = get_todo_store(workspace_dir=workspace_dir, session_id=session_id)
    return [todo.to_dict() for todo in store.get_all()]


def build_todo_progress_key(todos: list[dict[str, Any]]) -> str:
    """Stable fingerprint of open todo work for stall detection.

    Changes when items are added/removed or open statuses flip (e.g. pending →
    in_progress / completed). Pure notes edits on open items also change the key
    so a real ``todo(update)`` counts as progress.
    """
    completed = sum(1 for todo in todos if todo.get("status") == TodoStatus.COMPLETED.value)
    failed = sum(1 for todo in todos if todo.get("status") == TodoStatus.FAILED.value)
    open_parts: list[str] = []
    for todo in todos:
        status = str(todo.get("status", "") or "")
        if status not in (TodoStatus.PENDING.value, TodoStatus.IN_PROGRESS.value):
            continue
        todo_id = str(todo.get("id", "") or "").strip() or "?"
        notes = str(todo.get("notes", "") or "").strip()
        open_parts.append(f"{todo_id}:{status}:{notes}")
    open_parts.sort()
    return f"c{completed}|f{failed}|{'|'.join(open_parts)}"


def build_todo_loop_state(todos: list[dict[str, Any]]) -> TodoLoopState:
    """Build a TodoLoopState from a list of todo dicts."""
    completed = sum(1 for todo in todos if todo.get("status") == TodoStatus.COMPLETED.value)
    return TodoLoopState(
        total=len(todos),
        completed=completed,
        has_active=any(todo.get("status") == TodoStatus.IN_PROGRESS.value for todo in todos),
        has_pending=any(todo.get("status") == TodoStatus.PENDING.value for todo in todos),
        progress_key=build_todo_progress_key(todos),
    )


def read_todo_loop_state(workspace_dir: Path, session_id: str) -> TodoLoopState:
    """Load todos and build loop state in one call."""
    return build_todo_loop_state(load_session_todos(workspace_dir, session_id))


def count_active_todos(todos: list[dict[str, Any]]) -> int:
    return len(filter_open_todos(todos))


def todos_have_active_work(todos: list[dict[str, Any]]) -> bool:
    return bool(filter_open_todos(todos))


def format_todo_incomplete_reminder(todos: list[dict[str, Any]]) -> str:
    """System reminder listing still-open todos when the loop continues on incomplete work."""
    open_todos = filter_open_todos(todos)
    if not open_todos:
        return system_reminder("待办清单已无未完成项，请正常收尾本轮。")
    lines = ["待办仍有未完成项："]
    for todo in sort_todos_for_display(open_todos):
        lines.append(format_todo_line(todo, content_limit=120, notes_limit=80))
    lines.append(
        "能推进的请继续推进。若剩余项暂时无法推进（依赖后台任务、等待外部条件、需用户定夺等），"
        '调用 todo(action="park", description="卡点说明", message="向用户交代的结束语") 一步收尾：'
        "调用即结束本轮、不再进行下一轮，message 会直接交付用户；"
        "若有事项已出结果，park 时带上 todos 整表一并更新（completed/failed 如实标注）。"
        "待办保持未完成、不伪装完结。"
        "不要反复输出没有实质进展的短回复。"
    )
    return system_reminder("\n".join(lines))
