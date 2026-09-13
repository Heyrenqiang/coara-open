"""Executor second-layer guard: detect _raw sentinel from truncated arguments."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.agent.executor import ToolExecutor
from src.core.tool_base import BaseTool, ToolInvocation, ToolKind, ToolResult
from src.core.types import ToolCall
from tests.helpers import make_test_coara


class _TrackingEchoTool(BaseTool):
    """Echo tool that records whether create_invocation was called."""

    name = "echo"
    description = "Echo the input text."
    display_name = "Echo"
    kind = ToolKind.EXECUTE
    parameters_schema = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }
    invocation_created = False

    def create_invocation(self, params: dict[str, object]) -> ToolInvocation:
        self.__class__.invocation_created = True
        if "text" not in params:
            raise ValueError("Missing required parameter: text")
        return _TrackingEchoInvocation(params)


class _TrackingEchoInvocation(ToolInvocation):
    def get_description(self) -> str:
        return "Echo"

    async def execute(self, signal=None) -> ToolResult:
        return ToolResult.success(self.params.get("text", ""))


@pytest.mark.asyncio
async def test_executor_returns_clear_error_when_arguments_contain_raw_sentinel(
    tmp_path: Path,
) -> None:
    """_raw sentinel triggers clear error, never reaches create_invocation."""
    _TrackingEchoTool.invocation_created = False
    coara = make_test_coara(tmp_path)
    coara.register_tool(_TrackingEchoTool())
    await coara.initialize()

    tool_call = ToolCall(
        id="call-1",
        name="echo",
        arguments={"_raw": '{"text": "aaa'},  # truncated JSON fallback
    )
    executor = ToolExecutor()
    execution = await executor._execute_one(coara, 0, tool_call, False)

    assert execution.result.is_error
    content = str(execution.result.content)
    assert "参数 JSON 不完整" in content
    assert "max_tokens" in content
    assert "Missing required parameter" not in content
    assert _TrackingEchoTool.invocation_created is False  # never reached create_invocation


@pytest.mark.asyncio
async def test_executor_executes_normally_when_arguments_valid(
    tmp_path: Path,
) -> None:
    """Normal arguments pass through the guard and execute successfully."""
    _TrackingEchoTool.invocation_created = False
    coara = make_test_coara(tmp_path)
    coara.register_tool(_TrackingEchoTool())
    await coara.initialize()

    tool_call = ToolCall(
        id="call-1",
        name="echo",
        arguments={"text": "hello"},
    )
    executor = ToolExecutor()
    execution = await executor._execute_one(coara, 0, tool_call, False)

    assert not execution.result.is_error
    assert execution.result.content == "hello"
    assert _TrackingEchoTool.invocation_created is True
