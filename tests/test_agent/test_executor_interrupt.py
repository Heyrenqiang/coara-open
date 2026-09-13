"""执行器中断收尾回归（REMAINING #347/#348 executor 层）：

- 只读并发组中某工具经 signal 路径抛错（ws 切换 control_cancel /
  OperationAborted 重抛）时，兄弟任务必须被取消并有界收尾，其真实结果
  进入 interrupt_sink，不得成孤儿后被丢弃再被谎标「未执行」
- 尊重 AbortSignal 的工具（wait_for_abortable）被用户打断时，如实记录
  「执行中被中断」的已取消结果；control_cancel（ws 切换）工具自身仍不
  产生结果记录（语义不变）
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.agent.executor import ToolExecutor
from src.coara.turn_completion import CoaraRunCancelledError
from src.core.abort import AbortController, wait_for_abortable
from src.core.tool_base import BaseTool, ToolInvocation, ToolKind, ToolResult
from src.core.types import ToolCall
from tests.helpers import make_test_coara


class _SlowInvocation(ToolInvocation):
    """只读慢工具：开始后永久阻塞，记录是否被取消。"""

    started: asyncio.Event
    cancelled: asyncio.Event

    def get_description(self) -> str:
        return "slow"

    async def execute(self, signal=None) -> ToolResult:
        type(self).started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            type(self).cancelled.set()
            raise
        raise AssertionError("unreachable")


class _SlowTool(BaseTool):
    name = "slow_readonly"
    description = "slow read-only tool"
    kind = ToolKind.OTHER

    def create_invocation(self, params: dict) -> ToolInvocation:
        return _SlowInvocation(params)


class _SwitchInvocation(ToolInvocation):
    """模拟 mid-turn ws(switch)：等兄弟开始执行后抛 control_cancel。"""

    def get_description(self) -> str:
        return "switch_workspace"

    async def execute(self, signal=None) -> ToolResult:
        await _SlowInvocation.started.wait()
        raise CoaraRunCancelledError("switch_workspace:v8")


class _SwitchTool(BaseTool):
    name = "ws"
    description = "test tool that requests a mid-turn switch"
    kind = ToolKind.THINK
    category = "ws"

    def create_invocation(self, params: dict) -> ToolInvocation:
        return _SwitchInvocation(params)


class _AbortableInvocation(ToolInvocation):
    """尊重 AbortSignal 的工具：等待期间 signal 触发即抛 OperationAborted。"""

    started: asyncio.Event

    def get_description(self) -> str:
        return "abortable wait"

    async def execute(self, signal=None) -> ToolResult:
        type(self).started.set()
        await wait_for_abortable(asyncio.Future(), signal)
        raise AssertionError("unreachable")


class _AbortableTool(BaseTool):
    name = "abortable_wait"
    description = "tool that waits respecting the abort signal"
    kind = ToolKind.OTHER

    def create_invocation(self, params: dict) -> ToolInvocation:
        return _AbortableInvocation(params)


@pytest.mark.asyncio
async def test_readonly_group_cancel_drains_sibling_results(tmp_path: Path) -> None:
    """#347：只读并发组中 ws 切换抛错时，兄弟任务被取消并收尾，其已取消
    结果进入 interrupt_sink（不再成孤儿、不再被谎标「未执行」）。"""
    _SlowInvocation.started = asyncio.Event()
    _SlowInvocation.cancelled = asyncio.Event()
    coara = make_test_coara(tmp_path)
    coara.register_tool(_SlowTool())
    coara.register_tool(_SwitchTool())
    await coara.initialize()

    executor = ToolExecutor()
    sink: list = []
    with pytest.raises(CoaraRunCancelledError):
        await executor.execute(
            coara,
            [
                ToolCall(id="call-slow", name="slow_readonly", arguments={}),
                ToolCall(id="call-switch", name="ws", arguments={"action": "switch", "name": "v8"}),
            ],
            is_owner=True,
            interrupt_sink=sink,
        )

    # 兄弟任务确实被取消（不是孤儿），其真实结果进入 sink
    assert _SlowInvocation.cancelled.is_set()
    sink_by_id = {ex.tool_call.id: ex for ex in sink}
    assert "call-slow" in sink_by_id
    slow_result = sink_by_id["call-slow"].result
    assert getattr(slow_result, "is_cancelled", False)
    assert "已被用户取消" in str(slow_result.content)
    # control_cancel 语义不变：ws 切换工具自身不产生结果记录
    assert "call-switch" not in sink_by_id


@pytest.mark.asyncio
async def test_signal_aborted_tool_records_interrupted_result(tmp_path: Path) -> None:
    """#348：尊重 AbortSignal 的工具被用户打断时，如实记录「执行中被中断」
    的已取消结果进 interrupt_sink，不再无记录地被谎标「未执行」。"""
    _AbortableInvocation.started = asyncio.Event()
    coara = make_test_coara(tmp_path)
    coara.register_tool(_AbortableTool())
    await coara.initialize()

    controller = AbortController()
    executor = ToolExecutor()
    sink: list = []
    task = asyncio.create_task(
        executor.execute(
            coara,
            [ToolCall(id="call-abort", name="abortable_wait", arguments={})],
            is_owner=True,
            signal=controller.signal,
            interrupt_sink=sink,
        )
    )
    await _AbortableInvocation.started.wait()
    controller.abort("user_escape")
    with pytest.raises(CoaraRunCancelledError):
        await task

    assert len(sink) == 1
    result = sink[0].result
    assert getattr(result, "is_cancelled", False)
    assert "执行中被用户打断" in str(result.content)
    assert "未执行" not in str(result.content)
