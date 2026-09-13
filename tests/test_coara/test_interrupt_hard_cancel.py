"""Ctrl+C / interrupt hard-cancels fg/bg delegates and bash tasks."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.coara.background_agent import BackgroundAgentManager
from src.core.abort import AbortController
from src.tools.builtin.delegate import delegate as delegate_mod


@pytest.fixture(autouse=True)
def _reset_delegate_registries():
    delegate_mod._ACTIVE_SUBAGENTS.clear()
    delegate_mod._RUNNING_FG_TASKS.clear()
    delegate_mod._RUNNING_FG_DESCRIPTIONS.clear()
    mgr = BackgroundAgentManager()
    mgr._tasks.clear()
    mgr._cancel_requested.clear()
    yield
    delegate_mod._ACTIVE_SUBAGENTS.clear()
    delegate_mod._RUNNING_FG_TASKS.clear()
    delegate_mod._RUNNING_FG_DESCRIPTIONS.clear()
    mgr._tasks.clear()
    mgr._cancel_requested.clear()


def test_format_turn_interrupt_note_with_cancelled_delegates() -> None:
    from src.tools.builtin.delegate.delegate import format_turn_interrupt_note

    assert format_turn_interrupt_note(None) == "当前会话已打断。"
    assert format_turn_interrupt_note([]) == "当前会话已打断。"
    note = format_turn_interrupt_note([("sa-coaras-abcd1234", "修登录"), ("sa-coaras-efgh5678", "")])
    assert note.startswith("当前会话已打断。")
    assert 'delegate(action="resume", task_id=…)' in note
    assert "- sa-coaras-abcd1234：修登录" in note
    assert "- sa-coaras-efgh5678" in note


@pytest.mark.asyncio
async def test_hard_cancel_snapshots_cancelled_delegates() -> None:
    loop = asyncio.get_running_loop()

    async def _hang() -> None:
        await asyncio.Event().wait()

    fg_task = loop.create_task(_hang(), name="sa-coaras-ffff1111")
    delegate_mod._RUNNING_FG_TASKS["sa-coaras-ffff1111"] = fg_task
    delegate_mod._RUNNING_FG_DESCRIPTIONS["sa-coaras-ffff1111"] = "并行调研"

    counts = delegate_mod.hard_cancel_all_running_delegates(reason="user_ctrl_c")
    await asyncio.sleep(0)

    assert ("sa-coaras-ffff1111", "并行调研") in counts["cancelled_delegates"]
    assert fg_task.cancelled() or fg_task.done()


@pytest.mark.asyncio
async def test_hard_cancel_cancels_fg_bg_and_bash(monkeypatch: pytest.MonkeyPatch) -> None:
    loop = asyncio.get_running_loop()

    async def _hang() -> None:
        await asyncio.Event().wait()

    fg_task = loop.create_task(_hang(), name="sa-fg")
    bg_task = loop.create_task(_hang(), name="sa-bg")

    delegate_mod._RUNNING_FG_TASKS["sa-fg"] = fg_task
    BackgroundAgentManager()._tasks["sa-bg"] = bg_task

    bash_runner = SimpleNamespace(
        cancel_all=MagicMock(return_value=1),
    )
    monkeypatch.setattr(
        "src.background.bash_runner.BashBackgroundRunner",
        lambda: bash_runner,
    )

    counts = delegate_mod.hard_cancel_all_running_delegates(reason="user_ctrl_c")
    await asyncio.sleep(0)  # let cancellations land

    assert fg_task.cancelled() or fg_task.done()
    assert bg_task.cancelled() or bg_task.done()
    assert counts["fg_tasks"] == 1
    assert counts["bg_tasks"] == 1
    assert counts["bash_tasks"] == 1
    bash_runner.cancel_all.assert_called_once()


@pytest.mark.asyncio
async def test_interrupt_current_turn_hard_cancels(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.core.types import CoaraStatus
    from tests.helpers import make_test_coara

    called: dict[str, str] = {}

    def _fake_hard_cancel(*, reason: str = "user_interrupt") -> dict[str, object]:
        called["reason"] = reason
        return {
            "subagents_interrupted": 0,
            "fg_tasks": 1,
            "bg_tasks": 1,
            "bash_tasks": 1,
            "cancelled_delegates": [("sa-coaras-xxxx", "任务A")],
        }

    monkeypatch.setattr(
        "src.tools.builtin.delegate.delegate.hard_cancel_all_running_delegates",
        _fake_hard_cancel,
    )
    monkeypatch.setattr("src.llm.service.llm_service.abort_active", lambda: None)

    coara = make_test_coara(tmp_path)
    controller = AbortController()
    coara._active_turn = SimpleNamespace(
        turn_id="t1",
        controller=controller,
        signal=controller.signal,
        reason=None,
    )
    coara.status = CoaraStatus.RUNNING
    coara._continuation_inputs.append("stale")

    assert coara.interrupt_current_turn("user_ctrl_c", interrupt_source="test")
    assert called["reason"] == "user_ctrl_c"
    assert coara._continuation_inputs == []
    assert controller.signal.aborted
    assert coara._interrupt_cancelled_delegates == [("sa-coaras-xxxx", "任务A")]


@pytest.mark.asyncio
async def test_bash_cancel_all_cancels_running_tasks(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.background.bash_runner import BashBackgroundRunner
    from src.background.task_store import TaskStatus

    runner = BashBackgroundRunner()
    runner._tasks.clear()
    runner._task_stores.clear()
    runner._task_sessions.clear()
    runner._cancel_requested.clear()

    async def _hang() -> None:
        await asyncio.Event().wait()

    task = asyncio.get_running_loop().create_task(_hang(), name="bash-test")
    runner._tasks["bash-test"] = task

    store = MagicMock()
    monkeypatch.setattr(runner, "_store_for", lambda _tid: store)

    assert runner.cancel_all() == 1
    await asyncio.sleep(0)
    assert task.cancelled() or task.done()
    assert store.update.called
    # First positional arg is task_id; status marked killed.
    assert store.update.call_args.args[0] == "bash-test"
    assert store.update.call_args.kwargs.get("status") == TaskStatus.KILLED.value
    assert store.update.call_args.kwargs.get("interrupted") is True
