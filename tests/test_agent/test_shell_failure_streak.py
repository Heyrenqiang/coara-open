from __future__ import annotations

import pytest

from src.agent.loop import ShellFailureStreakGuard
from src.core.tool_base import BaseTool, ToolInvocation, ToolKind, ToolResult
from src.core.types import ToolCall
from tests.helpers import make_test_coara


class EchoTool(BaseTool):
    name = "echo"
    description = "Echo stub."
    display_name = "Echo"
    kind = ToolKind.EXECUTE
    parameters_schema = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    def create_invocation(self, params: dict[str, str]) -> ToolInvocation:
        return EchoToolInvocation(params)


class EchoToolInvocation(ToolInvocation):
    def __init__(self, params: dict[str, str]) -> None:
        super().__init__(params)
        self.text = params["text"]

    def get_description(self) -> str:
        return f"Echo: {self.text}"

    async def execute(self, signal=None) -> ToolResult:
        return ToolResult.success(self.text)


class FailingShellTool(BaseTool):
    name = "shell"
    description = "Always-failing shell stub for tests."
    display_name = "Shell"
    kind = ToolKind.EXECUTE
    parameters_schema = {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    }

    def __init__(self) -> None:
        super().__init__()
        self.execution_count = 0

    def create_invocation(self, params: dict[str, str]) -> ToolInvocation:
        return FailingShellToolInvocation(self, params)


class FailingShellToolInvocation(ToolInvocation):
    def __init__(self, tool: FailingShellTool, params: dict[str, str]) -> None:
        super().__init__(params)
        self._tool = tool
        self.command = params["command"]

    def get_description(self) -> str:
        return f"Shell: {self.command}"

    async def execute(self, signal=None) -> ToolResult:
        self._tool.execution_count += 1
        return ToolResult.error(f"command failed: {self.command}")


def test_shell_failure_streak_guard_resets_on_success() -> None:
    guard = ShellFailureStreakGuard(threshold=3)
    guard.record("shell", ToolResult.error("fail"))
    guard.record("shell", ToolResult.error("fail"))
    assert guard.should_block("shell") is None

    guard.record("shell", ToolResult.success("ok"))
    guard.record("shell", ToolResult.error("fail again"))
    assert guard.should_block("shell") is None


def test_shell_failure_streak_guard_ignores_other_tools() -> None:
    guard = ShellFailureStreakGuard(threshold=2)
    guard.record("shell", ToolResult.error("fail"))
    guard.record("write", ToolResult.success("ok"))
    guard.record("shell", ToolResult.error("fail"))
    alert = guard.should_block("shell")
    assert alert is not None
    assert alert.streak == 2


def test_shell_failure_streak_guard_does_not_increment_on_own_block_message() -> None:
    guard = ShellFailureStreakGuard(threshold=2)
    guard.record("shell", ToolResult.error("fail"))
    guard.record("shell", ToolResult.error("fail"))
    guard.record("shell", ToolResult.error("Detected 2 consecutive failed shell calls."))
    assert guard.should_block("shell") is not None
    assert guard._streak == 2


def test_shell_failure_streak_guard_counts_metadata_failed() -> None:
    guard = ShellFailureStreakGuard(threshold=2)
    guard.record(
        "shell",
        ToolResult.success(
            "[执行结束，退出码: 1]\npytest failed",
            metadata={"return_code": 1, "failed": True},
        ),
    )
    guard.record(
        "shell",
        ToolResult.success(
            "[执行结束，退出码: 1]\nstill failing",
            metadata={"return_code": 1, "failed": True},
        ),
    )
    alert = guard.should_block("shell")
    assert alert is not None
    assert alert.streak == 2


def test_shell_failure_streak_guard_resets_on_success_without_failed_metadata() -> None:
    guard = ShellFailureStreakGuard(threshold=2)
    guard.record(
        "shell",
        ToolResult.success(
            "[执行结束，退出码: 1]\nfail",
            metadata={"return_code": 1, "failed": True},
        ),
    )
    guard.record(
        "shell",
        ToolResult.success("ok", metadata={"return_code": 0, "failed": False}),
    )
    guard.record(
        "shell",
        ToolResult.success(
            "[执行结束，退出码: 1]\nfail again",
            metadata={"return_code": 1, "failed": True},
        ),
    )
    assert guard.should_block("shell") is None


@pytest.mark.asyncio
async def test_shell_failure_streak_blocks_after_threshold(tmp_path) -> None:
    coara = make_test_coara(tmp_path)
    failing = FailingShellTool()
    await coara.initialize()
    coara.register_tool(failing, replace=True)

    last_result: ToolResult | None = None
    for index in range(11):
        executions = await coara.tool_executor.execute(
            coara,
            [ToolCall(id=f"call-{index}", name="shell", arguments={"command": f"cmd-{index}"})],
            is_owner=True,
        )
        last_result = executions[0].result

    assert failing.execution_count == 10
    assert last_result is not None
    assert "consecutive failed shell" in str(last_result.content).lower()


@pytest.mark.asyncio
async def test_shell_failure_streak_survives_interleaved_non_shell_tool(tmp_path) -> None:
    coara = make_test_coara(tmp_path)
    failing = FailingShellTool()
    await coara.initialize()
    coara.register_tool(failing, replace=True)
    coara.register_tool(EchoTool())

    sequence: list[ToolCall] = []
    for index in range(5):
        sequence.append(ToolCall(id=f"shell-{index}", name="shell", arguments={"command": f"fail-{index}"}))
    sequence.append(ToolCall(id="echo-1", name="echo", arguments={"text": "ok"}))
    for index in range(5, 10):
        sequence.append(ToolCall(id=f"shell-{index}", name="shell", arguments={"command": f"fail-{index}"}))
    sequence.append(ToolCall(id="shell-blocked", name="shell", arguments={"command": "should-not-run"}))

    last_result: ToolResult | None = None
    for tool_call in sequence:
        executions = await coara.tool_executor.execute(coara, [tool_call], is_owner=True)
        last_result = executions[0].result

    assert failing.execution_count == 10
    assert last_result is not None
    assert "consecutive failed shell" in str(last_result.content).lower()
