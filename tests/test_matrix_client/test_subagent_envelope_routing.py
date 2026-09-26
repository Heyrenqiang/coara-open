"""手机链路帧出口：子智能体内容必须封 [COARA_SUBAGENT]（折进 delegate 行，绝不落成主会话气泡）。

回归对象（2026-09-25）：
- 跟话通道（``register_matrix_followup_end_channel``）曾自带一份更窄的帧分支：带父标识的
  子智能体过程正文/结果被当正文直发房间，手机上显示成主会话气泡。
- 跟话 sender 曾无条件顶替 (matrix, session) 槽位，把在跑回合的活性通道挤掉。
- 主通道折叠判据曾只认 ``kind in ("subagent_chunk","subagent_result")``，实例重建后以
  ``kind="chunk"`` 带父标的子智能体正文会退化成正文。

两条通道现在共用 ``dispatch_matrix_end_frame``，此处直测该函数 + 跟话注册守卫。
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from src.matrix_client.ingress_helpers import register_matrix_followup_end_channel
from src.matrix_client.response_stream import build_matrix_subagent_envelope, dispatch_matrix_end_frame

SUB_PREFIX = "[COARA_SUBAGENT]"
TOOL_PREFIX = "[COARA_TOOL]"


def _payload(envelope: str, prefix: str = SUB_PREFIX) -> dict:
    assert envelope.startswith(prefix)
    return json.loads(envelope[len(prefix) :])


async def _collect() -> tuple[list[str], object]:
    sent: list[str] = []

    async def send_chunk(_room: str, body: str) -> None:
        sent.append(body)

    return sent, send_chunk


def test_build_subagent_envelope_carries_kind_text_parent() -> None:
    payload = _payload(build_matrix_subagent_envelope("subagent_result", "结果正文", "delegate-1"))
    assert payload == {"kind": "subagent_result", "text": "结果正文", "parent_tool_call_id": "delegate-1"}
    # 端上解析要求 kind 非空（kind 已退化时也要能落进折叠区）
    assert _payload(build_matrix_subagent_envelope("", "x", "d1"))["kind"] == "subagent_chunk"


async def test_dispatch_envelopes_child_frames_whatever_the_kind() -> None:
    sent, send_chunk = await _collect()
    # 子智能体过程正文（kind=chunk 是实例重建后的退化形态）与最终结果都必须封信封
    await dispatch_matrix_end_frame(
        {"kind": "chunk", "text": "过程正文", "parent_tool_call_id": "delegate-1"},
        room_id="!r",
        send_chunk=send_chunk,
    )
    await dispatch_matrix_end_frame(
        {"kind": "subagent_result", "text": "最终结果", "parent_tool_call_id": "delegate-1"},
        room_id="!r",
        send_chunk=send_chunk,
    )
    assert len(sent) == 2
    assert _payload(sent[0])["parent_tool_call_id"] == "delegate-1"
    assert _payload(sent[0])["text"] == "过程正文"
    assert _payload(sent[1])["kind"] == "subagent_result"


async def test_dispatch_plain_text_and_prefix() -> None:
    sent, send_chunk = await _collect()
    await dispatch_matrix_end_frame({"kind": "chunk", "text": "主会话正文"}, room_id="!r", send_chunk=send_chunk)
    await dispatch_matrix_end_frame(
        {"kind": "chunk", "text": "切空间后正文"},
        room_id="!r",
        send_chunk=send_chunk,
        body_prefix=lambda: "[shop] ",
    )
    await dispatch_matrix_end_frame({"kind": "chunk", "text": "   "}, room_id="!r", send_chunk=send_chunk)
    assert sent == ["主会话正文", "[shop] 切空间后正文"]


async def test_dispatch_drops_injected_envelopes() -> None:
    sent, send_chunk = await _collect()
    await dispatch_matrix_end_frame(
        {"kind": "chunk", "text": "<后台结果>任务完成</后台结果>"},
        room_id="!r",
        send_chunk=send_chunk,
    )
    assert sent == []


async def test_dispatch_tool_frame_becomes_tool_envelope() -> None:
    sent, send_chunk = await _collect()
    await dispatch_matrix_end_frame(
        {"kind": "tool", "text": "read(a.py)", "tool_name": "read", "tool_call_id": "c1"},
        room_id="!r",
        send_chunk=send_chunk,
    )
    assert len(sent) == 1
    assert _payload(sent[0], TOOL_PREFIX)["label"] == "read(a.py)"


async def test_followup_sender_uses_the_shared_dispatch() -> None:
    root = MagicMock()
    root.end_registry.sender_for.return_value = None
    coara = MagicMock()
    coara.session_id = "s1"
    sent: list[str] = []

    async def send_text(_room: str, body: str) -> None:
        sent.append(body)

    register_matrix_followup_end_channel(root, coara, room_id="!r:local", send_text=send_text)
    sender = root.end_registry.register.call_args.args[1]

    await sender({"kind": "chunk", "text": "子智能体过程", "parent_tool_call_id": "delegate-1"})
    await sender({"kind": "chunk", "text": "主会话正文"})

    assert sent[0].startswith(SUB_PREFIX)
    assert sent[1] == "主会话正文"


async def test_followup_does_not_steal_running_turn_sender() -> None:
    class _LiveTurnSender:
        """在跑回合的活性 sender：没有跟话标。"""

    root = MagicMock()
    root.end_registry.sender_for.return_value = _LiveTurnSender()
    # 槽位上有通道、且它是回合主通道（不是跟话）——守卫（注册表类型）据此拒绝顶替
    root.end_registry.has.return_value = True
    root.end_registry.is_followup.return_value = False
    coara = MagicMock()
    coara.session_id = "s1"

    async def send_text(_room: str, body: str) -> None:  # pragma: no cover - 不会被调用
        return None

    register_matrix_followup_end_channel(root, coara, room_id="!r:local", send_text=send_text)

    root.end_registry.register.assert_not_called()
    root.end_registry.unregister.assert_not_called()


async def test_followup_replaces_previous_followup_sender() -> None:
    class _FollowupSender:
        _end_matrix_followup = True

    previous = _FollowupSender()
    root = MagicMock()
    root.end_registry.sender_for.return_value = previous
    # 槽位上是上一个跟话通道（注册表里的类型判据）——允许被新的跟话顶替
    root.end_registry.has.return_value = True
    root.end_registry.is_followup.return_value = True
    coara = MagicMock()
    coara.session_id = "s1"

    async def send_text(_room: str, body: str) -> None:  # pragma: no cover - 不会被调用
        return None

    register_matrix_followup_end_channel(root, coara, room_id="!r:local", send_text=send_text)

    root.end_registry.unregister.assert_called_once_with("matrix", previous, "s1")
    assert root.end_registry.register.call_count == 1
