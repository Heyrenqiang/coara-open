"""Unit tests for the attach (coara attach) connection registry."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from src.ui.attach_registry import AttachRegistry


def _make_ws(*, closed: bool = False) -> AsyncMock:
    ws = AsyncMock()
    ws.closed = closed
    ws.send_str = AsyncMock()
    ws.close = AsyncMock()
    return ws


@pytest.mark.asyncio
async def test_register_occupies_workspace_and_unregister_releases() -> None:
    reg = AttachRegistry()
    ws = _make_ws()
    conn = await reg.register(ws, "c1", "ws-a")
    assert conn is not None
    assert reg.is_workspace_occupied("ws-a") is True
    assert reg.workspace_owner("ws-a") == "c1"

    await reg.unregister("c1")
    assert reg.is_workspace_occupied("ws-a") is False
    assert reg.workspace_owner("ws-a") is None


@pytest.mark.asyncio
async def test_second_attach_on_same_workspace_allowed() -> None:
    """同空间允许多条 attach；各连接独立，按 conn_id 定向回投。"""
    reg = AttachRegistry()
    first = await reg.register(_make_ws(), "c1", "ws-a")
    assert first is not None
    second = await reg.register(_make_ws(), "c2", "ws-a")
    assert second is not None
    assert set(reg.workspace_owners("ws-a")) == {"c1", "c2"}
    assert reg.is_workspace_occupied("ws-a") is True
    # 不同空间仍可挂
    third = await reg.register(_make_ws(), "c3", "ws-b")
    assert third is not None


@pytest.mark.asyncio
async def test_send_to_routes_per_connection_same_workspace() -> None:
    """同空间两条 attach：帧只回给指定连接。"""
    reg = AttachRegistry()
    ws1 = _make_ws()
    ws2 = _make_ws()
    await reg.register(ws1, "c1", "ws-a")
    await reg.register(ws2, "c2", "ws-a")

    ok = await reg.send_to("c1", {"type": "chunk", "text": "hi"})
    assert ok is True
    ws1.send_str.assert_awaited_once()
    ws2.send_str.assert_not_awaited()

    await reg.unregister("c1")
    assert reg.workspace_owners("ws-a") == ["c2"]
    assert reg.is_workspace_occupied("ws-a") is True


@pytest.mark.asyncio
async def test_occupied_workspace_reusable_after_disconnect() -> None:
    """外挂 CLI 断连释放占用后，同空间可被新的 attach 占用。"""
    reg = AttachRegistry()
    await reg.register(_make_ws(), "c1", "ws-a")
    await reg.unregister("c1")
    conn = await reg.register(_make_ws(), "c2", "ws-a")
    assert conn is not None
    assert reg.workspace_owner("ws-a") == "c2"


@pytest.mark.asyncio
async def test_send_to_routes_per_connection() -> None:
    """按发起连接路由：每帧只回给指定连接，不广播到其它 attach。"""
    reg = AttachRegistry()
    ws1 = _make_ws()
    ws2 = _make_ws()
    await reg.register(ws1, "c1", "ws-a")
    await reg.register(ws2, "c2", "ws-b")

    ok = await reg.send_to("c1", {"type": "chunk", "text": "hi"})
    assert ok is True
    ws1.send_str.assert_awaited_once()
    ws2.send_str.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_to_closed_connection_unregisters() -> None:
    """发送时连接已关闭 → 自动注销并释放占用。"""
    reg = AttachRegistry()
    ws = _make_ws(closed=True)
    await reg.register(ws, "c1", "ws-a")
    ok = await reg.send_to("c1", {"type": "chunk"})
    assert ok is False
    assert reg.is_workspace_occupied("ws-a") is False


@pytest.mark.asyncio
async def test_send_failure_unregisters_and_releases() -> None:
    """发送抛连接错误 → 自动注销并释放占用。"""
    reg = AttachRegistry()
    ws = _make_ws()
    ws.send_str.side_effect = ConnectionResetError("gone")
    await reg.register(ws, "c1", "ws-a")
    ok = await reg.send_to("c1", {"type": "chunk"})
    assert ok is False
    assert reg.is_workspace_occupied("ws-a") is False


@pytest.mark.asyncio
async def test_send_to_nowait_noop_for_unknown_or_closed() -> None:
    """fire-and-forget 版：未知/已关连接静默跳过，不抛。"""
    reg = AttachRegistry()
    reg.send_to_nowait("nope", {"type": "chunk"})
    ws = _make_ws(closed=True)
    await reg.register(ws, "c1", "ws-a")
    reg.send_to_nowait("c1", {"type": "chunk"})
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_close_all_clears_everything() -> None:
    reg = AttachRegistry()
    await reg.register(_make_ws(), "c1", "ws-a")
    await reg.register(_make_ws(), "c2", "ws-b")
    await reg.close_all()
    assert reg.is_workspace_occupied("ws-a") is False
    assert reg.is_workspace_occupied("ws-b") is False


@pytest.mark.asyncio
async def test_rebind_workspace_moves_occupancy() -> None:
    reg = AttachRegistry()
    await reg.register(_make_ws(), "c1", "ws-a")
    assert await reg.rebind_workspace("c1", "ws-b") is True
    assert reg.is_workspace_occupied("ws-a") is False
    assert reg.workspace_owner("ws-b") == "c1"
    assert reg._connections["c1"].workspace_id == "ws-b"


@pytest.mark.asyncio
async def test_rebind_after_unregister_does_not_leak_occupancy() -> None:
    """连接已注销时 rebind 失败且不占用目标空间。"""
    reg = AttachRegistry()
    assert await reg.rebind_workspace("gone", "ws-b") is False
    assert reg.is_workspace_occupied("ws-b") is False


@pytest.mark.asyncio
async def test_rebind_releases_new_occupancy_if_conn_gone_mid_flight() -> None:
    """acquire 成功后发现连接已注销 → 吐回新占用。"""
    reg = AttachRegistry()
    await reg.register(_make_ws(), "c1", "ws-a")

    original_acquire = reg.occupancy.acquire

    async def acquire_then_unregister(workspace_id: str, route):  # type: ignore[no-untyped-def]
        ok = await original_acquire(workspace_id, route)
        if workspace_id == "ws-b" and ok:
            await reg.unregister("c1")
        return ok

    reg.occupancy.acquire = acquire_then_unregister  # type: ignore[method-assign]
    assert await reg.rebind_workspace("c1", "ws-b") is False
    assert reg.is_workspace_occupied("ws-b") is False
    assert reg.is_workspace_occupied("ws-a") is False
