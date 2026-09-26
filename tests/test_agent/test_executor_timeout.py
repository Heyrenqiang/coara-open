"""执行器超时即失败（无并行、无分离）回归：

工具执行超过阈值后 wait_for 取消任务并返回明确超时错误；
工具任务被取消（而非放任孤儿继续跑），shell 类经 CancelledError
路径杀进程树。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.agent.executor import ToolExecutor
from src.core.tool_base import BaseTool, ToolInvocation, ToolKind, ToolResult
from src.core.types import ToolCall
from tests.helpers import make_test_coara


class _ForeverInvocation(ToolInvocation):
    """永不完成的慢工具：挂起直到被取消。"""

    def get_description(self) -> str:
        return "forever tool"

    async def execute(self, signal=None) -> ToolResult:
        await asyncio.Future()
        return ToolResult.success("unreachable")


class _ForeverTool(BaseTool):
    name = "forever_tool"
    description = "never finishes"
    kind = ToolKind.OTHER

    def create_invocation(self, params: dict) -> ToolInvocation:
        return _ForeverInvocation(params)


class _CancelRecordingInvocation(ToolInvocation):
    cancelled: asyncio.Event

    def get_description(self) -> str:
        return "cancel recording"

    async def execute(self, signal=None) -> ToolResult:
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            type(self).cancelled.set()
            raise
        return ToolResult.success("unreachable")


class _CancelRecordingTool(BaseTool):
    name = "cancel_recording"
    description = "records cancellation"
    kind = ToolKind.OTHER

    def create_invocation(self, params: dict) -> ToolInvocation:
        return _CancelRecordingInvocation(params)


@pytest.mark.asyncio
async def test_slow_tool_times_out_and_returns_error(tmp_path: Path) -> None:
    """超时即失败：返回明确超时错误，工具任务被取消。"""
    _CancelRecordingInvocation.cancelled = asyncio.Event()
    coara = make_test_coara(tmp_path)
    coara.register_tool(_CancelRecordingTool())
    await coara.initialize()

    executor = ToolExecutor(default_timeout=0.05)
    executions = await executor.execute(
        coara,
        [ToolCall(id="call-t", name="cancel_recording", arguments={})],
        is_owner=True,
    )

    result = executions[0].result
    assert result.is_error
    assert "timed out" in (result.content or "").lower()
    # 任务被取消（而非放任孤儿继续跑）
    assert _CancelRecordingInvocation.cancelled.is_set()


@pytest.mark.asyncio
async def test_fast_tool_untouched(tmp_path: Path) -> None:
    """快工具不受影响：正常返回结果，不误报超时。"""

    class _FastInvocation(ToolInvocation):
        def get_description(self) -> str:
            return "fast tool"

        async def execute(self, signal=None) -> ToolResult:
            return ToolResult.success("fast ok")

    class _FastTool(BaseTool):
        name = "fast_tool"
        description = "finishes quickly"
        kind = ToolKind.OTHER

        def create_invocation(self, params: dict) -> ToolInvocation:
            return _FastInvocation(params)

    coara = make_test_coara(tmp_path)
    coara.register_tool(_FastTool())
    await coara.initialize()

    executor = ToolExecutor(default_timeout=0.05)
    executions = await executor.execute(
        coara,
        [ToolCall(id="call-f", name="fast_tool", arguments={})],
        is_owner=True,
    )
    result = executions[0].result
    assert not result.is_error
    assert "fast ok" in (result.content or "")


@pytest.mark.asyncio
async def test_no_timeout_tool_not_interrupted(tmp_path: Path) -> None:
    """get_execution_timeout 返回 None 的工具（自己管理时长）不被外层截断。"""

    class _SelfManagedInvocation(ToolInvocation):
        started = False

        def get_description(self) -> str:
            return "self managed"

        async def execute(self, signal=None) -> ToolResult:
            type(self).started = True
            await asyncio.sleep(0.1)
            return ToolResult.success("managed ok")

    class _SelfManagedTool(BaseTool):
        name = "self_managed"
        description = "manages its own timeout"
        kind = ToolKind.OTHER

        def create_invocation(self, params: dict) -> ToolInvocation:
            return _SelfManagedInvocation(params)

        def get_execution_timeout(self, default_timeout: float, args: dict | None = None) -> float | None:
            return None

    coara = make_test_coara(tmp_path)
    coara.register_tool(_SelfManagedTool())
    await coara.initialize()

    executor = ToolExecutor(default_timeout=0.02)
    executions = await executor.execute(
        coara,
        [ToolCall(id="call-m", name="self_managed", arguments={})],
        is_owner=True,
    )
    result = executions[0].result
    assert not result.is_error
    assert "managed ok" in (result.content or "")
