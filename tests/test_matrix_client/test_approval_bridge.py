"""Matrix approval 自定义 msgtype 协议与投递测试（ApprovalCenter 单一事实源）。

- 载荷纯函数：m.coara.approval 请求卡 / m.coara.approval_resolved 终态 /
  m.coara.approval_reply 回执的 content 构造与解析
- 出站：只发回合房间（turn room），无 room / 无房间发送器 → False（fail-closed）
- 端到端：ApprovalCenter.request 经矩阵通道发出卡片，回执事件经
  handle_approval_reply_event 进 ApprovalCenter.resolve（幂等）
- 终态：超时/打断/用户已答都发 m.coara.approval_resolved 到原卡房间
- 无 standing、无文本信封：回合外无可达通道由 ApprovalCenter fail-closed
"""

from __future__ import annotations

import asyncio
import json

import pytest

from src.coara.approval_center import get_approval_center, reset_approval_center_for_tests
from src.matrix_client import approval_bridge
from src.matrix_client.approval_bridge import (
    APPROVAL_MSGTYPE_REPLY,
    APPROVAL_MSGTYPE_REQUEST,
    APPROVAL_MSGTYPE_RESOLVED,
    build_approval_request_content,
    build_approval_resolved_content,
    handle_approval_reply_event,
    parse_approval_reply_content,
    register_approval_room_sender,
)


@pytest.fixture(autouse=True)
def clean_state(monkeypatch: pytest.MonkeyPatch):
    """隔离房间发送器 / 投递房间映射 / 全局 ApprovalCenter，测试间互不污染。"""
    reset_approval_center_for_tests()
    monkeypatch.setattr(approval_bridge, "_ROOM_SENDER", None)
    monkeypatch.setattr(approval_bridge, "_APPROVAL_ROOMS", {})
    yield
    reset_approval_center_for_tests()


def _options() -> list[dict[str, str]]:
    return [{"label": "同意", "description": "继续执行"}, {"label": "不同意", "description": ""}]


def _frame(*, approval_id: str = "ap-1", question: str = "允许执行吗？") -> dict:
    return {
        "approval_id": approval_id,
        "question": question,
        "options": _options(),
        "timeout_s": 300,
        "workspace": "v8",
        "created_at_ms": 1_700_000_000_000,
    }


def _reply_content(*, approved: bool, approval_id: str | None = "ap-1", msgtype: str = APPROVAL_MSGTYPE_REPLY) -> dict:
    content: dict = {"msgtype": msgtype, "body": "approval reply", "approved": approved}
    if approval_id is not None:
        content["approval_id"] = approval_id
    return content


# ----------------------------------------------------------------------
# 载荷纯函数
# ----------------------------------------------------------------------


def test_build_approval_request_content_carries_full_payload() -> None:
    """请求卡 content：msgtype/body + approval_id/question/options/timeout_s/workspace。"""
    content = build_approval_request_content(_frame(question="允许执行：\nrm -rf /tmp/x"))
    assert content["msgtype"] == APPROVAL_MSGTYPE_REQUEST
    assert content["body"] == "允许执行：\nrm -rf /tmp/x"  # fallback 渲染 = question
    assert content["approval_id"] == "ap-1"
    assert content["timeout_s"] == 300
    assert content["workspace"] == "v8"
    assert content["created_at_ms"] == 1_700_000_000_000
    assert content["options"][0] == {"label": "同意", "description": "继续执行"}


def test_build_approval_resolved_content_carries_outcome() -> None:
    content = build_approval_resolved_content({"approval_id": "ap-1", "outcome": "timeout"})
    assert content["msgtype"] == APPROVAL_MSGTYPE_RESOLVED
    assert content["approval_id"] == "ap-1"
    assert content["outcome"] == "timeout"
    assert "timeout" in content["body"]


def test_parse_approval_reply_content_recognizes_only_reply_msgtype() -> None:
    """回执解析：只认 m.coara.approval_reply + bool approved；其它一律不解析。"""
    assert parse_approval_reply_content(_reply_content(approved=True)) == (True, "ap-1")
    assert parse_approval_reply_content(_reply_content(approved=False, approval_id="def456")) == (False, "def456")
    # 非回执 msgtype / 结构不符不消费
    assert parse_approval_reply_content(
        {"msgtype": APPROVAL_MSGTYPE_REQUEST, "approval_id": "x", "approved": True}
    ) == (
        None,
        None,
    )
    assert parse_approval_reply_content({"msgtype": APPROVAL_MSGTYPE_REPLY, "approval_id": "x"}) == (None, None)
    assert parse_approval_reply_content({"msgtype": APPROVAL_MSGTYPE_REPLY, "approved": "yes"}) == (None, None)
    assert parse_approval_reply_content(None) == (None, None)
    assert parse_approval_reply_content("普通文本") == (None, None)


