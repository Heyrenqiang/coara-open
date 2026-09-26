"""Regression: mid-turn switch must not mix traces or interrupt the origin turn."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from src.coara.event_bus import EventBus
from src.coara.root import RootCoara
from src.coara.turn_detach import foreground_workspace_matcher, iter_while_foreground
from src.core.events import TraceEvent
from src.llm.provider import LLMProvider
from src.llm.registry import provider_registry
from src.ui.trace_recording import MultiWorkspaceTracePersistence
from src.workspace.manager import WorkspaceManager


class _NoopProvider(LLMProvider):
    async def complete(self, *args, **kwargs):
        from src.llm.provider import LLMResponse

        return LLMResponse(content="")

    async def stream_complete(self, *args, **kwargs):
        from src.llm.provider import StreamChunk

        yield StreamChunk()

    def get_context_window(self, model: str | None = None) -> int:
        return 32_000

    def abort(self) -> None:
        pass

    async def close(self) -> None:
        pass


@pytest.fixture
async def two_ws_root(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "src.workspace.ephemeral.is_ephemeral_workspace_path",
        lambda _path: False,
    )
    coara_home = tmp_path / "home"
    workspace_a = tmp_path / "a"
    workspace_b = tmp_path / "b"
    workspace_a.mkdir()
    workspace_b.mkdir()

    provider_registry.register("switch-audit", _NoopProvider(name="switch-audit", api_key="x", default_model="m"))

    root = RootCoara(workspace_dir=workspace_a, provider_name="switch-audit")
    root.workspace_manager = WorkspaceManager(workspace_a, coara_home=coara_home)
    await root.workspace_manager.initialize()
    entry_a = root.workspace_manager.registry.ensure_workspace(workspace_a, name="a")
    root.workspace_manager.registry.ensure_workspace(workspace_b, name="b")
    root.workspace_manager.registry.save()
    await root.ensure_workspace_session(entry_a)
    root._foreground_session_id = entry_a.id

    original_cwd = Path.cwd()
    os.chdir(workspace_a)
    try:
        yield root, workspace_a, workspace_b, coara_home
    finally:
        os.chdir(original_cwd)


@pytest.mark.asyncio
async def test_multi_workspace_trace_routes_by_workspace_dir(tmp_path: Path) -> None:
    bus = EventBus()
    ws_a = tmp_path / "a"
    ws_b = tmp_path / "b"
    ws_a.mkdir()
    ws_b.mkdir()
    persistence = MultiWorkspaceTracePersistence(bus, ws_a, coara_home=tmp_path / "home")

    bus.publish(
        TraceEvent(
            coara_id="c",
            coara_name="n",
            event_type="tool_call",
            message="from A",
            payload={"workspace_dir": str(ws_a.resolve()), "session_id": "sa", "tool_name": "shell"},
        )
    )
    persistence.set_foreground(ws_b)
    bus.publish(
        TraceEvent(
            coara_id="c",
            coara_name="n",
            event_type="tool_call",
            message="from A after switch",
            payload={"workspace_dir": str(ws_a.resolve()), "session_id": "sa", "tool_name": "shell"},
        )
    )
    bus.publish(
        TraceEvent(
            coara_id="c",
            coara_name="n",
            event_type="tool_call",
            message="from B",
            payload={"workspace_dir": str(ws_b.resolve()), "session_id": "sb", "tool_name": "read"},
        )
    )

    store_a = persistence.store_for(ws_a)
    store_b = persistence.store_for(ws_b)
    # Foreground is B; A store must still be open and receive A's events.
    assert store_a is not store_b
    assert persistence.store.workspace_dir.resolve() == ws_b.resolve()
    persistence.close()


@pytest.mark.asyncio
async def test_busy_switch_then_b_can_process_while_a_holds_lock(two_ws_root) -> None:
    root, workspace_a, workspace_b, _home = two_ws_root
    fg_a = root.foreground_coara
    await fg_a._process_lock.acquire()
    try:
        assert fg_a.is_turn_busy() or fg_a._process_lock.locked()
        assert await root.switch_workspace("b")
        assert root.foreground_coara.workspace_dir.resolve() == workspace_b.resolve()
        # B must not be blocked by A's lock.
        assert not root.foreground_coara._process_lock.locked()
        assert fg_a._process_lock.locked()
        # Origin session still the same object / dir.
        assert fg_a.workspace_dir.resolve() == workspace_a.resolve()
    finally:
        fg_a._process_lock.release()


@pytest.mark.asyncio
async def test_iter_detach_allows_consumer_to_continue(two_ws_root) -> None:
    root, _a, _b, _home = two_ws_root
    turn_ws = root._foreground_session_id
    chunks: list[str] = []

    async def agen():
        for i in range(5):
            yield f"c{i}"
            await asyncio.sleep(0)

    async def consume():
        async for c in iter_while_foreground(
            agen(),
            foreground_workspace_matcher(root, turn_ws),
            drain_name="audit-detach",
        ):
            chunks.append(c)
            if c == "c1":
                await root.switch_workspace("b")

    await consume()
    assert chunks == ["c0", "c1"]
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_background_completion_routes_to_launching_session(two_ws_root) -> None:
    """A 发起的后台任务在切到 B 后完成：通知注入 A 的会话 不落到 B。"""
    root, _a, _b, _home = two_ws_root
    fg_a = root.foreground_coara
    assert await root.switch_workspace("b")
    fg_b = root.foreground_coara

    root._subscribe_background_task_notifications()

    root.event_bus.publish(
        TraceEvent(
            coara_id="bash-background-runner",
            coara_name="BashBackgroundRunner",
            event_type="background_task_complete",
            message="done",
            payload={
                "task_id": "bash-xyz",
                "kind": "bash",
                "status": "completed",
                "description": "demo-task",
                "session_id": fg_a.session_id,
                "coara_id": fg_a.identity.coara_id,
            },
        )
    )
    await asyncio.sleep(0.3)

    # A idle non-foreground → completion appended to A's history directly.
    assert any("bash-xyz" in str(getattr(m, "content", "")) for m in fg_a.message_history)
    assert not any("bash-xyz" in str(getattr(m, "content", "")) for m in fg_b.message_history)


@pytest.mark.asyncio
async def test_background_completion_without_stamp_falls_back_to_foreground(two_ws_root) -> None:
    """遗留无 session 标记的完成事件：维持旧行为 注入前台。"""
    root, _a, _b, _home = two_ws_root
    root._subscribe_background_task_notifications()
    fg = root.foreground_coara

    root.event_bus.publish(
        TraceEvent(
            coara_id="bash-background-runner",
            coara_name="BashBackgroundRunner",
            event_type="background_task_complete",
            message="done",
            payload={
                "task_id": "bash-legacy",
                "kind": "bash",
                "status": "completed",
                "description": "legacy-task",
            },
        )
    )
    await asyncio.sleep(0.3)

    # Foreground idle → auto-run scheduled on the normal queue.
    assert not root.scheduler._normal_queue.empty() or any(
        "bash-legacy" in str(getattr(m, "content", "")) for m in fg.message_history
    )
