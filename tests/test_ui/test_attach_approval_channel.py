"""Attach 审批通道（AttachRemoteInteractionChannel）的回归测试。

对齐 ApprovalCenter 帧协议（通道是哑管道，语义在 ApprovalCenter）：
- 服务端按 conn_id 定向投递 approval_request 帧（approval_id/question/options），
  不再复用浏览器 registry，也不持有 pending future（future 在 ApprovalCenter）。
- 客户端 approval_reply 回执经 attach_ws 路由进 ApprovalCenter.resolve
  （幂等终态转换；通道本身不解析回执）。
- 断连挂起不取消：重连后按 conn_id 回放 pending_for_connection 重发帧。
- 发送失败返回 False → ApprovalCenter fail-closed（不静默放行）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.ui.attach_interaction_channel import AttachRemoteInteractionChannel


def _make_channel(*, send_ok: bool = True) -> tuple[AttachRemoteInteractionChannel, AsyncMock]:
    """构造通道 + 捕获发送帧的 mock attach_registry。"""
    server = MagicMock()
    server.attach_registry = MagicMock()
    server.attach_registry.send_to = AsyncMock(return_value=send_ok)
    return AttachRemoteInteractionChannel(server), server.attach_registry.send_to


def _frame(*, approval_id: str = "ap-1") -> dict:
    return {
        "approval_id": approval_id,
        "question": "执行危险操作？",
        "options": [{"label": "同意", "description": ""}, {"label": "不同意", "description": ""}],
        "timeout_s": 300,
        "workspace": "v8",
        "created_at_ms": 1_700_000_000_000,
    }


class TestDelivery:
    @pytest.mark.asyncio
    async def test_send_approval_request_delivers_to_connection(self) -> None:
        """approval_request 帧定向发到指定 conn_id（带 approval_id/question/options）。"""
        channel, send_to = _make_channel()
        bound = channel.for_connection("conn-a")

        sent = await bound.send_approval_request(_frame())

        assert sent is True
        conn_id, frame = send_to.call_args[0]
        assert conn_id == "conn-a"
        assert frame["type"] == "approval_request"
        assert frame["approval_id"] == "ap-1"
        assert frame["question"] == "执行危险操作？"
        assert frame["options"][0]["label"] == "同意"

    @pytest.mark.asyncio
    async def test_send_request_failure_returns_false(self) -> None:
        """连接已断（send_to 返回 False）→ 返回 False，由 ApprovalCenter fail-closed。"""
        channel, _ = _make_channel(send_ok=False)
        bound = channel.for_connection("conn-gone")
        assert await bound.send_approval_request(_frame()) is False

    @pytest.mark.asyncio
    async def test_send_approval_resolved_clears_pending_frame(self) -> None:
        """终态帧送达后清掉本通道重发帧（Center 侧状态由终态广播收口）。"""
        channel, send_to = _make_channel()
        bound = channel.for_connection("conn-a")

        assert await bound.send_approval_request(_frame()) is True
        assert channel.pending_for_connection("conn-a")  # 在途可重发

        resolved = await bound.send_approval_resolved({"approval_id": "ap-1", "outcome": "approved"})

        assert resolved is True
        resolved_frame = send_to.call_args[0][1]
        assert resolved_frame["type"] == "approval_resolved"
        assert channel.pending_for_connection("conn-a") == []  # 已终态不再重发


def _channel_with_server(*, send_ok: bool = True) -> tuple[AttachRemoteInteractionChannel, MagicMock]:
    """通道 + 其 server 替身（需改 send_to 行为时用，如模拟重发失败）。"""
    server = MagicMock()
    server.attach_registry = MagicMock()
    server.attach_registry.send_to = AsyncMock(return_value=send_ok)
    return AttachRemoteInteractionChannel(server), server


class TestRedelivery:
    @pytest.mark.asyncio
    async def test_redeliver_resends_same_approval_id_to_resumed_connection(self) -> None:
        """重连回放：按同一 approval_id 重发本连接挂起帧（未终态）。"""
        channel, server = _channel_with_server()
        await channel.send_approval_request(_frame(), conn_id="conn-a")
        server.attach_registry.send_to.reset_mock()

        delivered = await channel.redeliver_pending("conn-a")

        assert delivered == 1
        conn_id, frame = server.attach_registry.send_to.call_args[0]
        assert conn_id == "conn-a"
        assert frame["type"] == "approval_request"
        assert frame["approval_id"] == "ap-1"
        assert frame["question"] == "执行危险操作？"

    @pytest.mark.asyncio
    async def test_redeliver_isolates_other_connections(self) -> None:
        """只重发本连接的帧，它端挂起帧绝不被顶给本连接。"""
        channel, server = _channel_with_server()
        await channel.send_approval_request(_frame(approval_id="ap-a"), conn_id="conn-a")
        await channel.send_approval_request(_frame(approval_id="ap-b"), conn_id="conn-b")
        server.attach_registry.send_to.reset_mock()

        assert await channel.redeliver_pending("conn-b") == 1

        conn_id, frame = server.attach_registry.send_to.call_args[0]
        assert conn_id == "conn-b" and frame["approval_id"] == "ap-b"

    @pytest.mark.asyncio
    async def test_redeliver_skips_settled_frames(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """已终态帧不重发（不弹回已作废的审批），并清出重发表。"""
        rebound: list[tuple[str, str]] = []

        class _FakeCenter:
            def get(self, approval_id: str) -> MagicMock:
                return MagicMock(state="terminal")

            def rebind_reply_actor(self, approval_id: str, actor: str) -> bool:
                rebound.append((approval_id, actor))
                return True

        monkeypatch.setattr("src.coara.approval_center.get_approval_center", lambda: _FakeCenter())

        channel, server = _channel_with_server()
        await channel.send_approval_request(_frame(), conn_id="conn-a")
        server.attach_registry.send_to.reset_mock()

        assert await channel.redeliver_pending("conn-a") == 0
        server.attach_registry.send_to.assert_not_awaited()
        assert rebound == []
        assert channel.pending_for_connection("conn-a") == []

    @pytest.mark.asyncio
    async def test_redeliver_drops_frame_when_connection_unreachable(self) -> None:
        """重发失败（对端不可达）即清帧，不反复空发。"""
        channel, server = _channel_with_server(send_ok=False)
        await channel.send_approval_request(_frame(), conn_id="conn-a")
        server.attach_registry.send_to.reset_mock()

        assert await channel.redeliver_pending("conn-a") == 0
        assert channel.pending_for_connection("conn-a") == []


class TestConnectionLifecycle:
    @pytest.mark.asyncio
    async def test_pending_for_connection_lists_only_that_connection(self) -> None:
        """重连回放只取本连接的挂起帧（多连接隔离）。"""
        channel, _ = _make_channel()
        for cid, aid in (("conn-a", "ap-a"), ("conn-b", "ap-b"), ("conn-a", "ap-a2")):
            await channel.send_approval_request(_frame(approval_id=aid), conn_id=cid)
        frames = channel.pending_for_connection("conn-a")
        assert sorted(f["approval_id"] for f in frames) == ["ap-a", "ap-a2"]

    @pytest.mark.asyncio
    async def test_drop_frames_for_connection_clears_only_that_connection(self) -> None:
        """连接显式注销：清本连接重发帧，其它连接不受影响（Center 侧由超时/abort 收口）。"""
        channel, _ = _make_channel()
        await channel.send_approval_request(_frame(approval_id="ap-a"), conn_id="conn-a")
        await channel.send_approval_request(_frame(approval_id="ap-b"), conn_id="conn-b")

        channel.drop_frames_for_connection("conn-a")

        assert channel.pending_for_connection("conn-a") == []
        assert [f["approval_id"] for f in channel.pending_for_connection("conn-b")] == ["ap-b"]


class TestServerSideRouting:
    @pytest.mark.asyncio
    async def test_approval_reply_frame_resolves_in_center(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """attach_ws 收到 approval_reply 帧 → ApprovalCenter.resolve（幂等终态）。"""
        from src.ui.attach_ws import AttachWsHandlers

        resolved: list[tuple[str, bool, str]] = []

        class _FakeCenter:
            def resolve(self, approval_id: str, *, approved: bool, resolved_by: str = "", actor: str = "") -> bool:
                resolved.append((approval_id, approved, resolved_by, actor))
                return True

        monkeypatch.setattr("src.coara.approval_center.get_approval_center", lambda: _FakeCenter())

        channel, _ = _make_channel()
        handler = AttachWsHandlers.__new__(AttachWsHandlers)
        handler.attach_interaction_channel = channel
        conn = MagicMock()
        conn.workspace_id = "ws-a"
        conn.conn_id = "conn-a"
        handler.attach_registry = MagicMock()
        handler.attach_registry._connections = {"conn-a": conn}

        await handler._handle_attach_message(
            {"type": "approval_reply", "approval_id": "ap-1", "approved": True},
            MagicMock(),
            "conn-a",
        )

        assert resolved == [("ap-1", True, "cli-attached", "conn-a")]

    @pytest.mark.asyncio
    async def test_approval_reply_without_id_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """无 approval_id 的回执帧不消费（不猜、不误配）。"""
        from src.ui.attach_ws import AttachWsHandlers

        called: list[tuple] = []

        class _FakeCenter:
            def resolve(self, approval_id: str, **kwargs: object) -> bool:
                called.append((approval_id, kwargs))
                return True

        monkeypatch.setattr("src.coara.approval_center.get_approval_center", lambda: _FakeCenter())

        channel, _ = _make_channel()
        handler = AttachWsHandlers.__new__(AttachWsHandlers)
        handler.attach_interaction_channel = channel
        conn = MagicMock()
        conn.workspace_id = "ws-a"
        conn.conn_id = "conn-a"
        handler.attach_registry = MagicMock()
        handler.attach_registry._connections = {"conn-a": conn}

        await handler._handle_attach_message(
            {"type": "approval_reply", "approved": True},
            MagicMock(),
            "conn-a",
        )

        assert called == []
