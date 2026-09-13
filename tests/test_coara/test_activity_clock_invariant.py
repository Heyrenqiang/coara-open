"""Regression: activity clocks track the last conversation activity.

Invariant (docs/CONVENTIONS.md §活动计时):
- Any one of CLI / Web / Matrix may refresh on a real message — whichever
  messaged last.
- A finished turn (the last LLM API call) also refreshes via turn_end events,
  so a long autonomous turn is not treated as idle the moment it completes.
- switch_workspace / start_new_session / slash-only must NOT refresh.
- turn_end from subagents / background maintenance agents must NOT refresh.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from src.coara.root import RootCoara
from src.core.types import Message, MessageRole
from src.llm.registry import provider_registry
from src.workspace.manager import WorkspaceManager
from tests.helpers import FakeProvider


async def _make_root(tmp_path: Path, monkeypatch, responses: list | None = None):
    monkeypatch.setattr(
        "src.workspace.ephemeral.is_ephemeral_workspace_path",
        lambda _path: False,
    )
    coara_home = tmp_path / "home"
    workspace_a = tmp_path / "a"
    workspace_b = tmp_path / "b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    provider_registry.register("fake-activity-clock", FakeProvider(responses or []))
    root = RootCoara(workspace_dir=workspace_a, provider_name="fake-activity-clock")
    root.workspace_manager = WorkspaceManager(workspace_a, coara_home=coara_home)
    await root.workspace_manager.initialize()
    entry_a = root.workspace_manager.registry.ensure_workspace(workspace_a, name="a")
    entry_b = root.workspace_manager.registry.ensure_workspace(workspace_b, name="b")
    root.workspace_manager.registry.save()
    await root.ensure_workspace_session(entry_a)
    root._foreground_session_id = entry_a.id
    return root, entry_a, entry_b


@pytest.mark.asyncio
async def test_switch_and_new_session_do_not_refresh_activity_clocks(tmp_path: Path, monkeypatch) -> None:
    root, entry_a, entry_b = await _make_root(tmp_path, monkeypatch)
    original_cwd = Path.cwd()
    try:
        frozen = 1_700_000_000.0
        root._last_user_activity_at = frozen
        root._workspace_activity_at[entry_a.id] = frozen
        root._workspace_activity_at[entry_b.id] = frozen - 100.0

        assert await root.switch_workspace(entry_b.name)
        assert root._last_user_activity_at == frozen
        assert root._workspace_activity_at[entry_a.id] == frozen
        assert root._workspace_activity_at[entry_b.id] == frozen - 100.0

        await root.start_new_session(interrupt_source="new_command")
        assert root._last_user_activity_at == frozen
        assert root._workspace_activity_at[entry_b.id] == frozen - 100.0

        # Only an explicit message stamp may move the clocks.
        root.record_user_activity(push_status=False)
        assert root._last_user_activity_at > frozen
        assert root._workspace_activity_at[entry_b.id] == root._last_user_activity_at
    finally:
        os.chdir(original_cwd)
        await root.shutdown()


@pytest.mark.asyncio
async def test_stale_switch_renew_keeps_last_message_timestamp(tmp_path: Path, monkeypatch) -> None:
    root, entry_a, entry_b = await _make_root(tmp_path, monkeypatch)
    original_cwd = Path.cwd()
    try:
        assert await root.switch_workspace(entry_b.name)
        root.foreground_coara.message_history.append(Message(role=MessageRole.USER, content="旧"))
        root.foreground_coara.message_history.append(Message(role=MessageRole.ASSISTANT, content="旧回"))
        root.record_user_activity(push_status=False)
        stale_at = time.time() - 3 * 3600
        assert await root.switch_workspace(entry_a.name)
        root._workspace_activity_at[entry_b.id] = stale_at

        assert await root.switch_workspace(entry_b.name)
        assert root.last_switch_session_renewed is True
        assert root._workspace_activity_at[entry_b.id] == stale_at
        assert root.last_switch_last_active == stale_at
    finally:
        os.chdir(original_cwd)
        await root.shutdown()


def test_record_user_activity_call_sites_are_message_only() -> None:
    """Static guard: only known message-entry modules may call record_user_activity."""
    import subprocess
    import sys

    from src.utils.win_proc import no_window_creationflags

    repo = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import pathlib,re,sys;"
            "root=pathlib.Path(sys.argv[1]);"
            # root_shim.py：attach 客户端的 root 替身——其 record_user_activity 是
            # 本地 no-op（服务端收 chat 帧即自行刷新活动计时），不触碰内核时钟。
            # attached_chat_runner.py：chat_runner 的召回界面骨架（同样只真实对话回合前调）。
            # attach_ws.py：/ws/attach 端点 mixin（自 web_server.py 拆出，消息入口语义同源）。
            "allowed={'root.py','chat_runner.py','attached_chat_runner.py','web_server.py','attach_ws.py','ingress_helpers.py','media_inbound.py','root_shim.py'};"
            "hits=[];"
            "["
            "hits.append(str(p.relative_to(root))) "
            "for p in (root/'src').rglob('*.py') "
            "if 'record_user_activity(' in p.read_text(encoding='utf-8') "
            "and p.name not in allowed"
            "];"
            "print('\\n'.join(hits));"
            "sys.exit(1 if hits else 0)",
            str(repo),
        ],
        check=False,
        capture_output=True,
        text=True,
        creationflags=no_window_creationflags(),
    )
    assert result.returncode == 0, (
        f"record_user_activity called from unexpected files (must stay message-only):\n{result.stdout}{result.stderr}"
    )


def test_record_turn_activity_call_sites_are_root_only() -> None:
    """Static guard: only root.py may call record_turn_activity (turn_end driven)."""
    import subprocess
    import sys

    from src.utils.win_proc import no_window_creationflags

    repo = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import pathlib,sys;"
            "root=pathlib.Path(sys.argv[1]);"
            "hits=[];"
            "["
            "hits.append(str(p.relative_to(root))) "
            "for p in (root/'src').rglob('*.py') "
            "if 'record_turn_activity(' in p.read_text(encoding='utf-8') "
            "and p.name != 'root.py'"
            "];"
            "print('\\n'.join(hits));"
            "sys.exit(1 if hits else 0)",
            str(repo),
        ],
        check=False,
        capture_output=True,
        text=True,
        creationflags=no_window_creationflags(),
    )
    assert result.returncode == 0, (
        "record_turn_activity called from unexpected files "
        f"(must stay turn_end-only in root.py):\n{result.stdout}{result.stderr}"
    )


@pytest.mark.asyncio
async def test_turn_end_event_refreshes_activity_clocks(tmp_path: Path, monkeypatch) -> None:
    """A WorkspaceSession turn_end advances the clock (last-LLM-call basis)."""
    import asyncio

    from src.coara.workspace_state import load_session_state
    from src.core.events import TraceEvent

    root, entry_a, _entry_b = await _make_root(tmp_path, monkeypatch)
    original_cwd = Path.cwd()
    try:
        root._subscribe_turn_end_activity()
        frozen = 1_700_000_000.0
        root._last_user_activity_at = frozen
        root._workspace_activity_at[entry_a.id] = frozen

        root.event_bus.publish(
            TraceEvent(
                coara_id=str(root.foreground_coara.identity.coara_id),
                coara_name=root.foreground_coara.identity.name,
                event_type="turn_end",
                message="Turn completed",
                payload={"turn_id": "t1", "reason": "complete"},
            )
        )
        for _ in range(20):
            await asyncio.sleep(0.01)
            if root._workspace_activity_at[entry_a.id] != frozen:
                break

        assert root._workspace_activity_at[entry_a.id] > frozen
        assert root._last_user_activity_at > frozen

        # Disk epoch follows the same basis so restarts keep the turn-end clock.
        last_updated = 0.0
        for _ in range(50):
            await asyncio.sleep(0.01)
            _sid, last_updated = load_session_state(
                tmp_path / "a",
                coara_home=tmp_path / "home",
            )
            if last_updated and last_updated > frozen:
                break
        assert last_updated is not None and last_updated > frozen
    finally:
        os.chdir(original_cwd)
        await root.shutdown()


@pytest.mark.asyncio
async def test_turn_end_from_non_session_agent_does_not_refresh(tmp_path: Path, monkeypatch) -> None:
    """Subagent / background maintenance turn_end must never move the clock."""
    import asyncio

    from src.core.events import TraceEvent

    root, entry_a, _entry_b = await _make_root(tmp_path, monkeypatch)
    original_cwd = Path.cwd()
    try:
        root._subscribe_turn_end_activity()
        frozen = 1_700_000_000.0
        root._last_user_activity_at = frozen
        root._workspace_activity_at[entry_a.id] = frozen

        root.event_bus.publish(
            TraceEvent(
                coara_id="sa-janitor-background-agent",
                coara_name="janitor",
                event_type="turn_end",
                message="Turn completed",
                payload={"turn_id": "t2", "reason": "complete"},
            )
        )
        await asyncio.sleep(0.05)

        assert root._workspace_activity_at[entry_a.id] == frozen
        assert root._last_user_activity_at == frozen
    finally:
        os.chdir(original_cwd)
        await root.shutdown()


@pytest.mark.asyncio
async def test_internal_workspace_background_turn_end_does_not_refresh(tmp_path: Path, monkeypatch) -> None:
    """记录空间（internal，persona=daily）的定时整理回合不刷活动钟。

    daily dispatch 走 source=background；同会话的用户主动聊天回合才刷
    （静默/计时按回合 source 区分，不按空间一刀切——见 turn_end 订阅的
    background 过滤）。
    """
    import asyncio

    from src.core.events import TraceEvent
    from src.workspace.types import ViewCapability

    root, entry_a, _entry_b = await _make_root(tmp_path, monkeypatch)
    original_cwd = Path.cwd()
    try:
        # 记录空间条目：合并语义——展示名=记录、persona=daily、主页 /records
        internal_dir = tmp_path / "home" / "workspaces" / ".internal" / "daily"
        internal_dir.mkdir(parents=True, exist_ok=True)
        daily_entry = root.workspace_manager.registry.ensure_internal_workspace(
            internal_dir,
            name="记录",
            view=ViewCapability.ALL,
            content_type="records",
            storefront="display",
            home_view="/records",
            persona="daily",
        )
        session = await root.ensure_workspace_session(daily_entry)
        # persona 解析：条目名是展示名，对话主体仍是 daily
        assert getattr(session.coara, "_session_agent_kind", "") == "daily"

        root._subscribe_turn_end_activity()
        frozen = 1_700_000_000.0
        root._last_user_activity_at = frozen
        root._workspace_activity_at[entry_a.id] = frozen
        root._workspace_activity_at[daily_entry.id] = frozen

        # 定时整理回合（source=background）结束：两个钟都不得动
        root.event_bus.publish(
            TraceEvent(
                coara_id=str(session.coara.identity.coara_id),
                coara_name=session.coara.identity.name,
                event_type="turn_end",
                message="Turn completed",
                payload={"turn_id": "d1", "reason": "complete", "source": "background"},
            )
        )
        await asyncio.sleep(0.05)
        assert root._workspace_activity_at[daily_entry.id] == frozen
        assert root._workspace_activity_at[entry_a.id] == frozen
        assert root._last_user_activity_at == frozen

        # 用户主动聊天回合（无 background 标记）结束：本空间时钟照常刷新
        root.event_bus.publish(
            TraceEvent(
                coara_id=str(session.coara.identity.coara_id),
                coara_name=session.coara.identity.name,
                event_type="turn_end",
                message="Turn completed",
                payload={"turn_id": "d2", "reason": "complete", "source": "cli"},
            )
        )
        for _ in range(20):
            await asyncio.sleep(0.01)
            if root._workspace_activity_at[daily_entry.id] != frozen:
                break
        assert root._workspace_activity_at[daily_entry.id] > frozen
    finally:
        os.chdir(original_cwd)
        await root.shutdown()


@pytest.mark.asyncio
async def test_full_turn_end_stamps_activity_clock(tmp_path: Path, monkeypatch) -> None:
    """Regression: a finished real turn restarts the idle clock at turn end.

    Before the fix a hours-long autonomous turn was treated as idle the moment
    it completed (clock still pointed at the last user message), so the janitor
    watcher fired maintenance immediately after the turn.
    """
    import asyncio

    from src.llm.provider import LLMResponse

    root, entry_a, _entry_b = await _make_root(tmp_path, monkeypatch, responses=[LLMResponse(content="好的")])
    original_cwd = Path.cwd()
    try:
        root._subscribe_turn_end_activity()
        frozen = 1_700_000_000.0
        root._last_user_activity_at = frozen
        root._workspace_activity_at[entry_a.id] = frozen

        chunks = [chunk async for chunk in root.foreground_coara.process_message("你好")]
        assert "好的" in "".join(chunks)
        for _ in range(20):
            await asyncio.sleep(0.01)
            if root._workspace_activity_at[entry_a.id] != frozen:
                break

        assert root._workspace_activity_at[entry_a.id] > frozen
        assert root._last_user_activity_at > frozen
    finally:
        os.chdir(original_cwd)
        await root.shutdown()
