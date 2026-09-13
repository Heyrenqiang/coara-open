"""工具/回合类 trace 按端在发送侧过滤；全局 topic 不受 source 门禁。"""

from __future__ import annotations

from types import SimpleNamespace

from src.ui.trace_broadcast import TraceBroadcastHandlers


class _H(TraceBroadcastHandlers):
    def __init__(self) -> None:
        self._module_roots: dict = {}

    def _normalize_tool_ws_fields(self, event_type: str, payload: dict, event_payload: dict) -> None:
        return None


def _event(etype: str, *, source: str = "", session_id: str = "s1", **extra) -> SimpleNamespace:
    payload = {"session_id": session_id, **extra}
    if source:
        payload["source"] = source
    return SimpleNamespace(event_type=etype, payload=payload, coara_id="c1", message="")


def test_attach_drops_web_and_matrix_tool_start() -> None:
    h = _H()
    view = SimpleNamespace(session_id="s1", workspace_dir="")
    assert h._serialize_trace_event(_event("tool_start", source="web"), view, loose=True) is None
    assert h._serialize_trace_event(_event("tool_start", source="matrix"), view, loose=True) is None
    assert h._serialize_trace_event(_event("tool_start", source=""), view, loose=True) is None


def test_attach_keeps_cli_tool_events() -> None:
    h = _H()
    view = SimpleNamespace(session_id="s1", workspace_dir="")
    for src in ("cli", "cli-attached"):
        for et in ("tool_start", "tool_complete"):
            out = h._serialize_trace_event(_event(et, source=src), view, loose=True)
            assert out is not None and out["type"] == et


def test_browser_drops_cli_and_matrix_tools() -> None:
    h = _H()
    view = SimpleNamespace(session_id="s1", workspace_dir="")
    assert h._serialize_trace_event(_event("tool_complete", source="cli"), view, loose=False) is None
    assert h._serialize_trace_event(_event("tool_start", source="matrix"), view, loose=False) is None
    assert h._serialize_trace_event(_event("tool_start", source="web"), view, loose=False) is not None


def test_attach_drops_other_end_turn_and_thinking() -> None:
    h = _H()
    view = SimpleNamespace(session_id="s1", workspace_dir="")
    for et in ("turn_start", "turn_end", "thinking_progress", "llm_request_start", "user_message"):
        assert h._serialize_trace_event(_event(et, source="web"), view, loose=True) is None
        assert h._serialize_trace_event(_event(et, source="matrix"), view, loose=True) is None
        assert h._serialize_trace_event(_event(et, source="cli-attached"), view, loose=True) is not None


def test_attach_uses_origin_source_for_background_agent() -> None:
    h = _H()
    view = SimpleNamespace(session_id="s1", workspace_dir="")
    assert (
        h._serialize_trace_event(
            _event("background_agent_complete", origin_source="web"),
            view,
            loose=True,
        )
        is None
    )
    assert (
        h._serialize_trace_event(
            _event("background_agent_complete", origin_source="cli-attached"),
            view,
            loose=True,
        )
        is not None
    )


def test_attach_allows_subagent_loop_tools_without_source_when_workspace_matches() -> None:
    """子智能体未继承父 source 时：origin_scope=subagent_loop + 同空间仍进 attach。"""
    h = _H()
    view = SimpleNamespace(session_id="sess-1", workspace_dir="D:/ws/a")
    out = h._serialize_trace_event(
        _event(
            "tool_complete",
            source="",
            session_id="sa-xyz",
            workspace_dir="D:/ws/a",
            origin_scope="subagent_loop",
        ),
        view,
        loose=True,
    )
    assert out is not None and out["type"] == "tool_complete"


def test_global_topics_ignore_source() -> None:
    """会话起止等空间级事件：无 source 也发给 attach（仍受 pin 会话门禁）。"""
    h = _H()
    view = SimpleNamespace(session_id="s1", workspace_dir="")
    for et in ("session_started", "session_auto_new"):
        assert h._serialize_trace_event(_event(et), view, loose=True) is not None


