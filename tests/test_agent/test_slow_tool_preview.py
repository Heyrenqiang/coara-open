"""Matrix 慢工具行：超过阈值仍未完成才发 running，完成前关门避免乱序。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.agent.executor import SLOW_TOOL_LINE_DELAY_S, ToolExecutor
from src.core.types import ToolCall


def _matrix_coara() -> SimpleNamespace:
    routed: list[dict] = []
    return SimpleNamespace(
        _active_turn_source="matrix",
        _cli_silent=False,
        _active_turn=SimpleNamespace(turn_id="turn-1"),
        _route_tool_line=lambda payload: routed.append(payload),
        routed=routed,
    )


@pytest.mark.asyncio
async def test_slow_tool_preview_emits_running_after_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.agent.executor.SLOW_TOOL_LINE_DELAY_S", 0.05)
    coara = _matrix_coara()
    finished = asyncio.Event()
    tool = SimpleNamespace(name="shell")
    call = ToolCall(id="tc-1", name="shell", arguments={"command": "make"})
    task = ToolExecutor()._schedule_slow_tool_preview(
        coara=coara,
        tool=tool,
        tool_call=call,
        effective_call=call,
        finished=finished,
    )
    assert task is not None
    await asyncio.sleep(0.12)
    assert len(coara.routed) == 1
    assert coara.routed[0]["running"] is True
    assert coara.routed[0]["tool_call_id"] == "tc-1"
    finished.set()
    await task


@pytest.mark.asyncio
async def test_slow_tool_preview_skipped_when_finished_early() -> None:
    coara = _matrix_coara()
    finished = asyncio.Event()
    tool = SimpleNamespace(name="read")
    call = ToolCall(id="tc-2", name="read", arguments={"path": "a.py"})
    task = ToolExecutor()._schedule_slow_tool_preview(
        coara=coara,
        tool=tool,
        tool_call=call,
        effective_call=call,
        finished=finished,
    )
    assert task is not None
    await ToolExecutor._stop_slow_tool_preview(finished, task)
    await asyncio.sleep(0.05)
    assert coara.routed == []


def test_slow_tool_preview_only_for_matrix_source() -> None:
    web = SimpleNamespace(_active_turn_source="web", _cli_silent=False)
    call = ToolCall(id="tc-3", name="shell", arguments={})
    assert ToolExecutor._wants_slow_tool_preview(web, call) is False
    matrix = SimpleNamespace(_active_turn_source="matrix", _cli_silent=False)
    assert ToolExecutor._wants_slow_tool_preview(matrix, call) is True
    silent = SimpleNamespace(_active_turn_source="matrix", _cli_silent=True)
    assert ToolExecutor._wants_slow_tool_preview(silent, call) is False


def test_delay_constant_is_three_seconds() -> None:
    assert SLOW_TOOL_LINE_DELAY_S == 3.0