# ----------------------------------------------------------------------
# 出站投递（回合房间，无 standing / 文本信封）
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deliver_request_without_turn_room_fails_closed() -> None:
    """回合外（无 turn room）发卡直接 False：审批只发回合来源端，无 standing 兜底。"""
    assert await approval_bridge.deliver_approval_request_frame(_frame()) is False


@pytest.mark.asyncio
async def test_deliver_request_without_room_sender_fails_closed() -> None:
    """房间发送器未注册（bot/runner 未启动）→ False：不静默放行。"""
    from src.coara.turn_context import reset_turn_context, set_turn_context

    tokens = set_turn_context("!room:x", send_text=lambda *a: True, source="matrix")
    try:
        assert await approval_bridge.deliver_approval_request_frame(_frame()) is False
    finally:
        reset_turn_context(tokens)


@pytest.mark.asyncio
async def test_deliver_request_sends_msgtype_to_turn_room() -> None:
    """回合内发卡：m.coara.approval content 到回合房间，并记录投递目标。"""
    from src.coara.turn_context import reset_turn_context, set_turn_context

    sent: list[tuple[str, dict]] = []

    async def ok_send(room_id: str, content: dict) -> bool:
        sent.append((room_id, content))
        return True

    register_approval_room_sender(ok_send)
    tokens = set_turn_context("!room:m", send_text=lambda *a: True, source="matrix")
    try:
        assert await approval_bridge.deliver_approval_request_frame(_frame()) is True
    finally:
        reset_turn_context(tokens)

    room_id, content = sent[0]
    assert room_id == "!room:m"
    assert content["msgtype"] == APPROVAL_MSGTYPE_REQUEST
    assert json.loads(json.dumps(content))["approval_id"] == "ap-1"  # content 全 JSON 可序列化
    assert approval_bridge._APPROVAL_ROOMS == {"ap-1": "!room:m"}


# ----------------------------------------------------------------------
# 端到端：请求发出 → 回执事件 → resolve
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_roundtrip_via_matrix_channel_resolves() -> None:
    """矩阵回合内审批：卡片发出、手机回执事件进 ApprovalCenter.resolve、请求返回。"""
    from src.coara.turn_context import reset_turn_context, set_turn_context
    from src.matrix_client.remote_channel import MATRIX_REMOTE_INTERACTION_CHANNEL

    sent: list[tuple[str, dict]] = []

    async def ok_send(room_id: str, content: dict) -> bool:
        sent.append((room_id, content))
        return True

    register_approval_room_sender(ok_send)
    center = get_approval_center()
    tokens = set_turn_context(
        "!room:y", send_text=lambda *a: True, interaction_channel=MATRIX_REMOTE_INTERACTION_CHANNEL, source="matrix"
    )
    try:

        async def reply_soon() -> None:
            await asyncio.sleep(0.05)
            # 手机端按协议回 m.coara.approval_reply 事件
            content = sent[0][1]
            assert handle_approval_reply_event(
                sender="@user:coara.local",
                bot_user_id="@coara:coara.local",
                content=_reply_content(approved=True, approval_id=content["approval_id"]),
            )

        task = asyncio.create_task(reply_soon())
        try:
            result = await center.request(question="继续？", options=_options(), timeout_seconds=5.0)
        finally:
            await task
    finally:
        reset_turn_context(tokens)

    assert result is True
    assert sent[0][1]["msgtype"] == APPROVAL_MSGTYPE_REQUEST


@pytest.mark.asyncio
async def test_timeout_sends_resolved_to_recorded_room() -> None:
    """超时 settle：终态 m.coara.approval_resolved 发回原卡房间（手机端据此置灰）。"""
    from src.coara.turn_context import reset_turn_context, set_turn_context
    from src.matrix_client.remote_channel import MATRIX_REMOTE_INTERACTION_CHANNEL

    sent: list[tuple[str, dict]] = []

    async def ok_send(room_id: str, content: dict) -> bool:
        sent.append((room_id, content))
        return True

    register_approval_room_sender(ok_send)
    center = get_approval_center()
    tokens = set_turn_context(
        "!room:t", send_text=lambda *a: True, interaction_channel=MATRIX_REMOTE_INTERACTION_CHANNEL, source="matrix"
    )
    try:
        with pytest.raises(TimeoutError):
            await center.request(question="继续？", options=_options(), timeout_seconds=0.05)
        # 终态帧由 settle 的 background task 异步发出
        for _ in range(50):
            if any(c["msgtype"] == APPROVAL_MSGTYPE_RESOLVED for _, c in sent):
                break
            await asyncio.sleep(0.01)
    finally:
        reset_turn_context(tokens)

    resolved = [(r, c) for r, c in sent if c["msgtype"] == APPROVAL_MSGTYPE_RESOLVED]
    assert resolved, f"no resolved frame sent: {sent}"
    assert resolved[0][0] == "!room:t"
    assert resolved[0][1]["outcome"] == "timeout"


