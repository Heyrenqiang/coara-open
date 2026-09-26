"""子智能体帧不得落进主会话流：三端出口 + 内核产出侧的四道防线。

回归背景（2026-09-25）：子智能体正文以 ``kind=chunk``、不带父标识的形态落进 web 会话
视图带，被读端投影成主消息气泡。根因两层——① web 的跟话/唤醒 sender 各写了一份窄化
映射（只认 diff/tool，其余一律当正文），绕过了唯一出口 ``_emit_end_frame``；② 内核
产出侧按「父标识是否有值」判身份，父标识缺失时就产出无父标正文。

本文件锁住四道防线：
- 内核产出侧：子智能体实例的正文一律不走主会话通道；缺父标识时直接丢弃
- web 出口：子智能体帧缺父标识 → 丢弃，不进主流
- CLI 出口：子智能体帧（含缺父标识）不进主滚动区；带父标识的普通正文折进折叠块
- 手机出口：子智能体帧缺父标识 → 不投房间
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from src.coara.base import CoaraBase
from src.coara.end_registry import EndRegistry
from src.matrix_client.response_stream import SUBAGENT_ENVELOPE_PREFIX, dispatch_matrix_end_frame
from src.ui.attach_ws import _attach_output_frame


class _RecordingStream:
    def __init__(self) -> None:
        self.frames: list[tuple[str, dict]] = []

    def emit(self, kind: str, **payload: Any) -> None:
        self.frames.append((kind, payload))


def _subagent(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "session_id": "sa-sess",
        "identity": SimpleNamespace(user_facing=False, coara_id="c-1"),
        "_session_agent_kind": "subagent",
        "_delegate_parent_tool_call_id": "call-delegate-1",
        "_delegate_parent_session_id": "parent-sess",
        "_delegate_parent_workspace_dir": "D:\\ws",
        "_delegate_subagent_id": "sa-1",
        "_subagent_origin": ("web", "conn-1"),
        "_active_turn_source": "web",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_route_chunk_to_current_end_blocks_subagent_from_main_stream() -> None:
    """内核产出侧：子智能体实例的正文绝不按主会话 chunk 投递。"""
    registry = EndRegistry()
    frames: list[dict] = []
    registry.register("web", lambda f: frames.append(f), "sa-sess")
    sub = _subagent()
    sub._root_ref = SimpleNamespace(end_registry=registry)

    asyncio.run(CoaraBase._route_chunk_to_current_end(sub, "子智能体正文", "web"))

    assert frames == []


def test_flow_node_chunk_still_goes_main_stream() -> None:
    """flow 节点不是 delegate 产物：同为 user_facing=False，但正文必须照旧按主流投递。

    回归：``_session_agent_kind`` 对非 user_facing / 非 owner 的实例默认推导为 "subagent"，
    FlowCoordinator 建的节点正是如此（录像带标记 session_tape="flow"）；若按实例类型一刀切
    当成子智能体，节点正文会因为没有 delegate 父行而被丢弃。
    """
    registry = EndRegistry()
    frames: list[dict] = []
    registry.register("web", lambda f: frames.append(f), "sa-flow-n1")
    node = SimpleNamespace(
        _session_agent_kind="subagent",
        _session_tape="flow",
        _delegate_parent_tool_call_id="",
        _delegate_subagent_id="",
        _cli_silent=False,
        session_id="sa-flow-n1",
        workspace_dir="D:\\ws",
        _segments=SimpleNamespace(source="web", channel_id="", current=None),
        _active_turn_source="web",
        _root_ref=SimpleNamespace(end_registry=registry),
    )

    asyncio.run(CoaraBase._route_chunk_to_current_end(node, "节点正文", "web"))

    assert [f["kind"] for f in frames] == ["chunk"]
    assert frames[0]["text"] == "节点正文"


def test_route_subagent_chunk_drops_without_parent_id() -> None:
    """内核产出侧：缺父标识时宁可丢弃，也不退化成主会话正文。"""
    registry = EndRegistry()
    frames: list[dict] = []
    registry.register("web", lambda f: frames.append(f), "parent-sess")
    sub = _subagent(_delegate_parent_tool_call_id="")
    sub._root_ref = SimpleNamespace(end_registry=registry)

    asyncio.run(CoaraBase._route_subagent_chunk(sub, "子智能体正文"))

    assert frames == []


def test_route_subagent_chunk_folds_with_parent_id() -> None:
    """对照：父标识在时照常投给发起端并带上折叠凭据。"""
    registry = EndRegistry()
    frames: list[dict] = []
    registry.register("web", lambda f: frames.append(f), "parent-sess")
    sub = _subagent()
    sub._root_ref = SimpleNamespace(end_registry=registry)

    asyncio.run(CoaraBase._route_subagent_chunk(sub, "子智能体正文"))

    assert [f["kind"] for f in frames] == ["subagent_chunk"]
    assert frames[0]["parent_tool_call_id"] == "call-delegate-1"


def test_web_end_frame_drops_parentless_subagent_frames() -> None:
    """web 出口：缺父标识的子智能体帧丢弃，绝不落成 chunk。"""
    from src.ui.web_server import WebServer

    stream = _RecordingStream()
    WebServer._emit_end_frame(stream, {"kind": "subagent_chunk", "text": "过程旁白"})
    WebServer._emit_end_frame(stream, {"kind": "subagent_result", "text": "最终报告"})
    assert stream.frames == []

    WebServer._emit_end_frame(stream, {"kind": "subagent_chunk", "text": "过程旁白", "tool_call_id": "c1"})
    assert [k for k, _ in stream.frames] == ["subagent_chunk"]


def test_web_end_frame_folds_plain_chunk_with_parent() -> None:
    """web 出口：带父标识的普通正文折进 delegate 行，不进主流。"""
    from src.ui.web_server import WebServer

    stream = _RecordingStream()
    WebServer._emit_end_frame(stream, {"kind": "chunk", "text": "子智能体正文", "parent_tool_call_id": "c1"})

    assert [k for k, _ in stream.frames] == ["subagent_chunk"]
    assert stream.frames[0][1]["tool_call_id"] == "c1"


def test_cli_attach_frame_keeps_subagent_out_of_main_scroll() -> None:
    """CLI 出口：子智能体帧进折叠通道；缺凭据直接丢；带父标识的正文也折。"""
    assert _attach_output_frame({"kind": "subagent_chunk", "text": "过程旁白", "tool_call_id": "c1"}) == (
        "subagent_chunk",
        {
            "text": "过程旁白",
            "agent_kind": "subagent",
            "tool_call_id": "c1",
            "coara_id": "",
            "subagent_id": "",
        },
    )
    assert _attach_output_frame({"kind": "subagent_result", "text": "报告"}) is None
    assert _attach_output_frame({"kind": "chunk", "text": "正文", "parent_tool_call_id": "c1"}) == (
        "subagent_chunk",
        {"text": "正文", "agent_kind": "subagent", "tool_call_id": "c1"},
    )
    assert _attach_output_frame({"kind": "chunk", "text": "主会话正文"}) == ("chunk", {"text": "主会话正文"})


def test_matrix_end_frame_drops_parentless_subagent_frames() -> None:
    """手机出口：缺父标识的子智能体帧不投房间；带父标识封 [COARA_SUBAGENT]。"""
    sent: list[str] = []

    async def _send(room_id: str, text: str) -> None:
        sent.append(text)

    asyncio.run(
        dispatch_matrix_end_frame({"kind": "subagent_chunk", "text": "过程旁白"}, room_id="r", send_chunk=_send)
    )
    assert sent == []

    asyncio.run(
        dispatch_matrix_end_frame(
            {"kind": "subagent_result", "text": "报告", "parent_tool_call_id": "c1"},
            room_id="r",
            send_chunk=_send,
        )
    )
    assert len(sent) == 1
    assert sent[0].startswith(SUBAGENT_ENVELOPE_PREFIX)
