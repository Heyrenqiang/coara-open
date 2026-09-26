"""Todo list diff detection for UI and tool metadata."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .types import TodoStatus


@dataclass(slots=True)
class TodoChanges:
    created: list[dict[str, Any]]
    completed: list[dict[str, Any]]
    updated: list[dict[str, Any]]
    failed: list[dict[str, Any]]

def _fields_changed(previous: dict[str, Any], todo: dict[str, Any]) -> bool:
    return any(todo.get(field) != previous.get(field) for field in ("notes", "priority", "content"))


def detect_todo_changes(
    old_todos: list[dict[str, Any]],
    new_todos: list[dict[str, Any]],
) -> TodoChanges:
    old_by_id = {str(item.get("id", "")): item for item in old_todos if item.get("id")}
    created: list[dict[str, Any]] = []
    completed: list[dict[str, Any]] = []
    updated: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    for todo in new_todos:
        todo_id = str(todo.get("id", ""))
        if not todo_id:
            continue
        previous = old_by_id.get(todo_id)
        if previous is None:
            created.append(dict(todo))
            continue

        new_status = str(todo.get("status", ""))
        old_status = str(previous.get("status", ""))
        if new_status == TodoStatus.COMPLETED.value and old_status != TodoStatus.COMPLETED.value:
            completed.append(dict(todo))
            continue
        if new_status == TodoStatus.FAILED.value and old_status != TodoStatus.FAILED.value:
            failed.append(dict(todo))
            continue
        if new_status != old_status or _fields_changed(previous, todo):
            updated.append(dict(todo))

    return TodoChanges(
        created=created,
        completed=completed,
        updated=updated,
        failed=failed,
    )
