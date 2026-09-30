"""Per-connection WS outbox: bounded queue, drop-oldest, need_topup coalesce."""

from __future__ import annotations

import asyncio
import json

import pytest

from src.ui.attach_registry import AttachRegistry
from src.ui.web_socket_registry import WebSocketRegistry
from src.ui.ws_outbox import NEED_TOPUP_TYPE, WsOutbox


@pytest.mark.asyncio
async def test_outbox_drains_in_order_under_cap() -> None:
    sent: list[dict] = []
    gate = asyncio.Event()
    gate.set()

    async def send(msg: dict) -> bool:
        await gate.wait()
        sent.append(msg)
        return True

    box = WsOutbox(max_size=8)
    for i in range(5):
        box.enqueue("c1", {"type": "chunk", "n": i}, send)
    await asyncio.sleep(0.05)
    assert [m["n"] for m in sent] == [0, 1, 2, 3, 4]
    assert box.pending("c1") == 0
    assert box.has_gap("c1") is False


@pytest.mark.asyncio
async def test_outbox_full_drops_oldest_and_emits_need_topup() -> None:
    sent: list[dict] = []
    release = asyncio.Event()

    async def send(msg: dict) -> bool:
        await release.wait()
        sent.append(msg)
        return True

    box = WsOutbox(max_size=3)
    for i in range(5):
        box.enqueue("c1", {"type": "chunk", "n": i}, send)
    # Queue holds 3; two oldest dropped → gap. Drain blocked on release.
    assert box.pending("c1") == 3
    assert box.has_gap("c1") is True
    release.set()
    await asyncio.sleep(0.05)
    nums = [m["n"] for m in sent if m.get("type") == "chunk"]
    assert nums == [2, 3, 4]
    assert any(m.get("type") == NEED_TOPUP_TYPE for m in sent)
    # need_topup after data frames (coalesced once queue empty)
    assert sent[-1]["type"] == NEED_TOPUP_TYPE


@pytest.mark.asyncio
async def test_outbox_single_drain_task_under_burst() -> None:
    """Burst enqueue must not spawn one task per frame (the old ensure_future bug)."""
    in_flight = 0
    max_in_flight = 0
    release = asyncio.Event()

    async def send(msg: dict) -> bool:
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await release.wait()
        in_flight -= 1
        return True

    box = WsOutbox(max_size=64)
    for i in range(40):
        box.enqueue("c1", {"type": "chunk", "n": i}, send)
    await asyncio.sleep(0)  # let drain start
    assert max_in_flight == 1
    release.set()
    await asyncio.sleep(0.05)
    assert max_in_flight == 1


@pytest.mark.asyncio
async def test_web_registry_nowait_uses_outbox_and_need_topup() -> None:
    class _FakeWS:
        def __init__(self) -> None:
            self.closed = False
            self.payloads: list[str] = []
            self.gate = asyncio.Event()

        async def send_str(self, s: str) -> None:
            await self.gate.wait()
            self.payloads.append(s)

    reg = WebSocketRegistry()
    # Shrink outbox for the test
    reg._outbox = WsOutbox(max_size=2)
    ws = _FakeWS()
    await reg.register(ws, "active")  # type: ignore[arg-type]

    for i in range(4):
        reg.send_to_active_nowait({"type": "chunk", "n": i})
    assert reg._outbox.pending("active") == 2
    assert reg._outbox.has_gap("active") is True
    ws.gate.set()
    await asyncio.sleep(0.05)
    types = [json.loads(p)["type"] for p in ws.payloads]
    assert "need_topup" in types
    chunks = [json.loads(p)["n"] for p in ws.payloads if json.loads(p).get("type") == "chunk"]
    assert chunks == [2, 3]


@pytest.mark.asyncio
async def test_attach_registry_nowait_outbox() -> None:
    class _FakeWS:
        def __init__(self) -> None:
            self.closed = False
            self.payloads: list[str] = []

        async def send_str(self, s: str) -> None:
            self.payloads.append(s)

    reg = AttachRegistry()
    reg._outbox = WsOutbox(max_size=2)
    ws = _FakeWS()
    await reg.register(ws, "c1", "ws-a")  # type: ignore[arg-type]
    for i in range(4):
        reg.send_to_nowait("c1", {"type": "chunk", "n": i})
    await asyncio.sleep(0.05)
    msgs = [json.loads(p) for p in ws.payloads]
    assert any(m.get("type") == "need_topup" for m in msgs)
    assert [m["n"] for m in msgs if m.get("type") == "chunk"] == [2, 3]


@pytest.mark.asyncio
async def test_unregister_clears_outbox() -> None:
    box_holder: list[WsOutbox] = []

    class _FakeWS:
        closed = False

        async def send_str(self, s: str) -> None:
            await asyncio.sleep(10)

        async def close(self, **_kw: object) -> None:
            self.closed = True

    reg = WebSocketRegistry()
    box_holder.append(reg._outbox)
    await reg.register(_FakeWS(), "c1")  # type: ignore[arg-type]
    reg.send_to_active_nowait({"type": "chunk", "n": 1})
    assert reg._outbox.pending("c1") >= 0
    await reg.unregister("c1")
    assert reg._outbox.pending("c1") == 0
