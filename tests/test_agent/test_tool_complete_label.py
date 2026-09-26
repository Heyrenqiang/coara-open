"""工具完成 payload：失败行须带错误首行，段归属优先于发起端快照。"""

from __future__ import annotations

from types import SimpleNamespace

from src.agent.executor import ToolExecutor
from src.core.tool_base import ToolResult
from src.core.types import ToolCall


def _coara(*, seg_source: str = "web", launch_source: str = "web") -> SimpleNamespace:
    return SimpleNamespace(
        session_id="sess-1",
        identity=SimpleNamespace(coara_id="root", user_facing=True),
        _segments=SimpleNamespace(source=seg_source) if seg_source else None,
        _active_turn_source=launch_source,
        _active_turn=SimpleNamespace(turn_id="turn-1"),
        _session_log=None,
        _cli_silent=False,
    )


def test_error_tool_label_appends_first_line() -> None:
    payload = ToolExecutor()._build_tool_complete_payload(
        coara=_coara(),
        tool=SimpleNamespace(name="shell"),
        tool_call=ToolCall(id="c1", name="shell", arguments={"command": "make"}),
        effective_call=ToolCall(id="c1", name="shell", arguments={"command": "make"}),
        result=ToolResult.error("exit 1\nstack omitted"),
        duration_ms=12.0,
        cache_hit=False,
        raw_output_text="",
        source="cli-attached",
    )
    assert payload["is_error"] is True
    assert payload["tool_label"].startswith("shell - make")
    assert "报错: `exit 1`" in payload["tool_label"]
    assert "stack" not in payload["tool_label"]


def test_ok_tool_label_has_no_error_suffix() -> None:
    payload = ToolExecutor()._build_tool_complete_payload(
        coara=_coara(),
        tool=SimpleNamespace(name="read"),
        tool_call=ToolCall(id="c2", name="read", arguments={"path": "a.py"}),
        effective_call=ToolCall(id="c2", name="read", arguments={"path": "a.py"}),
        result=ToolResult.success("ok"),
        duration_ms=1.0,
        cache_hit=False,
        raw_output_text="ok",
        source="web",
    )
    assert payload["is_error"] is False
    assert payload["tool_label"] == "read - a.py"
    assert "报错" not in payload["tool_label"]


def test_payload_source_comes_from_caller() -> None:
    """端归属由调用方带入（工具开始那一刻锁定的值），payload 不自行重算。"""
    payload = ToolExecutor()._build_tool_complete_payload(
        coara=_coara(seg_source="matrix", launch_source="web"),
        tool=SimpleNamespace(name="read"),
        tool_call=ToolCall(id="c3", name="read", arguments={"path": "b.py"}),
        effective_call=ToolCall(id="c3", name="read", arguments={"path": "b.py"}),
        result=ToolResult.success("ok"),
        duration_ms=1.0,
        cache_hit=False,
        raw_output_text="ok",
        source="cli-attached",
    )
    # 会话当前段是 matrix，但本调用开始时就锁定了 cli-attached —— 用锁定值，不重算
    assert payload["source"] == "cli-attached"
