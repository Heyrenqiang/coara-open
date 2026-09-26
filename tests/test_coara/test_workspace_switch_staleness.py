"""Switch-in staleness: cached sessions renew after the idle timeout.

Rule (single source: ``session.idle_timeout_seconds``, default 7200):
- switching into a workspace whose last *message* is older than the timeout
  and that still has conversation starts a fresh session and reports
  ``session_renewed``;
- switching / ``/new`` / auto-idle renew do **not** refresh the activity clock
  — only CLI / Web / Matrix user messages call ``record_user_activity``;
- a workspace whose disk state is already stale keeps that last-message time
  and shows ``新会话`` without inventing a fresh "now" stamp.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from src.coara.root import RootCoara
from src.llm.registry import provider_registry
from src.workspace.manager import WorkspaceManager
from tests.helpers import FakeProvider


async def _make_root(tmp_path: Path, monkeypatch, name_a: str = "a", name_b: str = "b"):
    monkeypatch.setattr(
        "src.workspace.ephemeral.is_ephemeral_workspace_path",
        lambda _path: False,
    )
    coara_home = tmp_path / "home"
    workspace_a = tmp_path / name_a
    workspace_b = tmp_path / name_b
    workspace_a.mkdir()
    workspace_b.mkdir()

    provider_name = f"fake-stale-{name_a}"
    provider_registry.register(provider_name, FakeProvider([]))

    root = RootCoara(workspace_dir=workspace_a, provider_name=provider_name)
    root.workspace_manager = WorkspaceManager(workspace_a, coara_home=coara_home)
    await root.workspace_manager.initialize()
    entry_a = root.workspace_manager.registry.ensure_workspace(workspace_a, name=name_a)
    entry_b = root.workspace_manager.registry.ensure_workspace(workspace_b, name=name_b)
    root.workspace_manager.registry.save()
    await root.ensure_workspace_session(entry_a)
    root._foreground_session_id = entry_a.id
    return root, entry_a, entry_b


@pytest.mark.asyncio
async def test_switch_into_recent_cached_session_continues(tmp_path: Path, monkeypatch) -> None:
    from src.core.types import Message, MessageRole

    root, entry_a, entry_b = await _make_root(tmp_path, monkeypatch)
    original_cwd = Path.cwd()
    try:
        assert await root.switch_workspace(entry_b.name)
        # Simulate prior conversation so the UI treats this as a continued session.
        root.foreground_coara.message_history.append(Message(role=MessageRole.USER, content="你好"))
        root.foreground_coara.message_history.append(
            Message(role=MessageRole.ASSISTANT, content="你好，有什么可以帮忙的？")
        )
        session_id_b = root.foreground_coara.session_id
        root.record_user_activity(push_status=False)

        assert await root.switch_workspace(entry_a.name)
        assert await root.switch_workspace(entry_b.name)
        assert root.last_switch_session_renewed is False
        assert root.foreground_coara.session_id == session_id_b
    finally:
        os.chdir(original_cwd)
        await root.shutdown()


@pytest.mark.asyncio
async def test_switch_into_stale_cached_session_renews(tmp_path: Path, monkeypatch) -> None:
    from src.core.types import Message, MessageRole

    root, entry_a, entry_b = await _make_root(tmp_path, monkeypatch)
    original_cwd = Path.cwd()
    try:
        assert await root.switch_workspace(entry_b.name)
        root.foreground_coara.message_history.append(Message(role=MessageRole.USER, content="旧对话"))
        root.foreground_coara.message_history.append(Message(role=MessageRole.ASSISTANT, content="旧回复"))
        root.record_user_activity(push_status=False)
        old_session_id = root.foreground_coara.session_id

        # Leave B, then backdate B's *message* clock beyond the timeout.
        assert await root.switch_workspace(entry_a.name)
        stale_at = time.time() - 3 * 3600
        root._workspace_activity_at[entry_b.id] = stale_at
        assert await root.switch_workspace(entry_b.name)

        assert root.last_switch_session_renewed is True
        assert root.foreground_coara.session_id != old_session_id
        # Switch / renew must not refresh the message activity clock.
        assert root._workspace_activity_at[entry_b.id] == stale_at
    finally:
        os.chdir(original_cwd)
        await root.shutdown()


@pytest.mark.asyncio
async def test_stale_disk_state_keeps_last_message_time(tmp_path: Path, monkeypatch) -> None:
    root, entry_a, entry_b = await _make_root(tmp_path, monkeypatch)
    original_cwd = Path.cwd()
    try:
        # Simulate B's on-disk state from a previous process, 3h old.
        from src.coara.workspace_state import save_session_state

        stale_at = time.time() - 3 * 3600
        save_session_state(
            Path(entry_b.resolved_path()),
            "old-session-id",
            coara_home=root.workspace_manager.coara_home,
            last_updated=stale_at,
        )

        assert await root.switch_workspace(entry_b.name)
        # Disk state was stale → empty fresh session; UI says 新会话.
        # Activity stays the disk last-message time (not "now").
        assert root.last_switch_session_renewed is True
        assert root.foreground_coara.session_id != "old-session-id"
        assert root._workspace_activity_at[entry_b.id] == stale_at
        session_id_b = root.foreground_coara.session_id

        from src.core.types import Message, MessageRole

        root.foreground_coara.message_history.append(Message(role=MessageRole.USER, content="继续"))
        root.foreground_coara.message_history.append(Message(role=MessageRole.ASSISTANT, content="好的"))
        root.record_user_activity(push_status=False)

        # Switching away and back must keep the same session once it has conversation.
        assert await root.switch_workspace(entry_a.name)
        assert await root.switch_workspace(entry_b.name)
        assert root.last_switch_session_renewed is False
        assert root.last_switch_last_active is not None
        assert root.foreground_coara.session_id == session_id_b
    finally:
        os.chdir(original_cwd)
        await root.shutdown()


def test_format_workspace_switch_message_shows_last_conversation_clock() -> None:
    from datetime import datetime

    from src.coara.workspace_state import (
        format_last_conversation_clock,
        format_workspace_switch_message,
    )

    now_dt = datetime(2024, 3, 15, 16, 0, 0)
    now = now_dt.timestamp()
    same_day = datetime(2024, 3, 15, 14, 30, 0).timestamp()
    yesterday = datetime(2024, 3, 14, 9, 5, 0).timestamp()
    earlier = datetime(2024, 2, 1, 8, 0, 0).timestamp()

    assert format_last_conversation_clock(same_day, now=now) == "14:30"
    assert format_last_conversation_clock(yesterday, now=now) == "昨天 09:05"
    assert format_last_conversation_clock(earlier, now=now) == "2月1日 08:00"

    renewed = format_workspace_switch_message(
        "shop",
        session_renewed=True,
        last_active=now - 3 * 3600,
        now=now,
    )
    assert renewed == "已切换到工作空间 shop 新会话"

    continued = format_workspace_switch_message(
        "shop",
        session_renewed=False,
        last_active=same_day,
        now=now,
    )
    assert continued == "已切换到工作空间 shop 上次对话 14:30"
    assert "（" not in continued

    fallback = format_workspace_switch_message("shop", session_renewed=False, last_active=None)
    assert fallback == "已切换到工作空间 shop 继续上次会话"
