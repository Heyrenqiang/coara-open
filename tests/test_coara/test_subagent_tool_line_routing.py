"""子智能体工具行按**发起端**投递（不再硬编码只投 web）。

背景：`_route_subagent_tool_line` 此前 `registry.deliver("web", ...)` 写死，
docstring 声称「手机走房间消息」但那条路不存在 → 手机端永远收不到子智能体工具行，
折叠无从谈起。现按 `_subagent_origin_source` 投给发起端。
"""

from __future__ import annotations

from types import SimpleNamespace

from src.coara.base import _route_subagent_tool_line


class _RecordingRegistry:
    def __init__(self, *, hit: bool = True) -> None:
        self.received: list[tuple[str, str, dict, str]] = []
        self._hit = hit

    def deliver(self, source: str, session_id: str, frame: dict, channel_id: str = ""):  # noqa: ANN001
        self.received.append((source, session_id, frame, channel_id))
        return SimpleNamespace(hit=self._hit, value=None)


def _subagent(*, origin_source: str, registry: object) -> SimpleNamespace:
    return SimpleNamespace(
        _session_agent_kind="subagent",
        _cli_silent=False,
        _delegate_parent_tool_call_id="tc-parent",
        _subagent_origin=(origin_source, ""),
        _delegate_parent_session_id="sess-1",
        _delegate_parent_workspace_dir="D:/ws",
        _root_ref=SimpleNamespace(end_registry=registry),
    )


def test_subagent_tool_line_goes_to_matrix_origin() -> None:
    """发起端是 matrix：子工具行必须进房间（此前被硬编码 web 挡掉）。"""
    registry = _RecordingRegistry()
    coara = _subagent(origin_source="matrix", registry=registry)

    _route_subagent_tool_line(coara, {"tool_name": "read", "tool_call_id": "c1"}, "read(a.py)")

    assert len(registry.received) == 1
    source, session_id, frame, _channel = registry.received[0]
    assert source == "matrix"
    assert session_id == "sess-1"
    assert frame["kind"] == "tool"
    assert frame["text"] == "read(a.py)"
    assert frame["tool_name"] == "read"
    # 父标识：端上据此折进发起它的 delegate 行
    assert frame["parent_tool_call_id"] == "tc-parent"


def test_subagent_tool_line_goes_to_web_origin() -> None:
    """发起端是 web：仍投 web（原行为不回退）。"""
    registry = _RecordingRegistry()
    coara = _subagent(origin_source="web", registry=registry)

    _route_subagent_tool_line(coara, {"tool_name": "read", "tool_call_id": "c1"}, "read(a.py)")

    assert registry.received[0][0] == "web"


def test_main_session_frame_skips_subagent_path() -> None:
    """主会话帧没有父标识：不走这条路（由 _route_tool_line 常规路由投递）。"""
    registry = _RecordingRegistry()
    coara = _subagent(origin_source="matrix", registry=registry)
    coara._delegate_parent_tool_call_id = ""

    _route_subagent_tool_line(coara, {"tool_name": "read"}, "read(a.py)")

    assert registry.received == []


def test_no_origin_source_skips() -> None:
    """无来源（异常/测试桩）：不投，避免凭空多出内容。"""
    registry = _RecordingRegistry()
    coara = _subagent(origin_source="", registry=registry)

    _route_subagent_tool_line(coara, {"tool_name": "read"}, "read(a.py)")

    assert registry.received == []
