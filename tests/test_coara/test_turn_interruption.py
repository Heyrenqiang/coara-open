"""回合中断语义：批量工具部分完成时被取消，历史必须如实反映执行情况。

覆盖：
- 并发批：已完成工具写入真实结果，被取消工具写入已取消结果（不谎称）
- 串行批（同写锁）：未开始的工具闭合为「未执行」，与已取消区分
- LLMError：失败注记入史，模型下一轮知道上一轮失败过
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.core.errors import LLMError
from src.core.tool_base import BaseTool, ToolInvocation, ToolKind, ToolResult
from src.core.types import MessageRole, ToolCall
from src.llm.provider import LLMResponse
from tests.helpers import FakeProvider, make_test_coara


class _EchoInvocation(ToolInvocation):
    finished: asyncio.Event

    def __init__(self, params: dict[str, str]):
        super().__init__(params)
        self.text = params["text"]

    def get_description(self) -> str:
        return f"Echo: {self.text}"

    async def execute(self, signal=None) -> ToolResult:
        type(self).finished.set()
        return ToolResult.success(self.text)


class _EchoTool(BaseTool):
    name = "echo"
    description = "Echo the input text"
    display_name = "Echo"
    kind = ToolKind.EXECUTE
    parameters_schema = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    def create_invocation(self, params: dict[str, str]) -> ToolInvocation:
        if "text" not in params:
            raise ValueError("Missing required parameter: text")
        return _EchoInvocation(params)


class _SlowInvocation(_EchoInvocation):
    started: asyncio.Event

    async def execute(self, signal=None) -> ToolResult:
        type(self).started.set()
        await asyncio.Future()
        raise AssertionError("unreachable")


class _SlowTool(_EchoTool):
    name = "slow_echo"

    def create_invocation(self, params: dict[str, str]) -> ToolInvocation:
        if "text" not in params:
            raise ValueError("Missing required parameter: text")
        return _SlowInvocation(params)


class _LockedSlowTool(_SlowTool):
    """与 _LockedEchoTool 共用写锁，同批串行：slow 在前阻塞时 echo 不会开始。"""

    name = "locked_slow"

    def get_write_lock(self, arguments: dict) -> str:
        return "shared-key"


class _LockedEchoTool(_EchoTool):
    name = "locked_echo"

    def get_write_lock(self, arguments: dict) -> str:
        return "shared-key"


def _reset_events() -> None:
    _EchoInvocation.finished = asyncio.Event()
    _SlowInvocation.started = asyncio.Event()


def _tool_results_by_call_id(coara) -> dict[str, str]:
    return {
        message.tool_call_id: str(message.content)
        for message in coara.message_history
        if message.role == MessageRole.TOOL_RESULT
    }


@pytest.mark.asyncio
async def test_interrupt_concurrent_batch_records_real_results(tmp_path: Path) -> None:
    """并发批被取消：已完成工具的真实结果入史，被取消工具标注已取消。"""
    _reset_events()
    provider = FakeProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(id="call-fast", name="echo", arguments={"text": "alpha"}),
                    ToolCall(id="call-slow", name="slow_echo", arguments={"text": "beta"}),
                ],
            )
        ]
    )
    coara = make_test_coara(tmp_path, provider=provider)
    coara.register_tool(_EchoTool())
    coara.register_tool(_SlowTool())
    await coara.initialize()

    async def collect() -> list[str]:
        return [chunk async for chunk in coara.process_message("开始")]

    task = asyncio.create_task(collect())
    await _SlowInvocation.started.wait()
    await asyncio.wait_for(_EchoInvocation.finished.wait(), timeout=1)

    assert coara.interrupt_current_turn("user_escape")
    chunks = await asyncio.wait_for(task, timeout=5)

    assert chunks[-1] == "[系统] 当前会话已打断。"
    results = _tool_results_by_call_id(coara)
    # 已完成的 echo：真实结果入史，不是谎称「已取消」
    assert "alpha" in results["call-fast"]
    assert "取消" not in results["call-fast"]
    assert "未执行" not in results["call-fast"]
    # 被取消的 slow_echo：如实标注已取消
    assert "已被用户取消" in results["call-slow"]


@pytest.mark.asyncio
async def test_interrupt_serial_batch_marks_never_started_as_unexecuted(tmp_path: Path) -> None:
    """串行批被取消：未开始的工具闭合为「未执行」，与被取消的工具区分。"""
    _reset_events()
    provider = FakeProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(id="call-slow", name="locked_slow", arguments={"text": "beta"}),
                    ToolCall(id="call-fast", name="locked_echo", arguments={"text": "alpha"}),
                ],
            )
        ]
    )
    coara = make_test_coara(tmp_path, provider=provider)
    coara.register_tool(_LockedSlowTool())
    coara.register_tool(_LockedEchoTool())
    await coara.initialize()

    async def collect() -> list[str]:
        return [chunk async for chunk in coara.process_message("开始")]

    task = asyncio.create_task(collect())
    await _SlowInvocation.started.wait()

    assert coara.interrupt_current_turn("user_escape")
    chunks = await asyncio.wait_for(task, timeout=5)

    assert chunks[-1] == "[系统] 当前会话已打断。"
    assert not _EchoInvocation.finished.is_set()
    results = _tool_results_by_call_id(coara)
    assert "已被用户取消" in results["call-slow"]
    assert "未执行" in results["call-fast"]
    assert "alpha" not in results["call-fast"]


class _FailingProvider(FakeProvider):
    def __init__(self):
        super().__init__([])
        self.calls = 0

    async def complete(self, *args, **kwargs) -> LLMResponse:
        self.calls += 1
        raise LLMError("连接被重置")


@pytest.mark.asyncio
async def test_llm_error_appends_failure_note_to_history(tmp_path: Path) -> None:
    """LLM 异常：用户消息悬空处追加失败注记，UI 报错行为不变。"""
    provider = _FailingProvider()
    coara = make_test_coara(tmp_path, provider=provider)
    await coara.initialize()

    chunks = [chunk async for chunk in coara.process_message("你好")]

    assert provider.calls >= 1
    assert chunks[-1].startswith("Error:")
    notes = [
        message
        for message in coara.message_history
        if message.role == MessageRole.USER and "上一轮模型请求失败" in str(message.content)
    ]
    assert len(notes) == 1
    assert "LLMError" in str(notes[0].content)
    assert "连接被重置" in str(notes[0].content)
    # 用户消息保留在注记之前（协议顺序：user → 注记）
    contents = [str(message.content) for message in coara.message_history]
    assert contents.index("你好") < contents.index(str(notes[0].content))


class _AbortableWaitInvocation(_EchoInvocation):
    """尊重 AbortSignal 的工具：等待期间 signal 触发即抛 OperationAborted。"""

    started: asyncio.Event

    async def execute(self, signal=None) -> ToolResult:
        type(self).started.set()
        from src.core.abort import wait_for_abortable

        await wait_for_abortable(asyncio.Future(), signal)
        raise AssertionError("unreachable")


class _AbortableWaitTool(_EchoTool):
    name = "abortable_wait"

    def get_execution_timeout(self, default_timeout: float, args: dict | None = None) -> float | None:
        # 与 delegate 等等待型工具一致：禁用通用执行器超时，
        # execute 被直接 await，signal 触发时 wait_for_abortable 抛
        # OperationAborted（而非外层取消的 CancelledError）
        return None

    def create_invocation(self, params: dict[str, str]) -> ToolInvocation:
        if "text" not in params:
            raise ValueError("Missing required parameter: text")
        return _AbortableWaitInvocation(params)


@pytest.mark.asyncio
async def test_signal_aborted_tool_records_interrupted_result_not_unexecuted(tmp_path: Path) -> None:
    """REMAINING #348：尊重 AbortSignal 的工具执行中被用户打断，历史如实记录
    「执行中被中断」的已取消结果，不再被悬空闭合谎标「未执行」。
    （ws 切换的 control_cancel 不经此路径，语义由 test_ws_midturn_switch 钉住）"""
    _AbortableWaitInvocation.started = asyncio.Event()
    provider = FakeProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="call-abort", name="abortable_wait", arguments={"text": "beta"})],
            )
        ]
    )
    coara = make_test_coara(tmp_path, provider=provider)
    coara.register_tool(_AbortableWaitTool())
    await coara.initialize()

    async def collect() -> list[str]:
        return [chunk async for chunk in coara.process_message("开始")]

    task = asyncio.create_task(collect())
    await _AbortableWaitInvocation.started.wait()

    assert coara.interrupt_current_turn("user_escape")
    chunks = await asyncio.wait_for(task, timeout=5)

    assert chunks[-1] == "[系统] 当前会话已打断。"
    results = _tool_results_by_call_id(coara)
    # 执行中被中断：如实记录真实结果，不谎称「未执行」
    assert "执行中被用户打断" in results["call-abort"]
    assert "未执行" not in results["call-abort"]
