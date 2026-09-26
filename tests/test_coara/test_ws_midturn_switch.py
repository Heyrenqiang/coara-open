"""Regression tests for the mid-turn ws(switch) switch fix.

Background: when the model invokes ``ws(switch)`` *inside a running turn*, the
switch must deterministically end the current turn (so a fresh turn starts in
the target workspace with the env-seed "arrived" note). Previously the switch
relied on an abort signal that was observed only *after* the tool batch
returned, so the model would re-issue ``ws(switch)`` until the repeated-tool
loop guard fired (5 identical calls) and could leave the session on the old
workspace.

The fix: ``ws(switch)`` raises ``CoaraRunCancelledError`` when ``_inside_turn``
is set, and the executor propagates it (instead of swallowing it as a generic
error) so the orchestrator's ``except CoaraRunCancelledError`` ends the turn.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.agent.executor import ToolExecutor
from src.coara.turn_completion import CoaraRunCancelledError
from src.core.tool_base import BaseTool, ToolInvocation, ToolKind
from src.core.types import ToolCall
from src.tools.builtin.ws.ws import WsInvocation
from tests.helpers import make_test_coara


class _FakeToolForWs:
    """Lightweight coara double for the WsInvocation unit tests.

    Only needs the attributes ``_execute_switch`` touches: ``_inside_turn``,
    ``workspace_manager.active_path``, and ``switch_workspace``.
    """

    _inside_turn: bool
    workspace_manager: SimpleNamespace
    switch_workspace: object


async def _switch_true(name, **kwargs):
    return True


def _fake_coara(inside_turn: bool) -> _FakeToolForWs:
    coara = _FakeToolForWs()
    coara._inside_turn = inside_turn
    coara.message_history = []
    coara.workspace_manager = SimpleNamespace(active_path=Path("/ws/v8"))
    coara.switch_workspace = _switch_true
    # WsTool reads root.foreground_coara for the mid-turn check; the fake plays
    # both Root and foreground session in one object.
    coara.foreground_coara = coara
    return coara


def test_ws_use_mid_turn_raises_to_end_turn():
    inv = WsInvocation({"action": "switch", "name": "v8"}, _fake_coara(inside_turn=True))
    with pytest.raises(CoaraRunCancelledError):
        asyncio.run(inv.execute())


def test_ws_use_non_turn_returns_control_only():
    inv = WsInvocation({"action": "switch", "name": "v8"}, _fake_coara(inside_turn=False))
    result = asyncio.run(inv.execute())
    assert result.metadata.get("control_only") is True


class _CancelTool(BaseTool):
    """Tool whose invocation raises CoaraRunCancelledError on execute."""

    name = "ws"
    description = "test tool that requests a mid-turn switch"
    kind = ToolKind.THINK
    category = "ws"

    def create_invocation(self, params: dict):
        return _CancelInvocation(params)


class _CancelInvocation(ToolInvocation):
    def get_description(self) -> str:
        return "switch_workspace"

    async def execute(self, signal=None) -> object:
        raise CoaraRunCancelledError("switch_workspace:v8")


def test_executor_propagates_coara_run_cancelled(tmp_path: Path):
    """The executor must re-raise CoaraRunCancelledError instead of swallowing
    it as a generic tool error, so the orchestrator can end the turn."""
    coara = make_test_coara(tmp_path)
    coara.register_tool(_CancelTool())
    asyncio.run(coara.initialize())

    executor = ToolExecutor()
    with pytest.raises(CoaraRunCancelledError):
        asyncio.run(
            executor.execute(
                coara,
                [ToolCall(id="t1", name="ws", arguments={"action": "switch", "name": "v8"})],
                is_owner=True,
            )
        )
