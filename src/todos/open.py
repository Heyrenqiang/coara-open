"""Open todo filtering and ordering for display layers."""

from __future__ import annotations

from typing import Any

from src.todos.types import ACTIVE_TODO_STATUSES


def filter_open_todos(todos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return todos that are still pending or in_progress."""
    return [todo for todo in todos if str(todo.get("status", "")) in ACTIVE_TODO_STATUSES]
