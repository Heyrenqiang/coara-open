"""Tests for RootCoara idle-timeout auto /new behavior (janitor flow).

The idle watcher now scans cached workspaces, dispatches a background janitor
for expired sessions, and renews the session only when the activity epoch is
unchanged. These tests drive the watcher with a fake janitor that completes
immediately.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from src.coara.root import RootCoara
from src.core.config import config_manager
from src.core.types import Message, MessageRole
from src.llm.registry import provider_registry
from tests.helpers import FakeProvider


@pytest.fixture
async def root(tmp_path: Path):
    """Minimal RootCoara — no initialize(); idle watcher only."""
    await config_manager.load()
    provider_registry.register("fake-idle", FakeProvider([]))
    try:
        r = RootCoara(workspace_dir=tmp_path, provider_name="fake-idle")
        yield r
    finally:
        await r.stop_idle_timeout_watcher()


def _bind_foreground_session(root: RootCoara, tmp_path: Path) -> str:
    """Attach a fake foreground WorkspaceSession with real conversation."""
    ws_id = "fg-idle"

    async def fake_start(*, interrupt_source: str = "new_session") -> str:
        return "fake-new-session-id"

    coara = SimpleNamespace(
        workspace_dir=str(tmp_path),
        has_active_turn=lambda: False,
        start_new_session=fake_start,
        session_id="fake-sid",
        message_history=[
            Message(role=MessageRole.USER, content="环境上下文：\n- 今天日期：x"),
            Message(role=MessageRole.USER, content="帮我干活"),
        ],
    )
    root._sessions[ws_id] = SimpleNamespace(coara=coara, workspace_name="fg-idle")
    root._foreground_session_id = ws_id
    return ws_id


async def test_idle_timeout_watcher_behavior(root, tmp_path, monkeypatch):
    """Three cases: disabled when timeout=0; skips during a turn; fires once when idle."""
    await config_manager.load()
    cfg = config_manager.config
    cfg.session.idle_check_interval_seconds = 0.05

    async def _fast_new_session(*, interrupt_source: str = "new_session") -> str:
        from uuid import uuid4

        root.session_id = str(uuid4())
        root.audit_session_id = root.session_id
        root._trace_emitter.session_id = root.session_id
        root.message_history = [
            Message(role=MessageRole.USER, content="env-seed"),
        ]
        return root.session_id

    monkeypatch.setattr(root, "start_new_session", _fast_new_session)

    # Fake janitor completes immediately (task never registered in manager).
    async def _fake_janitor(*_a, **_kw):
        return "fake-task-1"

    monkeypatch.setattr(
        "src.coara.workspace_protocol.dispatch_janitor_background",
        _fake_janitor,
    )

    recorded: list[Any] = []
    root.set_trace_sink(lambda event: recorded.append(event))

    # --- disabled when timeout == 0 ---
    cfg.session.idle_timeout_seconds = 0.0
    original = root.session_id
    await root.start_idle_timeout_watcher()
    for _ in range(8):
        await asyncio.sleep(0.02)
    await root.stop_idle_timeout_watcher()
    assert root.session_id == original
    assert not any(e.event_type == "session_auto_new" for e in recorded)

    # --- skips while turn active ---
    ws_id = _bind_foreground_session(root, tmp_path)
    cfg.session.idle_timeout_seconds = 0.1
    root._sessions[ws_id].coara.has_active_turn = lambda: True
    root._workspace_activity_at[ws_id] = time.time() - 10.0
    await root.start_idle_timeout_watcher()
    for _ in range(8):
        await asyncio.sleep(0.02)
    await root.stop_idle_timeout_watcher()
    assert root.session_id == original
    assert not any(e.event_type == "session_auto_new" for e in recorded)

    # --- fires once after idle (via janitor dispatch + renew) ---
    root._sessions[ws_id].coara.has_active_turn = lambda: False
    recorded.clear()

    activity_time = 1_000_000.0
    clock = {"now": activity_time + 1.0}
    monkeypatch.setattr(time, "time", lambda: clock["now"])
    root._last_user_activity_at = activity_time
    root._workspace_activity_at[ws_id] = activity_time

    _real_sleep = asyncio.sleep

    async def fake_sleep(seconds: float) -> None:
        clock["now"] += seconds
        await _real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await root.start_idle_timeout_watcher()
    for _ in range(30):
        await asyncio.sleep(0)
        if any(e.event_type == "session_auto_new" for e in recorded):
            break
    await root.stop_idle_timeout_watcher()
    await _real_sleep(0)

    assert any(e.event_type == "session_auto_new" for e in recorded)
    auto = [e for e in recorded if e.event_type == "session_auto_new"]
    assert len(auto) == 1


def _save_running_task(tmp_path: Path, task_id: str, *, kind: str = "bash", subagent_type: str | None = None) -> None:
    """往工作空间自己的 TaskStore 里写一条 RUNNING 记录。"""
    from src.background.task_store import TaskRecord, TaskStatus
    from src.background.task_store_paths import task_store_for_workspace
    from src.core.time import now_iso

    task_store_for_workspace(tmp_path).save(
        TaskRecord(
            task_id=task_id,
            kind=kind,
            description=f"fake {kind} task",
            status=TaskStatus.RUNNING.value,
            created_at=now_iso(),
            updated_at=now_iso(),
            subagent_type=subagent_type,
        )
    )


async def test_idle_renew_skipped_with_running_background_task(root, tmp_path, monkeypatch):
    """空间有未收官后台任务（bash/agent/workflow）时，idle renew 不 /new。"""
    ws_id = _bind_foreground_session(root, tmp_path)
    epoch = time.time() - 10.0
    root._workspace_activity_at[ws_id] = epoch
    root._janitor_activity_at[ws_id] = epoch
    root._janitor_pending[ws_id] = ("fake-task-1", epoch)

    called: list[str] = []

    async def _spy_new_session(*, interrupt_source: str = "new_session") -> str:
        called.append(interrupt_source)
        return "sid"

    root._sessions[ws_id].coara.start_new_session = _spy_new_session

    _save_running_task(tmp_path, "t-bash-1")
    await root._janitor_finalize_pending()
    assert not called, "bash RUNNING 时应豁免 idle /new"
    assert root._janitor_activity_at.get(ws_id) is None, "豁免后应清已维护标记以便任务收官后补派"
    assert ws_id not in root._janitor_pending

    # 后台子智能体同样豁免
    root._janitor_pending[ws_id] = ("fake-task-1", epoch)
    _save_running_task(tmp_path, "t-agent-1", kind="agent", subagent_type="coaras")
    await root._janitor_finalize_pending()
    assert not called

    # 活跃工作流同样豁免
    root._janitor_pending[ws_id] = ("fake-task-1", epoch)
    _save_running_task(tmp_path, "t-flow-1", kind="workflow")
    await root._janitor_finalize_pending()
    assert not called


async def test_idle_renew_not_exempted_by_janitor_or_completed(root, tmp_path, monkeypatch):
    """janitor/daily 系统任务不豁免；任务收官后正常 /new。"""
    ws_id = _bind_foreground_session(root, tmp_path)
    epoch = time.time() - 10.0
    root._workspace_activity_at[ws_id] = epoch
    root._janitor_activity_at[ws_id] = epoch
    root._janitor_pending[ws_id] = ("fake-task-1", epoch)

    called: list[str] = []

    async def _spy_new_session(*, interrupt_source: str = "new_session") -> str:
        called.append(interrupt_source)
        return "sid"

    root._sessions[ws_id].coara.start_new_session = _spy_new_session

    # 只有 janitor 系统任务在跑：不豁免
    _save_running_task(tmp_path, "t-janitor-1", kind="agent", subagent_type="janitor")
    await root._janitor_finalize_pending()
    assert called == ["idle_timeout"]

    # 用户任务收官（COMPLETED）：正常 /new
    called.clear()
    from src.background.task_store import TaskStatus
    from src.background.task_store_paths import task_store_for_workspace

    _save_running_task(tmp_path, "t-bash-2")
    store = task_store_for_workspace(tmp_path)
    store.update("t-bash-2", status=TaskStatus.COMPLETED.value)
    store.update("t-janitor-1", status=TaskStatus.COMPLETED.value)
    root._janitor_pending[ws_id] = ("fake-task-1", epoch)
    await root._janitor_finalize_pending()
    assert called == ["idle_timeout"]
