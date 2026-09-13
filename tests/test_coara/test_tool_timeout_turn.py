"""回合级工具超时即失败集成：慢工具超过 executor 阈值 → 超时错误进历史，
回合正常继续（无后台分离等待、无迟到结果注入）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.agent.executor import ToolExecutor
from src.core.tool_base import BaseTool, ToolInvocation, ToolKind, ToolResult
from src.core.types import ToolCall
from src.llm.provider import LLMResponse
from tests.helpers import FakeProvider, make_test_coara


class _SlowInvocation(ToolInvocation):
    """超过 executor timeout 才完成的慢工具：触发超时失败。"""

    def get_description(self) -> str:
        return "slow tool"

    async def execute(self, signal=None) -> ToolResult:
        await asyncio.sleep(0.3)
        return ToolResult.success("慢工具结果")


class _SlowTool(BaseTool):
    name = "slow_tool"
    description = "tool that finishes after executor timeout"
    kind = ToolKind.OTHER

    def create_invocation(self, params: dict) -> ToolInvocation:
        return _SlowInvocation(params)


@pytest.mark.asyncio
async def test_timeout_tool_fails_and_turn_continues(tmp_path: Path) -> None:
    """慢工具超时即失败：超时错误进历史，回合不挂起、不注入迟到结果。"""
    provider = FakeProvider(
        [
            LLMResponse(content="", tool_calls=[ToolCall(id="c1", name="slow_tool", arguments={})]),
            LLMResponse(content="工具超时了，我改用其它方式"),
        ]
    )
    coara = make_test_coara(tmp_path, provider=provider)
    coara.register_tool(_SlowTool())
    coara.tool_executor = ToolExecutor(default_timeout=0.05)
    await coara.initialize()

    chunks = [chunk async for chunk in coara.process_message("执行")]
    joined = "".join(chunks)

    history_text = "\n".join(str(m.content) for m in coara.message_history)
    assert "timed out" in history_text.lower()
    # 超时即失败：慢工具的后台结果不得迟到注入
    assert "慢工具结果" not in history_text
    # 回合正常结束（第二轮 LLM 响应被消费）
    assert joined
