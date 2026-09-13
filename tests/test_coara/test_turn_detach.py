"""Tests for mid-turn workspace detach helper."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from src.coara.turn_detach import foreground_workspace_matcher, iter_while_foreground


@pytest.mark.asyncio
async def test_iter_while_foreground_drains_after_switch() -> None:
    items = ["a", "b", "c", "d"]
    seen: list[str] = []
    drained = asyncio.Event()
    foreground = {"id": "ws-a"}

    async def agen():
        for item in items:
            yield item
            await asyncio.sleep(0)

    def still() -> bool:
        return foreground["id"] == "ws-a"

    async def on_detach() -> None:
        drained.set()

    async def consume() -> None:
        async for chunk in iter_while_foreground(
            agen(),
            still,
            drain_name="test-detach",
            on_detach=on_detach,
        ):
            seen.append(chunk)
            if chunk == "b":
                foreground["id"] = "ws-b"

    await consume()
    assert seen == ["a", "b"]
    await asyncio.wait_for(drained.wait(), timeout=1.0)
    # Allow drain task to finish consuming c,d
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_foreground_workspace_matcher() -> None:
    root: Any = type("R", (), {"_foreground_session_id": "a"})()
    still = foreground_workspace_matcher(root, "a")
    assert still() is True
    root._foreground_session_id = "b"
    assert still() is False


@pytest.mark.asyncio
async def test_iter_while_foreground_reattach_forwards_to_sink() -> None:
    """Switch-away discards drained chunks; switching back mid-drain re-attaches.

    Regression: CLI lost all tool lines and text when the user switched away
    and back while a turn was still running (drain discarded everything, and
    remote_sync skips foreground cli-source chunks).
    """
    items = ["a", "b", "c", "d", "e"]
    seen: list[str] = []
    sunk: list[str] = []
    drain_done = asyncio.Event()
    foreground = {"id": "ws-a"}
    release = asyncio.Event()

    async def agen():
        for item in items:
            yield item
            await asyncio.sleep(0)
        # Hold the generator open until the test flips foreground back.
        await release.wait()
        yield "f"

    def still() -> bool:
        return foreground["id"] == "ws-a"

    async def consume() -> None:
        async for chunk in iter_while_foreground(
            agen(),
            still,
            drain_name="test-reattach",
            on_detached_item=sunk.append,
            on_drain_complete=drain_done.set,
        ):
            seen.append(chunk)
            if chunk == "b":
                foreground["id"] = "ws-b"

    await consume()
    assert seen == ["a", "b"]
    # While away: drained items are discarded, not forwarded to the sink.
    await asyncio.sleep(0.05)
    assert sunk == []
    # Switch back mid-drain: remaining chunks flow to the sink again.
    foreground["id"] = "ws-a"
    release.set()
    await asyncio.wait_for(drain_done.wait(), timeout=1.0)
    assert sunk == ["f"]


@pytest.mark.asyncio
async def test_iter_while_foreground_detaches_during_silence() -> None:
    """静默回合（无 chunk 到达）切走后也要 detach，消费方及时让位。

    Regression: 旧实现只在 chunk 到达时检查前台状态——长工具/等模型首包的
    静默期主循环卡死在 async-for 里，切走后新前台空间的输入（含 /model 等
    斜杠命令）堆在队列里永远不执行（「/model 没反应」实锤）。
    """
    release = asyncio.Event()
    drain_done = asyncio.Event()
    foreground = {"id": "ws-a"}
    sunk: list[str] = []
    seen: list[str] = []

    async def agen():
        yield "a"
        # 长时间静默：直到测试放行才产出后续 chunk
        await release.wait()
        yield "b"

    def still() -> bool:
        return foreground["id"] == "ws-a"

    async def consume() -> None:
        async for chunk in iter_while_foreground(
            agen(),
            still,
            drain_name="test-silence-detach",
            on_detached_item=sunk.append,
            on_drain_complete=drain_done.set,
        ):
            seen.append(chunk)

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    assert seen == ["a"]
    # 切走：没有任何 chunk 到达，消费方也必须在轮询间隔内 detach 返回。
    foreground["id"] = "ws-b"
    await asyncio.wait_for(consumer, timeout=2.0)
    assert consumer.done()
    # 切回后放行：drain 接管挂起的 anext，后续 chunk 转发到 sink。
    foreground["id"] = "ws-a"
    release.set()
    await asyncio.wait_for(drain_done.wait(), timeout=1.0)
    assert sunk == ["b"]


@pytest.mark.asyncio
async def test_iter_while_foreground_silence_detach_after_turn_end() -> None:
    """静默期 detach 后回合恰好结束：drain 接管时发现 agen 已耗尽，完成钩子照常配对。"""
    release = asyncio.Event()
    drain_done = asyncio.Event()
    foreground = {"id": "ws-a"}
    seen: list[str] = []

    async def agen():
        yield "a"
        # 静默期被 detach；放行后回合直接结束（不再产出 chunk）
        await release.wait()

    def still() -> bool:
        return foreground["id"] == "ws-a"

    async def consume() -> None:
        async for chunk in iter_while_foreground(
            agen(),
            still,
            drain_name="test-silence-end",
            on_drain_complete=drain_done.set,
        ):
            seen.append(chunk)

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    foreground["id"] = "ws-b"
    await asyncio.wait_for(consumer, timeout=2.0)
    release.set()
    # drain 接管的挂起 anext 收到 StopAsyncIteration：on_drain_complete 仍触发一次。
    await asyncio.wait_for(drain_done.wait(), timeout=1.0)
    assert seen == ["a"]
