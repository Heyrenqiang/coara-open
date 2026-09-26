"""单标签守卫：新连接 register 时以 close(4000) 顶掉旧活跃连接（P1-2）。

被顶替标签凭 4000 不自动重连，杜绝两标签互顶竞态；顶替不 unregister
旧连接（由其 WS handler finally 正常注销），active 立即切到新连接。
"""

from __future__ import annotations

import pytest

from src.ui.web_socket_registry import WebSocketRegistry


class _FakeWS:
    def __init__(self) -> None:
        self.closed = False
        self.close_calls: list[tuple[int, bytes]] = []

    async def close(self, *, code: int = 1000, message: bytes = b"") -> None:
        self.closed = True
        self.close_calls.append((code, message))


@pytest.mark.asyncio
async def test_register_supersedes_old_active_with_close_4000() -> None:
    reg = WebSocketRegistry()
    old_ws, new_ws = _FakeWS(), _FakeWS()
    await reg.register(old_ws, "old")  # type: ignore[arg-type]
    assert reg.active_connection is not None and reg.active_connection.conn_id == "old"

    await reg.register(new_ws, "new")  # type: ignore[arg-type]

    assert old_ws.closed is True
    assert old_ws.close_calls and old_ws.close_calls[0][0] == 4000
    assert new_ws.closed is False
    # 旧连接不 unregister（WS handler finally 负责），active 已切到新连接
    assert reg.active_connection is not None and reg.active_connection.conn_id == "new"
    assert reg.get_connection("old") is not None


@pytest.mark.asyncio
async def test_register_same_conn_id_does_not_close_itself() -> None:
    reg = WebSocketRegistry()
    ws = _FakeWS()
    await reg.register(ws, "c1")  # type: ignore[arg-type]
    await reg.register(ws, "c1")  # type: ignore[arg-type]
    assert ws.closed is False


@pytest.mark.asyncio
async def test_register_skips_already_closed_old_connection() -> None:
    reg = WebSocketRegistry()
    old_ws, new_ws = _FakeWS(), _FakeWS()
    await reg.register(old_ws, "old")  # type: ignore[arg-type]
    old_ws.closed = True  # 已关闭但 handler finally 尚未 unregister
    await reg.register(new_ws, "new")  # type: ignore[arg-type]
    assert old_ws.close_calls == []
    assert reg.active_connection is not None and reg.active_connection.conn_id == "new"
