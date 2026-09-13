"""trace/用量展示层并发修复回归（REMAINING_ISSUES #27/#28）。"""

from __future__ import annotations

import threading
from pathlib import Path

from src.core.events import TraceEvent
from src.ui.dashboard_handlers import DashboardRestHandlers
from src.ui.trace_store import TraceStore


def _append_event(store: TraceStore, *, content: str, session_id: str = "s1") -> None:
    store.append_event(
        TraceEvent(
            coara_id="c1",
            coara_name="root",
            event_type="conversation_message",
            message=content,
            payload={"session_id": session_id, "role": "user", "content": content},
        )
    )


def _message_rows(count: int) -> list[dict]:
    return [
        {
            "coara_id": "c1",
            "coara_name": "root",
            "session_id": f"s{i}",
            "role": "user",
            "content": f"m{i}",
            "timestamp": f"2026-01-01T00:00:{i:02d}+00:00",
            "turn_id": f"t{i}",
        }
        for i in range(count)
    ]


def test_build_sessions_concurrent_with_reset(tmp_path: Path) -> None:
    """#28：_build_sessions 与 reset_session_cache 并发不报错、会话列表无重复。"""
    store = TraceStore(tmp_path)
    try:
        messages = _message_rows(10)
        events: list[dict] = []

        errors: list[BaseException] = []

        def _builder() -> None:
            try:
                for _ in range(50):
                    sessions = store._build_sessions(messages, events)
                    keys = [(s["coara_id"], s["session_id"]) for s in sessions]
                    assert len(keys) == len(set(keys))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def _resetter() -> None:
            try:
                for _ in range(50):
                    store.reset_session_cache()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=_builder) for _ in range(3)]
        threads.append(threading.Thread(target=_resetter))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert not errors
    finally:
        store.close()


def test_incremental_read_appends_and_truncation(tmp_path: Path) -> None:
    """#27：增量读只读新增段；截断分支全量重读且游标正确。"""
    store = TraceStore(tmp_path)
    try:
        handlers = DashboardRestHandlers.__new__(DashboardRestHandlers)
        handlers.store = store

        _append_event(store, content="m1")
        store.flush()
        rows, size = handlers._read_jsonl_incremental(store.events_path, [], 0)
        assert [row["message"] for row in rows] == ["m1"]

        # 尺寸未变 → 直接复用缓存
        same_rows, same_size = handlers._read_jsonl_incremental(store.events_path, rows, size)
        assert same_rows is rows and same_size == size

        # 增量：只读到新增行并与缓存拼接
        _append_event(store, content="m2")
        store.flush()
        rows, size = handlers._read_jsonl_incremental(store.events_path, rows, size)
        assert [row["message"] for row in rows] == ["m1", "m2"]

        # 截断：外部覆写使文件变小 → 全量重读 + 重置会话缓存
        with store.io_lock:
            first_line = store.events_path.read_text(encoding="utf-8").splitlines()[0]
            store.events_path.write_text(first_line + "\n", encoding="utf-8")
        rows, new_size = handlers._read_jsonl_incremental(store.events_path, rows, size)
        assert [row["message"] for row in rows] == ["m1"]
        assert new_size == store.events_path.stat().st_size
    finally:
        store.close()