@pytest.mark.asyncio
async def test_resolved_frame_reaches_original_room_after_turn() -> None:
    """回合已结束后终态帧仍按记录房间送达（不依赖当前 turn context）。"""
    from src.coara.turn_context import reset_turn_context, set_turn_context

    sent: list[tuple[str, dict]] = []

    async def ok_send(room_id: str, content: dict) -> bool:
        sent.append((room_id, content))
        return True

    register_approval_room_sender(ok_send)
    tokens = set_turn_context("!room:orig", send_text=lambda *a: True, source="matrix")
    try:
        assert await approval_bridge.deliver_approval_request_frame(_frame(approval_id="r-1")) is True
    finally:
        reset_turn_context(tokens)

    sent.clear()
    # 回合已结束（无 turn room），仍按记录房间发 resolved
    assert await approval_bridge.deliver_approval_resolved_frame({"approval_id": "r-1", "outcome": "cancelled"}) is True
    assert sent and sent[0][0] == "!room:orig"
    assert sent[0][1]["msgtype"] == APPROVAL_MSGTYPE_RESOLVED
    assert sent[0][1]["outcome"] == "cancelled"
    assert approval_bridge._APPROVAL_ROOMS == {}


# ----------------------------------------------------------------------
# 入站回执路由
# ----------------------------------------------------------------------


def test_handle_reply_ignores_non_reply_msgtype() -> None:
    """非回执 msgtype（如 bot 自己发的请求/终态 echo）不消费。"""
    assert (
        handle_approval_reply_event(
            sender="@coara:coara.local",
            bot_user_id="@coara:coara.local",
            content={"msgtype": APPROVAL_MSGTYPE_REQUEST, "approval_id": "x"},
        )
        is False
    )


def test_handle_reply_without_id_consumed() -> None:
    """msgtype 匹配但无实例 id：消费掉（不猜、不 resolve），不污染会话。"""
    assert (
        handle_approval_reply_event(
            sender="@user:coara.local",
            bot_user_id="@coara:coara.local",
            content=_reply_content(approved=True, approval_id=None),
        )
        is True
    )


def test_handle_stale_reply_consumed() -> None:
    """陈旧回执（中心无 pending 记录）：幂等路由拒绝但事件消费掉。"""
    assert (
        handle_approval_reply_event(
            sender="@user:coara.local",
            bot_user_id="@coara:coara.local",
            content=_reply_content(approved=True, approval_id="ghost-id"),
        )
        is True
    )


def test_handle_reply_resolves_active_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    """活跃审批：回执事件按 approval_id 路由进 ApprovalCenter.resolve。"""
    resolved: list[tuple[str, bool, str]] = []

    class _FakeCenter:
        def resolve(self, approval_id: str, *, approved: bool, resolved_by: str = "", actor: str = "") -> bool:
            resolved.append((approval_id, approved, resolved_by, actor))
            return True

    monkeypatch.setattr("src.coara.approval_center.get_approval_center", lambda: _FakeCenter())

    assert (
        handle_approval_reply_event(
            sender="@user:coara.local",
            bot_user_id="@coara:coara.local",
            content=_reply_content(approved=False, approval_id="ap-live"),
        )
        is True
    )
    assert resolved == [("ap-live", False, "matrix", "@user:coara.local")]


# ----------------------------------------------------------------------
# 矩阵通道 confirm 委托 ApprovalCenter（回合内端到端）
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_matrix_channel_confirm_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """矩阵通道 confirm 委托 ApprovalCenter.request：卡片发出 → 回执 → 返回决定。"""
    from src.coara.turn_context import reset_turn_context, set_turn_context
    from src.matrix_client.remote_channel import MatrixRemoteInteractionChannel

    sent: list[tuple[str, dict]] = []

    async def ok_send(room_id: str, content: dict) -> bool:
        sent.append((room_id, content))
        return True

    register_approval_room_sender(ok_send)
    channel = MatrixRemoteInteractionChannel()
    tokens = set_turn_context(
        "!room:c",
        send_text=lambda *a: True,
        interaction_channel=channel,
        source="matrix",
        actor="@user:coara.local",
    )
    try:

        async def reply_soon() -> None:
            await asyncio.sleep(0.05)
            content = sent[0][1]
            assert handle_approval_reply_event(
                sender="@intruder:coara.local",
                bot_user_id="@coara:coara.local",
                content=_reply_content(approved=True, approval_id=content["approval_id"]),
            )
            assert handle_approval_reply_event(
                sender="@user:coara.local",
                bot_user_id="@coara:coara.local",
                content=_reply_content(approved=False, approval_id=content["approval_id"]),
            )

        task = asyncio.create_task(reply_soon())
        try:
            result = await channel.confirm("继续？", _options(), timeout_seconds=5.0)
        finally:
            await task
    finally:
        reset_turn_context(tokens)

    assert result is False
