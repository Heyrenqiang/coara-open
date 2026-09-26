"""Todo loop helpers and TodoStore persistence."""

from __future__ import annotations

from pathlib import Path

from src.todos.loop import (
    build_todo_loop_state,
    count_active_todos,
    load_session_todos,
    todos_have_active_work,
)
from src.todos.store import TodoStore
from src.todos.types import ACTIVE_TODO_STATUSES, TodoItem, TodoStatus


def test_todo_loop_helpers(tmp_path: Path) -> None:
    assert frozenset({"pending", "in_progress"}) == ACTIVE_TODO_STATUSES
    assert todos_have_active_work([{"id": "1", "status": "pending"}])
    assert not todos_have_active_work([{"id": "1", "status": "completed"}])

    todos = [
        {"id": "1", "status": "pending"},
        {"id": "2", "status": "in_progress"},
        {"id": "3", "status": "completed"},
    ]
    assert count_active_todos(todos) == 2

    state = build_todo_loop_state(
        [
            {"id": "1", "status": "completed"},
            {"id": "2", "status": "in_progress"},
            {"id": "3", "status": "pending"},
        ]
    )
    assert state.total == 3 and state.completed == 1
    assert state.has_active is True and state.has_pending is True

    store = TodoStore(workspace_dir=tmp_path, session_id="sess-1")
    store.merge_updates(
        [
            TodoItem(id="1", content="a", status=TodoStatus.PENDING),
            TodoItem(id="2", content="b", status=TodoStatus.COMPLETED),
        ]
    )
    assert count_active_todos(load_session_todos(tmp_path, "sess-1")) == 1


def test_todo_store_persistence(tmp_path: Path) -> None:
    store = TodoStore(workspace_dir=tmp_path, session_id="sess-a")
    store.merge_updates(
        [
            TodoItem(id="1", content="first", status=TodoStatus.PENDING),
            TodoItem(id="2", content="second", status=TodoStatus.PENDING),
        ]
    )
    reloaded = TodoStore(workspace_dir=tmp_path, session_id="sess-a").get_all()
    assert len(reloaded) == 2 and reloaded[0].id == "1" and reloaded[1].content == "second"

    store_b = TodoStore(workspace_dir=tmp_path, session_id="sess-b")
    store_b.merge_updates([TodoItem(id="1", content="old", status=TodoStatus.PENDING)])
    store_b.merge_updates([TodoItem(id="1", content="old", status=TodoStatus.COMPLETED, notes="done")])
    merged = store_b.get_all()
    assert len(merged) == 1 and merged[0].status == TodoStatus.COMPLETED and merged[0].notes == "done"

    store_c = TodoStore(workspace_dir=tmp_path, session_id="sess-c")
    store_c.merge_updates(
        [
            TodoItem(id="1", content="a", status=TodoStatus.IN_PROGRESS),
            TodoItem(id="2", content="b", status=TodoStatus.IN_PROGRESS),
        ]
    )
    statuses = {todo.id: todo.status for todo in store_c.get_all()}
    # 多项 in_progress 原样保留（并行推进是合法状态，store 不做焦点归一）。
    assert statuses["1"] == TodoStatus.IN_PROGRESS
    assert statuses["2"] == TodoStatus.IN_PROGRESS

    store_d = TodoStore(workspace_dir=tmp_path, session_id="sess-d")
    store_d.merge_updates([TodoItem(id="1", content="x", status=TodoStatus.PENDING)])
    store_d.clear()
    assert store_d.get_all() == []