def test_llm_switched_only_matching_workspace_attach() -> None:
    h = _H()
    view = SimpleNamespace(session_id="s1", workspace_dir="")
    # 同 session → 放行
    out = h._serialize_trace_event(
        _event("llm_switched", source="", provider="kimi", model="k3", session_id="s1"),
        view,
        loose=True,
    )
    assert out is not None and out.get("provider") == "kimi"
    # 其它会话 → 不推
    assert (
        h._serialize_trace_event(
            _event("llm_switched", provider="kimi", model="k3", session_id="other"),
            view,
            loose=True,
        )
        is None
    )


def test_workspace_switched_not_in_attach_topics() -> None:
    assert "workspace_switched" not in TraceBroadcastHandlers._ATTACH_TRACE_TOPICS
    from src.ui.trace_broadcast import _ATTACH_SKIP_TRACE_TYPES

    assert "workspace_switched" in _ATTACH_SKIP_TRACE_TYPES


def test_flow_lifecycle_is_end_scoped() -> None:
    h = _H()
    view = SimpleNamespace(session_id="s1", workspace_dir="")
    assert h._serialize_trace_event(_event("flow_started", source="web"), view, loose=True) is None
    assert h._serialize_trace_event(_event("flow_started", source="cli-attached"), view, loose=True) is not None
    assert h._serialize_trace_event(_event("process_spawned", source="matrix"), view, loose=True) is None


def test_flow_graph_changed_not_in_attach_topics() -> None:
    assert "flow_graph_changed" not in TraceBroadcastHandlers._ATTACH_TRACE_TOPICS


def test_cross_view_close_events_dropped_for_other_workspace() -> None:
    """P1-3：切走视图后迟到的关行事件带发起空间归属，按「其它空间」直接丢弃——
    不标 detached 复活已随切视图清掉的活动树行；同空间则跨视图放行关行。"""
    h = _H()
    view = SimpleNamespace(session_id="s1", workspace_dir="D:/ws/new")
    for et in ("subagent_complete", "subagent_failed", "background_agent_complete"):
        # 归属其它空间（sid 是旧空间会话）→ 丢（不 detached）
        out = h._serialize_trace_event(
            _event(et, source="web", session_id="sess-old", workspace_dir="D:/ws/old"),
            view,
            loose=False,
        )
        assert out is None, et
        # 同空间（视图内 sid 不等也放行关行）→ 放行
        out = h._serialize_trace_event(
            _event(et, source="web", session_id="sa-1", workspace_dir="D:/ws/new"),
            view,
            loose=False,
        )
        assert out is not None and "detached" not in out, et


def test_retract_requires_source_and_is_in_allowlists() -> None:
    """P0-1 注册表级不变量：chat_turn_retracted 是端作用域事件，无 source 会被
    门禁严格丢弃——ws.py 的 retract payload 必须带 source，且两端放行清单都在。"""
    from src.ui.trace_broadcast import _END_SCOPED_TRACE_TYPES

    assert "chat_turn_retracted" in _END_SCOPED_TRACE_TYPES
    assert "chat_turn_retracted" in TraceBroadcastHandlers._ATTACH_TRACE_TOPICS
    h = _H()
    view = SimpleNamespace(session_id="s1", workspace_dir="")
    # 无 source：浏览器严格丢弃 / attach 宽松（非 subagent_loop）也丢弃
    assert h._serialize_trace_event(_event("chat_turn_retracted", source=""), view, loose=False) is None
    assert h._serialize_trace_event(_event("chat_turn_retracted", source=""), view, loose=True) is None
    # 带 source：本端放行
    assert h._serialize_trace_event(_event("chat_turn_retracted", source="web"), view, loose=False) is not None
    assert h._serialize_trace_event(_event("chat_turn_retracted", source="cli"), view, loose=True) is not None
