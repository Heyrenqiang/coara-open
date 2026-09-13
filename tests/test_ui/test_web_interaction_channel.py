"""Web 交互通道（哑管道）回归测试。

对齐 ApprovalCenter 帧协议——审批语义在 ApprovalCenter，WebRemoteInteractionChannel
只负责帧投递与断连重投：

- send_approval_request 把 approval_request 帧送到活跃浏览器连接并登记重发帧
- 无活跃连接 / 发送失败 → False（ApprovalCenter fail-closed，不静默放行）
- 断连只标记不取消（mark_connection_disconnected），重连后 redeliver_pending
  以同一 approval_id 重发，Center 的 future 照常等待
- send_approval_resolved 终态帧清掉重发帧
- confirm() 委托 ApprovalCenter.request（vault 提示等沿用路径）
"""

from __future__ import annotations

import pytest

from src.ui.web_interaction_channel import WebRemoteInteractionChannel
from src.ui.web_socket_registry import WebSocketRegistry


class _FakeWS:
    def __init__(self) -> None:
        self.closed = False
        self.sent: list[dict] = []

    async def send_str(self, data: str) -> None:
        import json

        self.sent.append(json.loads(data))

    async def close(self, *, code: int = 1000, message: bytes = b"") -> None:
        self.closed = True


async def _connect(reg: WebSocketRegistry, conn_id: str) -> _FakeWS:
    ws = _FakeWS()
    await reg.register(ws, conn_id)  # type: ignore[arg-type]
    return ws


def _frame(*, approval_id: str = "ap-1") -> dict:
    return {
        "approval_id": approval_id,
        "question": "执行危险操作？",
        "options": [{"label": "同意", "description": ""}, {"label": "不同意", "description": ""}],
        "timeout_s": 300,
        "workspace": "v8",
        "created_at_ms": 1_700_000_000_000,
    }


@pytest.mark.asyncio
async def test_send_approval_request_delivers_full_frame_to_active() -> None:
    reg = WebSocketRegistry()
    channel = WebRemoteInteractionChannel(reg)
    ws = await _connect(reg, "c1")

    sent = await channel.send_approval_request(_frame())

    assert sent is True
    assert len(ws.sent) == 1
    frame = ws.sent[0]
    assert frame["type"] == "approval_request"
    assert frame["approval_id"] == "ap-1"
    assert frame["question"] == "执行危险操作？"
    assert frame["options"][0]["label"] == "同意"
    assert "timeout_s" in frame and "created_at_ms" in frame


@pytest.mark.asyncio
async def test_send_request_without_active_connection_returns_false() -> None:
    reg = WebSocketRegistry()
    channel = WebRemoteInteractionChannel(reg)

    assert await channel.send_approval_request(_frame()) is False


@pytest.mark.asyncio
async def test_send_approval_resolved_clears_pending_frame() -> None:
    reg = WebSocketRegistry()
    channel = WebRemoteInteractionChannel(reg)
    ws = await _connect(reg, "c1")
    await channel.send_approval_request(_frame())
    assert channel._pending_frames  # 在途可重发

    sent = await channel.send_approval_resolved({"approval_id": "ap-1", "outcome": "approved"})

    assert sent is True
    assert ws.sent[-1]["type"] == "approval_resolved"
    assert channel._pending_frames == {}  # 终态不再重发


@pytest.mark.asyncio
async def test_disconnect_holds_frame_and_reconnect_redelivers_same_id() -> None:
    reg = WebSocketRegistry()
    channel = WebRemoteInteractionChannel(reg)
    reg.on_disconnect = channel.mark_connection_disconnected
    ws1 = await _connect(reg, "c1")

    await channel.send_approval_request(_frame())
    assert ws1.sent[0]["type"] == "approval_request"

    # 断连：帧保留（Center 的 future 继续等，超时/abort 兜底）
    await reg.unregister("c1")
    assert len(channel._pending_frames) == 1

    # 重连：同一 approval_id 重发到新活跃连接
    ws2 = await _connect(reg, "c2")
    delivered = await channel.redeliver_pending()
    assert delivered == 1
    assert ws2.sent and ws2.sent[0]["type"] == "approval_request"
    assert ws2.sent[0]["approval_id"] == "ap-1"


@pytest.mark.asyncio
async def test_confirm_delegates_to_approval_center(monkeypatch: pytest.MonkeyPatch) -> None:
    """confirm（vault 提示沿用路径）委托 ApprovalCenter.request。"""
    reg = WebSocketRegistry()
    channel = WebRemoteInteractionChannel(reg)
    calls: list[dict] = []

    class _FakeCenter:
        async def request(self, **kwargs: object) -> bool:
            calls.append(kwargs)
            return True

    monkeypatch.setattr("src.coara.approval_center.get_approval_center", lambda: _FakeCenter())

    result = await channel.confirm("允许执行吗？", [{"label": "允许", "description": ""}], timeout_seconds=12)

    assert result is True
    assert calls and calls[0]["question"] == "允许执行吗？"
    assert calls[0]["options"] == [{"label": "允许", "description": ""}]
    assert calls[0]["timeout_seconds"] == 12
