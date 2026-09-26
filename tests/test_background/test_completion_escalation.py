"""Background task completion escalation: dead launching session falls back to foreground."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from src.coara.root import RootCoara
from src.core.events import TraceEvent
from src.llm.provider import LLMProvider
from src.llm.registry import provider_registry
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
async def single_ws_root(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "src.workspace.ephemeral.is_ephemeral_workspace_path",
        lambda _path: False,
    )
    coara_home = tmp_path / "home"
    workspace = tmp_path / "ws"
    workspace.mkdir()

    provider_registry.register("bg-escalation", _NoopProvider(name="bg-escalation", api_key="x", default_model="m"))

    root = RootCoara(workspace_dir=workspace, provider_name="bg-escalation")
    root.workspace_manager = WorkspaceManager(workspace, coara_home=coara_home)
    await root.workspace_manager.initialize()
    entry = root.workspace_manager.registry.ensure_workspace(workspace, name="ws")
    root.workspace_manager.registry.save()
    await root.ensure_workspace_session(entry)
    root._foreground_session_id = entry.id

    original_cwd = Path.cwd()
    os.chdir(workspace)
    try:
        yield root, workspace
    finally:
        os.chdir(original_cwd)


@pytest.mark.asyncio
async def test_notifications_wire_runners_to_event_bus(single_ws_root) -> None:
    """接线回归（P0）：订阅后 BashBackgroundRunner / BackgroundAgentManager 拿到 EventBus。

    曾把 set_event_bus 误嵌进唤醒方法，真实完成事件在生产路径上发不出来。
    """
    from src.background.bash_runner import BashBackgroundRunner
    from src.coara.background_agent import BackgroundAgentManager

    root, _ws = single_ws_root
    # 单例跨测试共享：先清空再订阅，断言接线发生在本次调用内
    BashBackgroundRunner().set_event_bus(None)
    BackgroundAgentManager().set_event_bus(None)

    root._subscribe_background_task_notifications()

    assert BashBackgroundRunner()._event_bus is root.event_bus
    assert BackgroundAgentManager()._event_bus is root.event_bus


@pytest.mark.asyncio
async def test_completion_awakens_background_turn(single_ws_root) -> None:
    """空闲会话完成 → 真正跑 process_message(source=background)，不绕过 runner。"""
    root, _ws = single_ws_root
    root._subscribe_background_task_notifications()
    fg = root.foreground_coara

    started: list[str] = []
    orig = fg.process_message

    async def _spy(content, **kwargs):
        started.append(str(kwargs.get("source") or ""))
        async for chunk in orig(content, **kwargs):
            yield chunk

    fg.process_message = _spy  # type: ignore[method-assign]

    root.event_bus.publish(
        TraceEvent(
            coara_id="bash-background-runner",
            coara_name="BashBackgroundRunner",
            event_type="background_task_complete",
            message="done",
            payload={
                "task_id": "bash-wake",
                "kind": "bash",
                "status": "completed",
                "description": "demo-task",
                "session_id": fg.session_id,
                "coara_id": fg.identity.coara_id,
            },
        )
    )
    await asyncio.sleep(1.0)

    # 唤醒回合确实以 source=background 跑起来了
    assert "background" in started
    # 完成通知进驻历史（process_message 内部 append）
    assert any("bash-wake" in str(getattr(m, "content", "")) for m in fg.message_history)


@pytest.mark.asyncio
async def test_busy_web_completion_injected_via_continuation(single_ws_root) -> None:
    """web 忙时与其它端同口径：接续注入，不进收件箱。"""
    root, workspace = single_ws_root
    root._subscribe_background_task_notifications()
    fg = root.foreground_coara
    fg._inside_turn = True
    fg._last_user_input_source = "web"

    root.event_bus.publish(
        TraceEvent(
            coara_id="bash-background-runner",
            coara_name="BashBackgroundRunner",
            event_type="background_task_complete",
            message="done",
            payload={
                "task_id": "bash-web-busy",
                "kind": "bash",
                "status": "completed",
                "description": "demo-task",
                "session_id": fg.session_id,
                "coara_id": fg.identity.coara_id,
            },
        )
    )
    await asyncio.sleep(0.3)

    assert any("bash-web-busy" in str(getattr(i, "text", "")) for i in fg._continuation_inputs)
    from src.workspace.updates.store import WorkspaceUpdatesStore

    store = WorkspaceUpdatesStore(workspace.parent / "home", registry=root.workspace_manager.registry)
    assert store.list_messages(workspace="ws", status="all") == []
    fg._inside_turn = False
    fg._continuation_inputs.clear()


@pytest.mark.asyncio
async def test_busy_cli_completion_still_injected_via_continuation(single_ws_root) -> None:
    """对照组：回复端是 CLI 的忙时会话照旧走接续注入（裁决只收窄 web）。"""
    root, _ws = single_ws_root
    root._subscribe_background_task_notifications()
    fg = root.foreground_coara
    fg._inside_turn = True
    fg._last_user_input_source = "cli"

    root.event_bus.publish(
        TraceEvent(
            coara_id="bash-background-runner",
            coara_name="BashBackgroundRunner",
            event_type="background_task_complete",
            message="done",
            payload={
                "task_id": "bash-cli-busy",
                "kind": "bash",
                "status": "completed",
                "description": "demo-task",
                "session_id": fg.session_id,
                "coara_id": fg.identity.coara_id,
            },
        )
    )
    await asyncio.sleep(0.3)

    assert any("bash-cli-busy" in str(getattr(i, "text", "")) for i in fg._continuation_inputs)
    fg._inside_turn = False
    fg._continuation_inputs.clear()


@pytest.mark.asyncio
async def test_completion_from_dead_session_escalates_to_foreground(single_ws_root) -> None:
    """子智能体发起的任务在其收官后完成：发起会话已消亡 通知升级投主会话 不丢弃。"""
    root, _ws = single_ws_root
    root._subscribe_background_task_notifications()
    fg = root.foreground_coara

    root.event_bus.publish(
        TraceEvent(
            coara_id="bash-background-runner",
            coara_name="BashBackgroundRunner",
            event_type="background_task_complete",
            message="done",
            payload={
                "task_id": "bash-dead-launcher",
                "kind": "bash",
                "status": "completed",
                "description": "demo-task",
                # 已消亡的子智能体会话标记 任何活 session 都匹配不上
                "session_id": "session-gone",
                "coara_id": "coara-gone",
            },
        )
    )
    await asyncio.sleep(0.3)

    assert any("bash-dead-launcher" in str(getattr(m, "content", "")) for m in fg.message_history)


@pytest.mark.asyncio
async def test_completion_from_live_session_still_routes_to_origin(single_ws_root) -> None:
    """活会话标记不受影响：完成通知仍注入发起会话自身。"""
    root, _ws = single_ws_root
    root._subscribe_background_task_notifications()
    fg = root.foreground_coara

    root.event_bus.publish(
        TraceEvent(
            coara_id="bash-background-runner",
            coara_name="BashBackgroundRunner",
            event_type="background_task_complete",
            message="done",
            payload={
                "task_id": "bash-live-launcher",
                "kind": "bash",
                "status": "completed",
                "description": "demo-task",
                "session_id": fg.session_id,
                "coara_id": fg.identity.coara_id,
            },
        )
    )
    await asyncio.sleep(0.3)

    assert any("bash-live-launcher" in str(getattr(m, "content", "")) for m in fg.message_history)


@pytest.mark.asyncio
async def test_advisor_completion_awakens_like_other_background_tasks(single_ws_root) -> None:
    """aide 与 coaras 同机制：后台收官与其它后台任务一样唤醒新回合（不再静默进驻历史）。"""
    root, _ws = single_ws_root
    root._subscribe_background_task_notifications()
    fg = root.foreground_coara

    started: list[str] = []
    orig = fg.process_message

    async def _spy(content, **kwargs):
        started.append(str(kwargs.get("source") or ""))
        async for chunk in orig(content, **kwargs):
            yield chunk

    fg.process_message = _spy  # type: ignore[method-assign]

    root.event_bus.publish(
        TraceEvent(
            coara_id="background-agent-manager",
            coara_name="BackgroundAgentManager",
            event_type="background_task_complete",
            message="done",
            payload={
                "task_id": "sa-advisor-park",
                "kind": "agent",
                "subagent_type": "aide",
                "status": "completed",
                "description": "调研任务",
                "result_full": "调研结果正文",
                "session_id": fg.session_id,
                "coara_id": fg.identity.coara_id,
            },
        )
    )
    await asyncio.sleep(0.3)

    # 与其它后台任务一致：唤醒 background 回合，完成通知进驻历史
    assert "background" in started
    assert any("sa-advisor-park" in str(getattr(m, "content", "")) for m in fg.message_history)


@pytest.mark.asyncio
async def test_completion_does_not_write_updates_inbox(single_ws_root) -> None:
    """后台任务完成只回发起会话历史 不再产生工作空间动态收件箱条目"""
    from src.workspace.updates.store import WorkspaceUpdatesStore

    root, workspace = single_ws_root
    root._subscribe_background_task_notifications()
    fg = root.foreground_coara

    root.event_bus.publish(
        TraceEvent(
            coara_id="bash-background-runner",
            coara_name="BashBackgroundRunner",
            event_type="background_task_complete",
            message="done",
            payload={
                "task_id": "bash-no-inbox",
                "kind": "bash",
                "status": "failed",
                "has_error": True,
                "description": "demo-task",
                "session_id": fg.session_id,
                "coara_id": fg.identity.coara_id,
            },
        )
    )
    await asyncio.sleep(0.3)

    # 会话历史通道保留
    assert any("bash-no-inbox" in str(getattr(m, "content", "")) for m in fg.message_history)
    # 收件箱无新条目（即使失败也不再落）
    store = WorkspaceUpdatesStore(workspace.parent / "home")
    assert store.list_messages(workspace="ws", status="all") == []


@pytest.mark.asyncio
async def test_completion_busy_during_deferred_new_flushes_after(single_ws_root) -> None:
    """延迟 /new 进行中：先挂起；清理结束后再注入（不进收件箱）。"""
    from src.workspace.updates.store import WorkspaceUpdatesStore

    root, workspace = single_ws_root
    root._subscribe_background_task_notifications()
    fg = root.foreground_coara

    gate = asyncio.Event()
    fg._deferred_new_session_task = asyncio.create_task(gate.wait())

    queued_before = len(fg._continuation_inputs)
    root.event_bus.publish(
        TraceEvent(
            coara_id="bash-background-runner",
            coara_name="BashBackgroundRunner",
            event_type="background_task_complete",
            message="done",
            payload={
                "task_id": "bash-deferred-new",
                "kind": "bash",
                "status": "completed",
                "description": "demo-task",
                "session_id": fg.session_id,
                "coara_id": fg.identity.coara_id,
            },
        )
    )
    await asyncio.sleep(0.2)

    # 清理未结束：尚未注入
    assert len(fg._continuation_inputs) == queued_before
    assert not any("bash-deferred-new" in str(getattr(m, "content", "")) for m in fg.message_history)
    store = WorkspaceUpdatesStore(workspace.parent / "home", registry=root.workspace_manager.registry)
    assert store.list_messages(workspace="ws", status="all") == []

    # 结束延迟清理（会话仍忙）→ 接续注入
    fg._inside_turn = True
    gate.set()
    await asyncio.sleep(0.3)
    assert any("bash-deferred-new" in str(getattr(i, "text", "")) for i in fg._continuation_inputs)
    assert store.list_messages(workspace="ws", status="all") == []

    fg._deferred_new_session_task = None
    fg._inside_turn = False
    fg._continuation_inputs.clear()


@pytest.mark.asyncio
async def test_awaken_during_deferred_new_flushes_after(single_ws_root) -> None:
    """延迟 /new 进行中 + 会话空闲：清理结束后再唤醒（不进收件箱）。"""
    from src.workspace.updates.store import WorkspaceUpdatesStore

    root, workspace = single_ws_root
    root._subscribe_background_task_notifications()
    fg = root.foreground_coara

    gate = asyncio.Event()
    fg._deferred_new_session_task = asyncio.create_task(gate.wait())

    started: list[str] = []
    orig = fg.process_message

    async def _spy(content, **kwargs):
        started.append(str(kwargs.get("source") or ""))
        async for chunk in orig(content, **kwargs):
            yield chunk

    fg.process_message = _spy  # type: ignore[method-assign]

    root.event_bus.publish(
        TraceEvent(
            coara_id="bash-background-runner",
            coara_name="BashBackgroundRunner",
            event_type="background_task_complete",
            message="done",
            payload={
                "task_id": "bash-awaken-deferred",
                "kind": "bash",
                "status": "completed",
                "description": "demo-task",
                "session_id": fg.session_id,
                "coara_id": fg.identity.coara_id,
            },
        )
    )
    await asyncio.sleep(0.2)

    assert started == []
    store = WorkspaceUpdatesStore(workspace.parent / "home", registry=root.workspace_manager.registry)
    assert store.list_messages(workspace="ws", status="all") == []

    gate.set()
    await asyncio.sleep(0.5)

    assert started  # 清理后唤醒
    assert any("bash-awaken-deferred" in str(getattr(m, "content", "")) for m in fg.message_history)
    assert store.list_messages(workspace="ws", status="all") == []

    fg._deferred_new_session_task = None
