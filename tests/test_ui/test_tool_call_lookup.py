"""GET /api/v1/tool-call：按 call_id 回退检索落盘 trace，还原 ToolActivity 同构 JSON。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.core.events import TraceEvent
from src.ui.trace_store import TraceStore
from src.ui.web_server import WebServer


def _append(store: TraceStore, event_type: str, call_id: str, *, timestamp: str = "", **payload: object) -> None:
    payload.setdefault("session_id", "s1")
    if event_type == "tool_result":
        payload.setdefault("call_id", call_id)
    else:
        payload.setdefault("tool_call_id", call_id)
    kwargs: dict[str, Any] = {}
    if timestamp:
        kwargs["timestamp"] = timestamp
    store.append_event(
        TraceEvent(coara_id="c1", coara_name="root", event_type=event_type, message="", payload=dict(payload), **kwargs)
    )


def _write_full_sequence(store: TraceStore, call_id: str = "call-1") -> None:
    """tool_start → tool_complete（大输出 spill ref）→ tool_call → tool_result 全链。"""
    _append(store, "tool_start", call_id, tool_name="read", arguments={"path": "D:/ws/a.py"}, turn_id="t1")
    _append(
        store,
        "tool_complete",
        call_id,
        tool_name="read",
        turn_id="t1",
        tool_output="file body",
        tool_output_truncated=True,
        tool_output_ref="abc123",
        duration_ms=12.5,
        is_error=False,
    )
    _append(store, "tool_call", call_id, tool_name="read", arguments={"path": "D:/ws/a.py"}, is_error=False)
    _append(store, "tool_result", call_id, tool="read", summary="read done", ok=True)


def test_store_find_tool_call_events_hit_and_miss(tmp_path: Path) -> None:
    store = TraceStore(tmp_path)
    try:
        _write_full_sequence(store)
        store.flush()

        rows = store.find_tool_call_events("call-1")
        assert [r["event_type"] for r in rows] == ["tool_start", "tool_complete", "tool_call", "tool_result"]

        assert store.find_tool_call_events("nope") == []
        assert store.find_tool_call_events("") == []
    finally:
        store.close()


def test_store_find_tool_call_events_stops_at_latest_start(tmp_path: Path) -> None:
    """同一 call_id 出现两段（start 复用）时只返回最近一段。"""
    store = TraceStore(tmp_path)
    ts = "2026-09-23T10:00:0"
    try:
        _append(store, "tool_start", "call-1", tool_name="read", arguments={"path": "old"}, timestamp=f"{ts}0+00:00")
        _append(store, "tool_complete", "call-1", tool_name="read", tool_output="old out", timestamp=f"{ts}1+00:00")
        _append(store, "tool_start", "call-1", tool_name="read", arguments={"path": "new"}, timestamp=f"{ts}2+00:00")
        _append(store, "tool_complete", "call-1", tool_name="read", tool_output="new out", timestamp=f"{ts}3+00:00")
        store.flush()

        rows = store.find_tool_call_events("call-1")
        assert [r["event_type"] for r in rows] == ["tool_start", "tool_complete"]
        assert rows[0]["payload"]["arguments"] == {"path": "new"}
        assert rows[1]["payload"]["tool_output"] == "new out"
    finally:
        store.close()


def test_store_find_tool_call_events_completion_without_start(tmp_path: Path) -> None:
    """tool_start 已被挤出扫描窗口（或未发）时，收尾事件也能聚合。"""
    store = TraceStore(tmp_path)
    try:
        _append(store, "tool_complete", "call-x", tool_name="shell", tool_output="done", is_error=True)
        _append(store, "tool_result", "call-x", tool="shell", summary="failed", ok=False)
        store.flush()

        rows = store.find_tool_call_events("call-x")
        assert [r["event_type"] for r in rows] == ["tool_complete", "tool_result"]
    finally:
        store.close()


def test_aggregate_full_hit() -> None:
    """命中：聚合还原 name/args/diff/output/ref/duration/summary 全字段。"""
    rows = [
        {
            "event_type": "tool_start",
            "timestamp": "2026-09-23T10:00:00+00:00",
            "payload": {
                "tool_name": "edit",
                "tool_call_id": "call-1",
                "arguments": {"path": "a.py", "old_string": "x"},
                "turn_id": "t1",
            },
        },
        {
            "event_type": "tool_complete",
            "timestamp": "2026-09-23T10:00:01+00:00",
            "payload": {
                "tool_name": "edit",
                "tool_call_id": "call-1",
                "tool_output": "ok",
                "tool_output_truncated": True,
                "tool_output_ref": "ref123",
                "diff_lines": {"blocks": [{"kind": "edit"}]},
                "duration_ms": 33.0,
                "is_error": False,
                "turn_id": "t1",
            },
        },
        {
            "event_type": "tool_result",
            "timestamp": "2026-09-23T10:00:01+00:00",
            "payload": {"tool": "edit", "call_id": "call-1", "summary": "edit ok", "ok": True},
        },
    ]
    activity = WebServer._aggregate_tool_call_events("call-1", rows)
    assert activity is not None
    assert activity["call_id"] == "call-1"
    assert activity["tool"] == "edit"
    assert activity["done"] is True
    assert activity["args"] == {"path": "a.py", "old_string": "x"}
    assert activity["diff_lines"] == {"blocks": [{"kind": "edit"}]}
    assert activity["tool_output"] == "ok"
    assert activity["tool_output_truncated"] is True
    assert activity["tool_output_ref"] == "ref123"
    assert activity["duration_ms"] == 33.0
    assert activity["is_error"] is False
    assert activity["summary"] == "edit ok"
    assert activity["ok"] is True
    assert activity["turn_id"] == "t1"


def test_aggregate_big_output_ref_only() -> None:
    """大输出 ref 回填：tool_output 为空但带 tool_output_ref 时 ref 必须进结果。"""
    rows = [
        {
            "event_type": "tool_complete",
            "timestamp": "2026-09-23T10:00:00+00:00",
            "payload": {
                "tool_name": "shell",
                "tool_call_id": "call-big",
                "tool_output": "",
                "tool_output_ref": "deadbeef",
                "duration_ms": 900.0,
                "is_error": False,
            },
        },
    ]
    activity = WebServer._aggregate_tool_call_events("call-big", rows)
    assert activity is not None
    assert activity["tool_output_ref"] == "deadbeef"
    assert "tool_output" not in activity
    assert activity["done"] is True


def test_aggregate_tool_call_fallback_fields() -> None:
    """tool_call 兜底：无 tool_complete 时由它补 args 与输出预览（output_ref 键名不同）。"""
    rows = [
        {
            "event_type": "tool_call",
            "timestamp": "2026-09-23T10:00:00+00:00",
            "payload": {
                "tool_name": "grep",
                "tool_call_id": "call-2",
                "call_id": "call-2",
                "arguments": {"pattern": "foo"},
                "tool_output": "preview…",
                "is_error": False,
                "output_ref": "ref-2",
            },
        },
    ]
    activity = WebServer._aggregate_tool_call_events("call-2", rows)
    assert activity is not None
    assert activity["args"] == {"pattern": "foo"}
    assert activity["tool_output"] == "preview…"
    assert activity["tool_output_ref"] == "ref-2"


def test_aggregate_empty_returns_none() -> None:
    """未命中：无任何事件时返回 None（接口层据此回 404）。"""
    assert WebServer._aggregate_tool_call_events("missing", []) is None
