"""Todo data models for session-scoped self-managed execution lists."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from src.core.time import now_iso


class TodoStatus(StrEnum):
    """Todo item lifecycle."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


# Soft transition rules: unusual moves are logged but not blocked.
ALLOWED_STATUS_TRANSITIONS: dict[TodoStatus, frozenset[TodoStatus]] = {
    TodoStatus.PENDING: frozenset({TodoStatus.IN_PROGRESS, TodoStatus.COMPLETED, TodoStatus.FAILED}),
    TodoStatus.IN_PROGRESS: frozenset({TodoStatus.PENDING, TodoStatus.COMPLETED, TodoStatus.FAILED}),
    TodoStatus.COMPLETED: frozenset({TodoStatus.PENDING}),
    TodoStatus.FAILED: frozenset({TodoStatus.PENDING, TodoStatus.IN_PROGRESS}),
}

TODO_PRIORITY_RANK: dict[str, int] = {"high": 0, "medium": 1, "low": 2}


def is_allowed_status_transition(old: TodoStatus, new: TodoStatus) -> bool:
    if old == new:
        return True
    return new in ALLOWED_STATUS_TRANSITIONS.get(old, frozenset())


def todo_priority_rank(priority: str) -> int:
    return TODO_PRIORITY_RANK.get(str(priority or "medium").lower(), 1)


ACTIVE_TODO_STATUSES = frozenset({TodoStatus.PENDING.value, TodoStatus.IN_PROGRESS.value})


@dataclass(slots=True)
class TodoItem:
    """Single self-managed todo item."""

    id: str
    content: str
    status: TodoStatus
    priority: str = "medium"
    notes: str = ""
    updated_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "status": self.status.value,
            "priority": self.priority,
            "notes": self.notes,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TodoItem:
        return cls(
            id=str(data.get("id", "")),
            content=str(data.get("content", "")),
            status=TodoStatus(str(data.get("status", TodoStatus.PENDING.value))),
            priority=str(data.get("priority", "medium")),
            notes=str(data.get("notes", "")),
            updated_at=str(data.get("updated_at", now_iso())),
        )


@dataclass(slots=True)
class TodoState:
    """Persisted todo state for one Coara session."""

    session_id: str
    updated_at: str
    todos: list[TodoItem]

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "updated_at": self.updated_at,
            "todos": [todo.to_dict() for todo in self.todos],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TodoState:
        return cls(
            session_id=str(data.get("session_id", "")),
            updated_at=str(data.get("updated_at", now_iso())),
            todos=[TodoItem.from_dict(item) for item in data.get("todos", []) or []],
        )
