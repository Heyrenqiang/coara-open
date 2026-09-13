"""Todo store reliability: persistence errors, registry, changes, reminders."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from src.todos.changes import detect_todo_changes
from src.todos.display import format_todo_write_model_content, sort_todos_for_display
from src.todos.open import filter_open_todos
from src.todos.registry import get_todo_store, get_todo_store_lock, reset_todo_store_registry
from src.todos.store import TodoStore, TodoStoreError
from src.todos.trace_payload import build_todo_update_trace_payload
from src.todos.types import TodoItem, TodoStatus


def test_save_failure_raises_todo_store_error(tmp_path: Path) -> None:
    store = TodoStore(workspace_dir=tmp_path, session_id="sess-save")
    with (
        patch("src.todos.store.write_json_atomic", side_effect=OSError("disk full")),
        pytest.raises(TodoStoreError, match="Failed to save"),
    ):
        store.merge_updates([TodoItem(id="1", content="a", status=TodoStatus.PENDING)])


def test_get_todo_store_returns_cached_instance(tmp_path: Path) -> None:
    reset_todo_store_registry()
    first = get_todo_store(workspace_dir=tmp_path, session_id="sess-cache")
    second = get_todo_store(workspace_dir=tmp_path, session_id="sess-cache")
    assert first is second


def test_store_and_lock_evict_together(tmp_path: Path) -> None:
    """store 与 lock 同键共生逐出：LRU 逐出后两侧一起换新，不出现双实例双锁。"""
    reset_todo_store_registry()
    first_store = get_todo_store(tmp_path, "victim")
    first_lock = get_todo_store_lock(tmp_path, "victim")
    # 只经 store 入口搅动 LRU 至溢出（旧实现只逐出 store 缓存，lock 缓存残留）
    for i in range(64):
        get_todo_store(tmp_path, f"churn-{i}")
    new_store = get_todo_store(tmp_path, "victim")
    new_lock = get_todo_store_lock(tmp_path, "victim")
    assert new_store is not first_store
    assert new_lock is not first_lock


def test_detect_todo_changes_tracks_updated_and_failed() -> None:
    old = [
        {"id": "1", "status": "pending", "content": "a", "notes": ""},
        {"id": "2", "status": "in_progress", "content": "b", "notes": ""},
    ]
    new = [
        {"id": "1", "status": "in_progress", "content": "a", "notes": ""},
        {"id": "2", "status": "failed", "content": "b", "notes": "timeout"},
    ]
    changes = detect_todo_changes(old, new)
    assert len(changes.updated) == 1
    assert changes.updated[0]["id"] == "1"
    assert len(changes.failed) == 1
    assert changes.failed[0]["id"] == "2"


def test_format_todo_write_reminder_points_next_task() -> None:
    """completed 变化：无 previous（保守回全表）时 reminder 点名已完成、剩余与下一个（纯数据）。"""
    todos = [
        {"id": "1", "status": "completed", "content": "done task"},
        {"id": "2", "status": "in_progress", "content": "active task"},
    ]
    changes = detect_todo_changes(
        [{"id": "1", "status": "pending", "content": "done task"}],
        todos,
    )
    content = format_todo_write_model_content(summary="", todos=todos, changes=changes)
    assert "done task" in content
    assert "已完成：1" in content
    assert "剩余 1 项：2" in content
    assert "下一个：2" in content
    assert "active task" in content
    assert "todo(update)" not in content


def test_format_todo_write_status_only_gives_changed_rows() -> None:
    """仅状态翻转（无新增/删除/文案修改）：回执只给变化行 + 一行剩余统计，不回全表。"""
    previous = [
        {"id": "1", "status": "in_progress", "content": "task one"},
        {"id": "2", "status": "pending", "content": "task two"},
    ]
    todos = [
        {"id": "1", "status": "completed", "content": "task one"},
        {"id": "2", "status": "in_progress", "content": "task two"},
    ]
    changes = detect_todo_changes(previous, todos)
    content = format_todo_write_model_content(summary="", todos=todos, changes=changes, previous=previous)
    assert "1 → completed" in content
    assert "2 → in_progress" in content
    assert "[✓] 1 task one" not in content  # 全表未回显
    assert "已完成：1" in content
    assert "剩余 1 项：2" in content


def test_format_todo_write_structural_change_returns_full_table() -> None:
    """结构变化（新增/删除/文案修改）：回排序后全表。"""
    previous = [{"id": "1", "status": "pending", "content": "old text"}]
    todos = [
        {"id": "1", "status": "pending", "content": "new text"},
        {"id": "2", "status": "pending", "content": "brand new"},
    ]
    changes = detect_todo_changes(previous, todos)
    content = format_todo_write_model_content(summary="", todos=todos, changes=changes, previous=previous)
    assert "new text" in content
    assert "brand new" in content


def test_sort_todos_for_display_orders_priority_within_pending() -> None:
    todos = [
        {"id": "1", "status": "pending", "content": "low", "priority": "low"},
        {"id": "2", "status": "pending", "content": "high", "priority": "high"},
    ]
    ordered = sort_todos_for_display(todos)
    assert ordered[0]["id"] == "2"


@pytest.mark.asyncio
async def test_todo_store_lock_serializes_concurrent_writes(tmp_path: Path) -> None:
    reset_todo_store_registry()
    session_id = "sess-lock"
    lock = get_todo_store_lock(tmp_path, session_id)
    store = get_todo_store(tmp_path, session_id)

    async def write_status(todo_id: str, status: TodoStatus) -> None:
        async with lock:
            current = store.get_all()
            by_id = {todo.id: todo for todo in current}
            item = by_id.get(todo_id) or TodoItem(id=todo_id, content=todo_id, status=status)
            item.status = status
            store.merge_updates([item])

    await asyncio.gather(
        write_status("a", TodoStatus.IN_PROGRESS),
        write_status("b", TodoStatus.PENDING),
    )

    todos = store.get_all()
    assert len(todos) == 2


def _capture_warnings(monkeypatch) -> list[str]:
    warnings: list[str] = []
    monkeypatch.setattr(
        "src.todos.store.logger.warning",
        lambda msg, *args, **kwargs: warnings.append(str(msg)),
    )
    return warnings


def test_merge_updates_warns_on_status_regression(tmp_path: Path, monkeypatch) -> None:
    """既有项 completed -> in_progress 是真回退，保留告警（软约束，不阻断）。"""
    warnings = _capture_warnings(monkeypatch)
    store = TodoStore(workspace_dir=tmp_path, session_id="sess-regress")
    store.merge_updates([TodoItem(id="1", content="同一任务", status=TodoStatus.COMPLETED)])

    store.merge_updates([TodoItem(id="1", content="同一任务", status=TodoStatus.IN_PROGRESS)])

    assert any("unusual status transition" in w for w in warnings)


def test_merge_updates_no_transition_warning_for_new_ids(tmp_path: Path, monkeypatch) -> None:
    """新 id 直接落任意状态，不参与状态机校验。"""
    warnings = _capture_warnings(monkeypatch)
    store = TodoStore(workspace_dir=tmp_path, session_id="sess-new-id")

    store.merge_updates([TodoItem(id="1", content="新任务", status=TodoStatus.IN_PROGRESS)])

    assert not any("unusual status transition" in w for w in warnings)


def test_filter_open_todos_excludes_terminal() -> None:
    todos = [
        {"id": "1", "status": "pending"},
        {"id": "2", "status": "completed"},
        {"id": "3", "status": "in_progress"},
    ]
    open_items = filter_open_todos(todos)
    assert [t["id"] for t in open_items] == ["1", "3"]


def test_build_todo_update_trace_payload_includes_optional_fields() -> None:
    payload = build_todo_update_trace_payload(
        {
            "summary": "progress",
            "todos": [{"id": "1"}],
            "changes": {"created": []},
        },
        "sess-1",
    )
    assert payload["session_id"] == "sess-1"
    assert payload["changes"] == {"created": []}

    minimal = build_todo_update_trace_payload({"summary": "", "todos": []}, "sess-2")
    assert "changes" not in minimal
