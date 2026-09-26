"""Markdown and line formatters for todo lists."""

from __future__ import annotations

from typing import Any

from src.core.message_tags import system_reminder
from src.todos.changes import TodoChanges
from src.todos.types import ACTIVE_TODO_STATUSES, TodoStatus, todo_priority_rank

TODO_STATUS_MARKERS: dict[str, str] = {
    TodoStatus.PENDING.value: "[ ]",
    TodoStatus.IN_PROGRESS.value: "[•]",
    TodoStatus.COMPLETED.value: "[✓]",
    TodoStatus.FAILED.value: "[!]",
}


def todo_status_marker(status: str) -> str:
    return TODO_STATUS_MARKERS.get(status, "[ ]")


def _truncate_text(text: str, limit: int | None) -> str:
    if limit is None:
        return text
    if limit <= 0:
        return "..."
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


def format_todo_line(
    todo: dict[str, Any],
    *,
    content_limit: int | None = None,
    notes_limit: int | None = None,
) -> str:
    todo_id = str(todo.get("id", "") or "").strip()
    content = _truncate_text(str(todo.get("content", "") or ""), content_limit)
    prefix = f"{todo_id} " if todo_id else ""
    line = f"{todo_status_marker(str(todo.get('status', TodoStatus.PENDING.value)))} {prefix}{content}".rstrip()
    notes = _truncate_text(str(todo.get("notes", "") or "").strip(), notes_limit)
    if notes:
        line += f" - {notes}"
    return line


def sort_todos_for_display(todos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort by status (in_progress first), then priority (high first), then id."""
    status_order = {
        TodoStatus.IN_PROGRESS.value: 0,
        TodoStatus.PENDING.value: 1,
        TodoStatus.COMPLETED.value: 2,
        TodoStatus.FAILED.value: 3,
    }
    return sorted(
        todos,
        key=lambda todo: (
            status_order.get(str(todo.get("status", "")), 9),
            todo_priority_rank(str(todo.get("priority", "medium"))),
            str(todo.get("id", "")),
        ),
    )


def format_todo_markdown(summary: str, todos: list[dict[str, Any]]) -> str:
    """Render todos into a compact markdown block."""
    lines = [format_todo_line(todo) for todo in sort_todos_for_display(todos)]
    if summary:
        return f"{summary}\n" + "\n".join(lines)
    return "\n".join(lines)


def _status_only_flips(
    previous: list[dict[str, Any]] | None,
    todos: list[dict[str, Any]],
) -> list[dict[str, Any]] | None:
    """仅状态翻转（无新增/删除/文案修改）时返回状态有变化的条目；否则返回 None。"""
    if previous is None:
        return None
    old_by_id = {str(t.get("id", "")): t for t in previous if t.get("id")}
    new_by_id = {str(t.get("id", "")): t for t in todos if t.get("id")}
    if set(old_by_id) != set(new_by_id):
        return None
    flipped: list[dict[str, Any]] = []
    for todo in todos:
        old = old_by_id[str(todo.get("id", ""))]
        if any(todo.get(field) != old.get(field) for field in ("content", "notes", "priority")):
            return None
        if str(todo.get("status", "")) != str(old.get("status", "")):
            flipped.append(todo)
    return flipped


def format_todo_write_model_content(
    *,
    summary: str,
    todos: list[dict[str, Any]],
    changes: TodoChanges | None = None,
    previous: list[dict[str, Any]] | None = None,
) -> str:
    """Model-facing todo write result: 变化行或全表 + 一行纯数据提醒。

    仅状态翻转时回执只给变化行（id → 状态）+ 剩余统计；结构有变化（新增/删除/
    文案修改）才回排序后全表。提醒只含事实数据（完成/失败点名、剩余项、下一个），
    行为指导归工具描述，不在此重复。
    """
    sorted_todos = sort_todos_for_display(todos)
    if not todos:
        reminder = system_reminder("待办清单已清空，无未完成项。")
        return f"{summary}\n\n{reminder}".strip() if summary else reminder

    remaining = [todo for todo in sorted_todos if str(todo.get("status", "")) in ACTIVE_TODO_STATUSES]

    fact_parts: list[str] = []
    if changes and changes.completed:
        fact_parts.append("已完成：" + "、".join(f"{t.get('id')}" for t in changes.completed))
    if changes and changes.failed:
        fact_parts.append("失败：" + "、".join(f"{t.get('id')}" for t in changes.failed))
    if changes and changes.created:
        fact_parts.append(f"新建 {len(changes.created)} 项")
    if remaining:
        remain_ids = "、".join(f"{t.get('id')}({todo_status_marker(str(t.get('status', '')))})" for t in remaining)
        fact_parts.append(f"剩余 {len(remaining)} 项：{remain_ids}")
        nxt = remaining[0]
        nxt_content = _truncate_text(str(nxt.get("content", "") or ""), 30)
        fact_parts.append(f"下一个：{nxt.get('id')}（{nxt_content}）")
    elif changes and (changes.completed or changes.failed):
        fact_parts.append("清单全部完成")
    if not fact_parts:
        fact_parts.append("清单无变化")
    reminder = system_reminder("；".join(fact_parts) + "。")

    flipped = _status_only_flips(previous, todos)
    lines: list[str] = []
    if summary:
        lines.append(summary)
    if flipped is not None:
        lines.extend(f"{t.get('id')} → {t.get('status')}" for t in sort_todos_for_display(flipped))
    else:
        lines.extend(format_todo_line(todo) for todo in sorted_todos)
    body = "\n".join(lines).strip()
    return f"{body}\n\n{reminder}" if body else reminder
