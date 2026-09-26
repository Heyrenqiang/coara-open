from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.agent.executor import ToolExecutor
from src.coara.base import CoaraBase
from src.core.tool_base import BaseTool, ToolInvocation, ToolKind, ToolResult
from src.core.types import CoaraPersona, Message, MessageRole, ToolCall
from src.llm.provider import LLMResponse
from src.todos.store import TodoStore
from src.todos.turn_control import (
    TODO_SUBSTANTIAL_DELIVERY_CHARS,
    TodoLoopState,
    TurnAction,
    TurnController,
    TurnState,
)
from src.todos.types import TodoItem
from tests.helpers import BlockingProvider, FakeProvider, make_test_coara


class CapturingProvider(FakeProvider):
    def __init__(self, responses: list[LLMResponse]):
        super().__init__(responses)
        self.calls: list[tuple[list, str | None, object]] = []

    async def complete(self, *args, **kwargs) -> LLMResponse:
        self.calls.append((list(kwargs.get("messages", [])), kwargs.get("system_prompt"), kwargs.get("tools")))
        return await super().complete(*args, **kwargs)


class CompressionCapturingProvider(CapturingProvider):
    def __init__(self, responses: list[LLMResponse], *, context_window: int):
        super().__init__(responses)
        self._context_window = context_window

    def get_context_window(self, model: str | None = None) -> int:
        return self._context_window


class EchoTool(BaseTool):
    name = "echo"
    description = "Echo the input text."
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
        return EchoToolInvocation(params)


class EchoToolInvocation(ToolInvocation):
    def __init__(self, params: dict[str, str]):
        super().__init__(params)
        self.text = params["text"]

    def get_description(self) -> str:
        return f"Echo: {self.text}"

    async def execute(self, signal=None) -> ToolResult:
        return ToolResult.success(self.text)


class SlowEchoTool(EchoTool):
    def create_invocation(self, params: dict[str, str]) -> ToolInvocation:
        if "text" not in params:
            raise ValueError("Missing required parameter: text")
        return SlowEchoToolInvocation(params)


class SlowEchoToolInvocation(EchoToolInvocation):
    started = asyncio.Event()

    async def execute(self, signal=None) -> ToolResult:
        self.__class__.started.set()
        await asyncio.Future()
        raise AssertionError("unreachable")


class NoTimeoutSlowTool(EchoTool):
    def get_execution_timeout(self, default_timeout: float, args: dict | None = None) -> float | None:
        return None

    def create_invocation(self, params: dict[str, str]) -> ToolInvocation:
        if "text" not in params:
            raise ValueError("Missing required parameter: text")
        return NoTimeoutSlowToolInvocation(params)


class NoTimeoutSlowToolInvocation(EchoToolInvocation):
    async def execute(self, signal=None) -> ToolResult:
        await asyncio.sleep(0.05)
        return ToolResult.success(self.text)


class ErrorTool(EchoTool):
    async_error = "boom"

    def create_invocation(self, params: dict[str, str]) -> ToolInvocation:
        if "text" not in params:
            raise ValueError("Missing required parameter: text")
        return ErrorToolInvocation(params, self.async_error)


class ErrorToolInvocation(EchoToolInvocation):
    def __init__(self, params: dict[str, str], error_message: str):
        super().__init__(params)
        self.error_message = error_message

    async def execute(self, signal=None) -> ToolResult:
        return ToolResult.error(self.error_message)


class MockWorkflowTool(BaseTool):
    name = "orchestrator"
    description = "Mock orchestrator tool for tests (workflow save)."
    display_name = "Orchestrator"
    kind = ToolKind.EXECUTE
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {"type": "string"},
            "definition": {"type": "string"},
            "source_subagent": {"type": "string"},
        },
        "required": ["action"],
    }
    saved_payloads: list[str] = []
    source_subagents: list[str] = []

    def __init__(self, *, save_error: bool = False):
        self.save_error = save_error
        super().__init__()

    def create_invocation(self, params: dict[str, object]) -> ToolInvocation:
        action = str(params.get("action") or "")
        if action == "save" and not str(params.get("definition") or "").strip():
            raise ValueError("Missing required parameter: definition")
        return MockWorkflowInvocation(params, save_error=self.save_error)


class MockWorkflowInvocation(ToolInvocation):
    def __init__(self, params: dict[str, object], *, save_error: bool):
        super().__init__(params)
        self.save_error = save_error

    def get_description(self) -> str:
        return f"Workflow action: {self.params.get('action')}"

    async def execute(self, signal=None) -> ToolResult:
        action = str(self.params.get("action") or "")
        if action == "save":
            if self.save_error:
                return ToolResult.error("草案无效")
            MockWorkflowTool.saved_payloads.append(self.params["definition"])
            MockWorkflowTool.source_subagents.append(str(self.params.get("source_subagent", "")))
            return ToolResult.success(
                "草案已保存，draft_id: draft-test-001",
                metadata={"draft_id": "draft-test-001", "status": "saved"},
            )
        if action == "run":
            return ToolResult.success("任务已提交", metadata={"task_id": "wf-test-001"})
        return ToolResult.error(f"Unexpected workflow action in mock: {action}")


class RepeatedSuccessTool(EchoTool):
    def create_invocation(self, params: dict[str, str]) -> ToolInvocation:
        if "text" not in params:
            raise ValueError("Missing required parameter: text")
        return RepeatedSuccessToolInvocation(params)


class RepeatedSuccessToolInvocation(EchoToolInvocation):
    async def execute(self, signal=None) -> ToolResult:
        return ToolResult.success("same-result", metadata={"subagent_id": self.text})


class BackgroundDelegateTool(BaseTool):
    name = "delegate"
    description = "Launch a background delegate task."
    display_name = "Delegate"
    kind = ToolKind.EXECUTE
    parameters_schema = {
        "type": "object",
        "properties": {
            "description": {"type": "string"},
            "prompt": {"type": "string"},
            "subagent_type": {"type": "string"},
            "background": {"type": "boolean"},
        },
        "required": ["description", "prompt", "subagent_type"],
    }

    def create_invocation(self, params: dict[str, object]) -> ToolInvocation:
        return BackgroundDelegateInvocation(params)


class BackgroundDelegateInvocation(ToolInvocation):
    def get_description(self) -> str:
        return "Launch background delegate"

    async def execute(self, signal=None) -> ToolResult:
        return ToolResult.success(
            "后台任务已启动 [bg-aide-test]，aide 正在处理中，完成后会通知你。",
            metadata={
                "mode": "background",
                "task_id": "bg-aide-test",
                "subagent_type": "aide",
                "description": "塔防游戏开发规划",
            },
        )


class ForegroundDelegateTool(BaseTool):
    name = "delegate"
    description = "Return a foreground delegate result."
    display_name = "Delegate"
    kind = ToolKind.EXECUTE
    parameters_schema = {
        "type": "object",
        "properties": {
            "description": {"type": "string"},
            "prompt": {"type": "string"},
            "subagent_type": {"type": "string"},
        },
        "required": ["description", "prompt", "subagent_type"],
    }

    def create_invocation(self, params: dict[str, object]) -> ToolInvocation:
        return ForegroundDelegateInvocation(params)


class ForegroundDelegateInvocation(ToolInvocation):
    def get_description(self) -> str:
        return "Return foreground delegate result"

    async def execute(self, signal=None) -> ToolResult:
        return ToolResult.success(
            "整个工程的架构为：Root + WorkflowEngine",
            metadata={
                "subagent_type": "explore",
                "subagent_name": "explore",
                "description": "分析整个工程",
            },
        )


class PendingTodoCoara(CoaraBase):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        from src.todos.turn_control import TurnAction, TurnController, TurnDecision

        class _AlwaysPendingTurnController(TurnController):
            def decide_after_llm(self, state: TurnState) -> TurnDecision:
                if state.has_tool_calls:
                    return TurnDecision(action=TurnAction.CONTINUE, reason="tool_calls_present")
                return TurnDecision(action=TurnAction.CONTINUE, reason="todo_incomplete")

        self.turn_controller = _AlwaysPendingTurnController()


def _seed_todos(coara: CoaraBase, tmp_path: Path, todos: list[TodoItem]) -> None:
    store = TodoStore(workspace_dir=tmp_path, session_id=coara.session_id)
    store.merge_updates(todos)


@pytest.mark.asyncio
async def test_coara_processes_tool_calls_and_returns_final_answer(tmp_path: Path) -> None:
    provider = FakeProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(id="call-1", name="echo", arguments={"text": "alpha"}),
                    ToolCall(id="call-2", name="echo", arguments={"text": "beta"}),
                ],
            ),
            LLMResponse(content="final answer"),
        ]
    )

    coara = make_test_coara(tmp_path, provider=provider)
    coara.register_tool(EchoTool())
    await coara.initialize()

    chunks = [chunk async for chunk in coara.process_message("hello")]

    # Chunks: completion marker ×2 + final answer (pre-execution intent was removed)
    assert len(chunks) == 3
    assert "✓ echo(" in chunks[0]
    assert "✓ echo(" in chunks[1]
    assert chunks[2] == "final answer"

    tool_results = [message for message in coara.message_history if message.role == MessageRole.TOOL_RESULT]
    # Tool results are now wrapped with <system> tags by _wrap_tool_result
    assert len(tool_results) == 2
    assert "alpha" in str(tool_results[0].content)
    assert "beta" in str(tool_results[1].content)


@pytest.mark.asyncio
async def test_background_delegate_keeps_parent_turn_alive(tmp_path: Path) -> None:
    """Background delegate no longer ends the parent turn; parent continues."""
    provider = FakeProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="delegate",
                        arguments={
                            "description": "塔防游戏开发规划",
                            "prompt": "开发一个塔防游戏",
                            "subagent_type": "aide",
                            "background": True,
                        },
                    )
                ],
            ),
            LLMResponse(content="已安排 aide 后台分析，我会继续推进。"),
        ]
    )

    coara = make_test_coara(tmp_path, provider=provider)
    coara.register_tool(BackgroundDelegateTool())
    await coara.initialize()

    chunks = [chunk async for chunk in coara.process_message("delegate 一个 aide")]

    assert "已安排 aide 后台分析" in "".join(chunks)
    # Background delegate should not leave tool-result or assistant noise.
    # (The tool result content "后台任务已启动 …" is erased from history.)
    assert not any(
        message.role == MessageRole.TOOL_RESULT and "后台任务已启动" in str(message.content)
        for message in coara.message_history
    )
    assert not any(
        message.role == MessageRole.ASSISTANT and "后台任务已启动" in str(message.content)
        for message in coara.message_history
    )
    assert not any(
        message.role == MessageRole.ASSISTANT
        and message.tool_calls
        and any(tool_call.name == "delegate" for tool_call in message.tool_calls)
        for message in coara.message_history
    )
    assert any(
        message.role == MessageRole.USER and message.content == "delegate 一个 aide"
        for message in coara.message_history
    )
    # A system-info reminder about launched background tasks is injected.
    assert any(
        message.role == MessageRole.USER and "后台子代理任务已启动" in str(message.content)
        for message in coara.message_history
    )


@pytest.mark.asyncio
async def test_foreground_delegate_result_is_tagged_with_concrete_subagent_name(tmp_path: Path) -> None:
    provider = FakeProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="delegate",
                        arguments={
                            "description": "分析整个工程",
                            "prompt": "分析整个工程",
                            "subagent_type": "explore",
                        },
                    )
                ],
            ),
            LLMResponse(content="收到，继续分析。"),
        ]
    )

    coara = make_test_coara(tmp_path, provider=provider)
    coara.register_tool(ForegroundDelegateTool())
    await coara.initialize()

    chunks = [chunk async for chunk in coara.process_message("分析整个工程")]

    assert chunks[-1] == "收到，继续分析。"
    tool_results = [message for message in coara.message_history if message.role == MessageRole.TOOL_RESULT]
    assert len(tool_results) == 1
    # _format_tool_result_for_history now returns content block list
    content_blocks = tool_results[0].content
    assert isinstance(content_blocks, list)
    text_parts = " ".join(b["text"] for b in content_blocks if b.get("type") == "text")
    # Subagent name prefix was removed; tool_call_id is sufficient for LLM correlation.
    assert "整个工程的架构为：Root + WorkflowEngine" in text_parts


@pytest.mark.asyncio
async def test_start_new_session_resets_deferred_tool_reveal_state(tmp_path: Path) -> None:
    from tests.test_tools.test_tool import _FakeDeferredTool

    coara = make_test_coara(tmp_path)
    coara._tool_manager.register_tool(_FakeDeferredTool())
    coara._tool_manager.reveal_tool("capture_fullscreen")

    assert "capture_fullscreen" not in {s["name"] for s in coara._tool_manager.get_deferred_tool_summaries(True)}

    await coara.start_new_session()

    assert coara._tool_manager._revealed == set()
    pool = coara._tool_manager.get_deferred_tool_summaries(True)
    assert any(s["name"] == "capture_fullscreen" for s in pool)


@pytest.mark.asyncio
async def test_start_new_session_clears_file_snapshot_state(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("alpha", encoding="utf-8")

    coara = make_test_coara(tmp_path)
    await coara.initialize()

    read_tool = coara._tool_manager.tools["read"]
    write_tool = coara._tool_manager.tools["write"]

    initial_read = await read_tool.create_invocation({"path": str(target)}).execute()
    assert not initial_read.is_error

    previous_session_id = coara.session_id
    new_session_id = await coara.start_new_session()

    write_result = await write_tool.create_invocation({"path": str(target), "contents": "beta"}).execute()

    assert new_session_id != previous_session_id
    assert not write_result.is_error
    assert target.read_text(encoding="utf-8") == "beta"


@pytest.mark.asyncio
async def test_in_process_coaras_do_not_share_file_snapshot_state(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("alpha", encoding="utf-8")

    coara_a = make_test_coara(tmp_path, name="CoaraA")
    coara_b = make_test_coara(tmp_path, name="CoaraB")
    await coara_a.initialize()
    await coara_b.initialize()

    read_a = coara_a._tool_manager.tools["read"]
    write_b = coara_b._tool_manager.tools["write"]

    read_result = await read_a.create_invocation({"path": str(target)}).execute()
    write_result = await write_b.create_invocation({"path": str(target), "contents": "beta"}).execute()

    assert not read_result.is_error
    assert not write_result.is_error
    assert target.read_text(encoding="utf-8") == "beta"


@pytest.mark.asyncio
async def test_tool_executor_allows_tool_to_disable_outer_timeout(tmp_path: Path) -> None:
    coara = make_test_coara(tmp_path)
    coara.tool_executor = ToolExecutor(default_timeout=0.01)
    coara.register_tool(NoTimeoutSlowTool())
    await coara.initialize()

    executions = await coara.tool_executor.execute(
        coara,
        [ToolCall(id="call-1", name="echo", arguments={"text": "done"})],
        False,
    )

    assert len(executions) == 1
    assert executions[0].result.is_error is False
    assert executions[0].result.content == "done"


@pytest.mark.asyncio
async def test_coara_respects_custom_max_tool_iterations(tmp_path: Path) -> None:
    provider = FakeProvider(
        [
            LLMResponse(content="", tool_calls=[ToolCall(id="call-1", name="echo", arguments={"text": "step-1"})]),
            LLMResponse(content="", tool_calls=[ToolCall(id="call-2", name="echo", arguments={"text": "step-2"})]),
        ]
    )

    coara = CoaraBase(
        name="LimitedCoara",
        persona=CoaraPersona(name="LimitedCoara", role="tester"),
        workspace_dir=tmp_path,
        provider=provider,
        max_tool_iterations=2,
    )
    coara.register_tool(EchoTool())
    await coara.initialize()

    chunks = [chunk async for chunk in coara.process_message("hello")]

    assert chunks[-1] == "Stopped after reaching the maximum tool iterations (2)."
    assert coara.status.value == "failed"


@pytest.mark.parametrize("explicit_none", [True, False])
@pytest.mark.asyncio
async def test_coara_unbounded_iterations(tmp_path: Path, explicit_none: bool) -> None:
    provider = FakeProvider(
        [
            LLMResponse(content="", tool_calls=[ToolCall(id="call-1", name="echo", arguments={"text": "step-1"})]),
            LLMResponse(content="", tool_calls=[ToolCall(id="call-2", name="echo", arguments={"text": "step-2"})]),
            LLMResponse(content="", tool_calls=[ToolCall(id="call-3", name="echo", arguments={"text": "step-3"})]),
            LLMResponse(content="final answer"),
        ]
    )

    kwargs: dict[str, object] = {"max_tool_iterations": None} if explicit_none else {}
    coara = CoaraBase(
        name="UnlimitedCoara",
        persona=CoaraPersona(name="UnlimitedCoara", role="tester"),
        workspace_dir=tmp_path,
        provider=provider,
        **kwargs,
    )
    coara.register_tool(EchoTool())
    await coara.initialize()

    chunks = [chunk async for chunk in coara.process_message("hello")]

    assert chunks[-1] == "final answer"
    assert all("maximum tool iterations" not in chunk for chunk in chunks)
    assert coara.status.value == "idle"


def test_root_coara_default_max_tool_iterations() -> None:
    from src.coara.root import RootCoara

    assert RootCoara._MAX_TOOL_ITERATIONS == 1200


@pytest.mark.asyncio
async def test_process_message_prefers_llm_context_compression_before_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.context.window import context_window_manager
    from src.llm.provider import LLMProvider, StreamChunk
    from src.llm.provider import LLMResponse as ProviderLLMResponse
    from src.llm.registry import provider_registry
    from src.llm.service import llm_service

    class CompressionProfileProvider(LLMProvider):
        def __init__(self) -> None:
            super().__init__(name="compression", api_key="test", default_model="compression-model")
            self.calls = 0

        async def complete(self, *args, **kwargs) -> ProviderLLMResponse:
            self.calls += 1
            return ProviderLLMResponse(content="<state_snapshot><overall_goal>压缩摘要</overall_goal></state_snapshot>")

        async def stream_complete(self, *args, **kwargs):
            yield StreamChunk(delta_content="")

        def get_context_window(self, model: str | None = None) -> int:
            return 15_000

        async def close(self) -> None:
            pass

        def abort(self) -> None:
            pass

    compression_provider = CompressionProfileProvider()
    provider_registry.clear()
    provider_registry.register("compression", compression_provider)

    try:
        provider = CompressionCapturingProvider(
            responses=[LLMResponse(content="压缩后继续回答")],
            context_window=18_000,
        )
        coara = CoaraBase(
            name="compressor",
            persona=CoaraPersona(name="compressor", role="assistant"),
            workspace_dir=tmp_path,
            provider=provider,
        )
        await coara.initialize()
        monkeypatch.setattr(context_window_manager, "compression_threshold", 0.01)
        llm_service.configure_test_profiles(
            default_provider="compression",
            default_model="compression-model",
        )
        coara.message_history = [
            Message(
                role=MessageRole.USER if i % 2 == 0 else MessageRole.ASSISTANT,
                content=f"历史消息-{i}-" + ("内容" * 90),
            )
            for i in range(12)
        ]

        chunks = [chunk async for chunk in coara.process_message("继续")]

        assert chunks[-1] == "压缩后继续回答"
        assert compression_provider.calls >= 1
        assert any("<state_snapshot>" in str(message.content) for message in coara.message_history)
    finally:
        provider_registry.clear()
        llm_service.reset_for_tests()


@pytest.mark.asyncio
async def test_coara_stops_on_repeated_non_progressing_todo_turns(tmp_path: Path) -> None:
    """TurnStagnationGuard no longer fires on text-only repeated responses.

    The guard was redesigned to only fire on repeated tool errors. Text-only
    turns (no tool calls) continue until max_tool_iterations is reached.
    """
    provider = FakeProvider([LLMResponse(content="still thinking") for _ in range(5)])

    coara = PendingTodoCoara(
        name="PendingTodoCoara",
        persona=CoaraPersona(name="PendingTodoCoara", role="tester"),
        workspace_dir=tmp_path,
        provider=provider,
        max_tool_iterations=5,
    )
    await coara.initialize()

    chunks = [chunk async for chunk in coara.process_message("hello")]

    assert chunks[-1].startswith("Stopped after reaching the maximum tool iterations")
    assert coara.status.value == "failed"


@pytest.mark.asyncio
async def test_coara_stops_after_consecutive_all_error_tool_turns(tmp_path: Path) -> None:
    # All calls use the SAME arguments so build_error_signature produces the
    # same hash each turn — this is what triggers the repeated-error guard.
    provider = FakeProvider(
        [
            LLMResponse(content="", tool_calls=[ToolCall(id="call-1", name="echo", arguments={"text": "same"})]),
            LLMResponse(content="", tool_calls=[ToolCall(id="call-2", name="echo", arguments={"text": "same"})]),
            LLMResponse(content="", tool_calls=[ToolCall(id="call-3", name="echo", arguments={"text": "same"})]),
            LLMResponse(content="", tool_calls=[ToolCall(id="call-4", name="echo", arguments={"text": "same"})]),
        ]
    )

    coara = make_test_coara(tmp_path, provider=provider)
    coara.register_tool(ErrorTool())
    await coara.initialize()

    chunks = [chunk async for chunk in coara.process_message("hello")]

    assert chunks[-1].startswith("Detected")
    assert "consecutive" in chunks[-1]
    assert coara.status.value == "failed"


@pytest.mark.asyncio
async def test_repeated_success_with_only_volatile_metadata_does_not_count_as_progress(tmp_path: Path) -> None:
    """Repeated success with only volatile metadata does not trigger stagnation.

    TurnStagnationGuard only fires on repeated tool errors. Repeated successful
    tool calls — even with identical progress signatures — continue until
    max_tool_iterations.
    """
    provider = FakeProvider(
        [
            LLMResponse(content="", tool_calls=[ToolCall(id="call-1", name="echo", arguments={"text": "1"})]),
            LLMResponse(content="", tool_calls=[ToolCall(id="call-2", name="echo", arguments={"text": "2"})]),
            LLMResponse(content="", tool_calls=[ToolCall(id="call-3", name="echo", arguments={"text": "3"})]),
            LLMResponse(content="", tool_calls=[ToolCall(id="call-4", name="echo", arguments={"text": "4"})]),
            LLMResponse(content="", tool_calls=[ToolCall(id="call-5", name="echo", arguments={"text": "5"})]),
            LLMResponse(content="", tool_calls=[ToolCall(id="call-6", name="echo", arguments={"text": "6"})]),
            LLMResponse(content="", tool_calls=[ToolCall(id="call-7", name="echo", arguments={"text": "7"})]),
        ]
    )

    coara = CoaraBase(
        name="RepeatedSuccessCoara",
        persona=CoaraPersona(name="RepeatedSuccessCoara", role="tester"),
        workspace_dir=tmp_path,
        provider=provider,
        max_tool_iterations=7,
    )
    coara.register_tool(RepeatedSuccessTool())
    await coara.initialize()

    chunks = [chunk async for chunk in coara.process_message("hello")]

    # Stagnation guard does not fire for repeated success; hits iteration limit.
    assert chunks[-1].startswith("Stopped after reaching the maximum tool iterations")
    assert coara.status.value == "failed"


def test_turn_controller_keeps_loop_alive_when_todo_incomplete() -> None:
    controller = TurnController()
    controller.reset_turn()
    todo = TodoLoopState(
        total=2,
        completed=1,
        has_active=False,
        has_pending=True,
        progress_key="c1|f0|2:pending:",
    )

    decision = controller.decide_after_llm(
        TurnState(
            has_tool_calls=False,
            todo=todo,
            assistant_text_chars=50,
        )
    )

    assert decision.action == TurnAction.CONTINUE
    assert decision.reason == "todo_incomplete"
    assert controller.todo_stall_streak == 1


def test_turn_controller_stops_todo_incomplete_spin_without_progress() -> None:
    controller = TurnController()
    controller.reset_turn()
    todo = TodoLoopState(
        total=2,
        completed=1,
        has_active=True,
        has_pending=False,
        progress_key="c1|f0|7:in_progress:",
    )

    # Different short texts, same todo fingerprint → stop on the second continue.
    first = controller.decide_after_llm(
        TurnState(
            has_tool_calls=False,
            todo=todo,
            assistant_text_chars=20,
            assistant_text="先看一眼待办",
        )
    )
    second = controller.decide_after_llm(
        TurnState(
            has_tool_calls=False,
            todo=todo,
            assistant_text_chars=18,
            assistant_text="再确认一下进度",
        )
    )

    assert first.action == TurnAction.CONTINUE
    assert first.reason == "todo_incomplete"
    assert second.action == TurnAction.RETURN_FINAL
    assert second.reason == "todo_incomplete_stalled"


def test_turn_controller_stops_on_duplicate_continue_text() -> None:
    """Consecutive identical short replies → stop even if stall streak is still 1."""
    controller = TurnController()
    controller.reset_turn()
    todo = TodoLoopState(
        total=1,
        completed=0,
        has_active=True,
        has_pending=False,
        progress_key="c0|f0|1:in_progress:",
    )

    first = controller.decide_after_llm(
        TurnState(
            has_tool_calls=False,
            todo=todo,
            assistant_text_chars=12,
            assistant_text="先记一笔",
        )
    )
    second = controller.decide_after_llm(
        TurnState(
            has_tool_calls=False,
            todo=todo,
            assistant_text_chars=14,
            assistant_text="  先记一笔\n",  # whitespace-normalized match
        )
    )

    assert first.action == TurnAction.CONTINUE
    assert first.reason == "todo_incomplete"
    assert second.action == TurnAction.RETURN_FINAL
    assert second.reason == "todo_incomplete_duplicate_reply"


def test_turn_controller_resets_stall_when_todo_progress_key_changes() -> None:
    controller = TurnController()
    controller.reset_turn()
    first_todo = TodoLoopState(
        total=1, completed=0, has_active=True, has_pending=False, progress_key="c0|f0|1:in_progress:"
    )
    progressed = TodoLoopState(
        total=1, completed=0, has_active=True, has_pending=False, progress_key="c0|f0|1:in_progress:did step"
    )

    assert (
        controller.decide_after_llm(TurnState(has_tool_calls=False, todo=first_todo, assistant_text_chars=10)).reason
        == "todo_incomplete"
    )
    # Same fingerprint again would stall; progress_key change starts a fresh streak.
    again = controller.decide_after_llm(TurnState(has_tool_calls=False, todo=progressed, assistant_text_chars=10))
    assert again.action == TurnAction.CONTINUE
    assert again.reason == "todo_incomplete"
    assert controller.todo_stall_streak == 1


def test_turn_controller_tool_calls_without_progress_do_not_reset_stall() -> None:
    """Polling tool calls (unchanged todo fingerprint) must not wash the stall
    streak — text → poll → text stops the spin instead of looping forever."""
    controller = TurnController()
    controller.reset_turn()
    todo = TodoLoopState(total=1, completed=0, has_active=True, has_pending=False, progress_key="c0|f0|1:in_progress:")

    # Tool-call iteration (e.g. launch/poll something): continues, streak 0.
    tool_turn = controller.decide_after_llm(TurnState(has_tool_calls=True, todo=todo))
    assert tool_turn.reason == "tool_calls_present"

    # First short text-only reply → nudge, streak 1.
    first = controller.decide_after_llm(
        TurnState(has_tool_calls=False, todo=todo, assistant_text_chars=20, assistant_text="还在跑")
    )
    assert first.action == TurnAction.CONTINUE
    assert first.reason == "todo_incomplete"
    assert controller.todo_stall_streak == 1

    # Polling tool call with unchanged fingerprint → streak NOT reset.
    poll = controller.decide_after_llm(TurnState(has_tool_calls=True, todo=todo))
    assert poll.reason == "tool_calls_present"
    assert controller.todo_stall_streak == 1

    # Second short text-only reply with still no progress → stop the spin.
    second = controller.decide_after_llm(
        TurnState(has_tool_calls=False, todo=todo, assistant_text_chars=18, assistant_text="继续等构建")
    )
    assert second.action == TurnAction.RETURN_FINAL
    assert second.reason == "todo_incomplete_stalled"


def test_turn_controller_tool_calls_with_progress_reset_stall() -> None:
    """A tool-call iteration that advanced the todo fingerprint earns a fresh streak."""
    controller = TurnController()
    controller.reset_turn()
    todo = TodoLoopState(total=1, completed=0, has_active=True, has_pending=False, progress_key="c0|f0|1:in_progress:")
    progressed = TodoLoopState(
        total=1, completed=0, has_active=True, has_pending=False, progress_key="c0|f0|1:in_progress:did step"
    )

    controller.decide_after_llm(
        TurnState(has_tool_calls=False, todo=todo, assistant_text_chars=10, assistant_text="看一眼")
    )
    assert controller.todo_stall_streak == 1

    # Tool-call iteration that advanced the todo (notes edited → key changed).
    controller.decide_after_llm(TurnState(has_tool_calls=True, todo=progressed))
    assert controller.todo_stall_streak == 0

    # Next short text starts a fresh streak instead of tripping the old one.
    nxt = controller.decide_after_llm(
        TurnState(has_tool_calls=False, todo=progressed, assistant_text_chars=10, assistant_text="记一笔")
    )
    assert nxt.action == TurnAction.CONTINUE
    assert nxt.reason == "todo_incomplete"
    assert controller.todo_stall_streak == 1


def test_turn_controller_allows_substantial_reply_when_todo_incomplete() -> None:
    controller = TurnController()

    decision = controller.decide_after_llm(
        TurnState(
            has_tool_calls=False,
            todo=TodoLoopState(total=2, completed=1, has_active=True, has_pending=False),
            assistant_text_chars=TODO_SUBSTANTIAL_DELIVERY_CHARS,
            assistant_text="x" * TODO_SUBSTANTIAL_DELIVERY_CHARS,
        )
    )

    assert decision.action == TurnAction.RETURN_FINAL
    assert decision.reason == "todo_incomplete_substantial_reply"


def test_turn_controller_allows_exit_when_no_todo_left() -> None:
    controller = TurnController()

    decision = controller.decide_after_llm(
        TurnState(
            has_tool_calls=False,
            todo=TodoLoopState(total=2, completed=2, has_active=False, has_pending=False),
        )
    )

    assert decision.action == TurnAction.RETURN_FINAL
    assert decision.reason == "no_tool_calls_and_no_pending_state"


@pytest.mark.asyncio
async def test_process_message_continues_after_plain_text_when_todo_incomplete(tmp_path: Path) -> None:
    provider = CapturingProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="todo-1",
                        name="todo",
                        arguments={
                            "action": "update",
                            "description": "测试：记录待办",
                            "todos": [
                                {"id": "1", "content": "收集比亚迪关键数据", "status": "pending", "priority": "high"},
                            ],
                        },
                    )
                ],
            ),
            LLMResponse(content="先汇报一个阶段性判断"),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="todo-2",
                        name="todo",
                        arguments={
                            "action": "update",
                            "description": "测试：标记完成",
                            "todos": [
                                {"id": "1", "content": "收集比亚迪关键数据", "status": "completed"},
                            ],
                        },
                    )
                ],
            ),
            LLMResponse(content="最终完成"),
        ]
    )
    coara = make_test_coara(tmp_path, name="TodoCoara", provider=provider)
    await coara.initialize()

    chunks = [chunk async for chunk in coara.process_message("开始")]

    assert chunks[-1] == "最终完成"
    assert "先汇报一个阶段性判断" not in chunks
    assert len(provider.calls) == 4


@pytest.mark.asyncio
async def test_process_message_delivers_substantial_reply_when_todo_incomplete(tmp_path: Path) -> None:
    report = "结构化解读报告：" + ("内容段落。" * 80)
    provider = CapturingProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="todo-1",
                        name="todo",
                        arguments={
                            "action": "update",
                            "description": "测试：记录待办",
                            "todos": [
                                {
                                    "id": "7",
                                    "content": "撰写结构化解读报告",
                                    "status": "in_progress",
                                    "priority": "high",
                                },
                            ],
                        },
                    )
                ],
            ),
            LLMResponse(content=report),
        ]
    )
    coara = make_test_coara(tmp_path, name="TodoCoara", provider=provider)
    await coara.initialize()

    chunks = [chunk async for chunk in coara.process_message("开始")]

    assert chunks[-1] == report
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_process_message_stops_short_todo_spin_without_progress(tmp_path: Path) -> None:
    """Two short text-only replies with unchanged todos must end the turn (no LLM thrash)."""
    provider = CapturingProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="todo-1",
                        name="todo",
                        arguments={
                            "action": "update",
                            "description": "测试：记录待办",
                            "todos": [
                                {
                                    "id": "1",
                                    "content": "推进任务",
                                    "status": "in_progress",
                                    "priority": "high",
                                },
                            ],
                        },
                    )
                ],
            ),
            LLMResponse(content="先记一笔"),
            LLMResponse(content="再记一笔"),
            LLMResponse(content="should-not-run"),
        ]
    )
    coara = make_test_coara(tmp_path, name="TodoStall", provider=provider)
    await coara.initialize()

    chunks = [chunk async for chunk in coara.process_message("开始")]

    assert chunks[-1].strip() == "再记一笔"
    assert len(provider.calls) == 3
    # Stall nudge injected once after the first short continue.
    assert any(
        isinstance(m.content, str) and "待办仍有未完成项" in m.content
        for m in coara.message_history
        if m.role == MessageRole.USER
    )


@pytest.mark.asyncio
async def test_process_message_todo_park_ends_turn_immediately(tmp_path: Path) -> None:
    """todo(action="park") is a one-shot close: the closing message lives in the
    park call itself, the turn ends right after the tool batch — no extra LLM
    round, no reminder injection. The open todos stay open (park is not completion)."""
    provider = CapturingProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="todo-1",
                        name="todo",
                        arguments={
                            "action": "update",
                            "description": "测试：记录待办",
                            "todos": [{"id": "1", "content": "等后台构建", "status": "in_progress"}],
                        },
                    )
                ],
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="todo-2",
                        name="todo",
                        arguments={
                            "action": "park",
                            "description": "等后台构建完成",
                            "message": "构建在后台运行，完成后我会继续。",
                            "todos": [{"id": "1", "content": "等后台构建", "status": "in_progress"}],
                        },
                    )
                ],
            ),
            LLMResponse(content="should-not-run"),
        ]
    )
    coara = make_test_coara(tmp_path, name="TodoPark", provider=provider)
    await coara.initialize()

    chunks = [chunk async for chunk in coara.process_message("开始构建")]

    # Turn ended right after the park tool batch: only 2 LLM calls.
    assert len(provider.calls) == 2
    # 结束语是 park 参数，不经 LLM 流式通道，由编排器直接交付。
    assert any("构建在后台运行，完成后我会继续。" in chunk for chunk in chunks)
    # 结束语以 assistant 正文入史（在 park 工具结果之后）。
    last = coara.message_history[-1]
    assert last.role == MessageRole.ASSISTANT
    assert "构建在后台运行，完成后我会继续。" in str(last.content)
    assert not any(
        isinstance(m.content, str) and "待办仍有未完成项" in m.content
        for m in coara.message_history
        if m.role == MessageRole.USER
    )


@pytest.mark.asyncio
async def test_process_message_stops_consecutive_empty_replies_when_todo_incomplete(
    tmp_path: Path,
) -> None:
    """待办未完成时的空回复不会叠加：空响应在 provider 边界判死，回合立即收束。

    ReAct 文本协议已删除，``_complete_with_provider`` 不再有「空响应 → 回退重试」。
    现设计：finish_reason=stop 且零内容（无文本/工具/思考）由
    ``_ensure_non_empty_response`` 抛 EmptyResponseError（仅流式零 delta 会回退
    非流式重发一次），turn 层按不可重试的 LLM 错误收尾——status=FAILED、注入
    「该轮未产出回复」系统注记、给用户一行中文指引并结束回合。因此空回复既不会
    连续叠加，也不会留下空白 assistant 消息。turn 层的 todo_incomplete_stalled
    仍负责「非空短文本无进展」的停止判定。
    """
    provider = CapturingProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="todo-1",
                        name="todo",
                        arguments={
                            "action": "update",
                            "description": "测试：记录待办",
                            "todos": [
                                {
                                    "id": "1",
                                    "content": "推进任务",
                                    "status": "in_progress",
                                    "priority": "high",
                                },
                            ],
                        },
                    )
                ],
            ),
            # Short non-empty → continue once (todo_incomplete) + stall nudge
            LLMResponse(content="先记一笔"),
            # Empty reply → provider EMPTY_RESPONSE 判死 → 回合收束（不备第 4 条：
            # 多一次请求即 Provider 池空，用例立即炸出）
            LLMResponse(content=""),
        ]
    )
    coara = make_test_coara(tmp_path, name="TodoEmptyLoop", provider=provider)
    await coara.initialize()

    chunks = [chunk async for chunk in coara.process_message("开始")]

    assert "先记一笔" in "".join(chunks)
    # 空响应即终局：无重试，也不会再发第 4 次请求
    assert len(provider.calls) == 3
    assert chunks[-1].strip() == "Error: 模型返回了空内容。请重试，如果持续出现请尝试切换模型。"
    assert coara.status.value == "failed"
    # 失败注记入史：下一轮知道这条用户消息尚未被处理
    assert any(
        m.role == MessageRole.USER and isinstance(m.content, str) and "该轮未产出回复" in m.content
        for m in coara.message_history
    )
    # No stacked blank assistants left in history（空响应不落史）
    blank = [
        m
        for m in coara.message_history
        if m.role == MessageRole.ASSISTANT and not (m.content or "").strip() and not m.tool_calls
    ]
    assert blank == []


def test_stagnation_guard_only_fires_on_repeated_errors() -> None:
    """TurnStagnationGuard fires on repeated tool errors, not on text-only turns."""
    from src.agent.loop import TurnStagnationGuard

    guard = TurnStagnationGuard(repeated_error_threshold=3)

    # Non-error turns (error_signature=None) never trigger the guard.
    for _ in range(10):
        alert = guard.record(made_progress=False, error_signature=None)
        assert alert is None
    assert guard._consecutive_same_error_turns == 0

    # Repeated same error signature triggers after threshold.
    for _ in range(2):
        alert = guard.record(made_progress=False, error_signature="err-1")
        assert alert is None
    alert = guard.record(made_progress=False, error_signature="err-1")
    assert alert is not None
    assert alert.reason == "repeated_error"
    assert guard._consecutive_same_error_turns == 3

    # Different error signature resets the streak.
    guard.reset()
    guard.record(made_progress=False, error_signature="err-1")
    guard.record(made_progress=False, error_signature="err-2")
    assert guard._consecutive_same_error_turns == 1

    # Made progress resets the streak.
    guard.reset()
    guard.record(made_progress=False, error_signature="err-1")
    guard.record(made_progress=True, error_signature="err-1")
    assert guard._consecutive_same_error_turns == 0


@pytest.mark.asyncio
async def test_process_message_can_be_interrupted_during_llm_call(tmp_path: Path) -> None:
    provider = BlockingProvider()
    coara = make_test_coara(tmp_path, provider=provider)
    await coara.initialize()

    events = []
    coara.set_trace_sink(events.append)

    async def collect() -> list[str]:
        return [chunk async for chunk in coara.process_message("请开始")]

    task = asyncio.create_task(collect())
    await provider.started.wait()

    assert coara.has_active_turn()
    assert coara.interrupt_current_turn("user_escape")

    chunks = await asyncio.wait_for(task, timeout=1)

    assert chunks == ["[系统] 当前会话已打断。"]
    assert coara.status.value == "idle"
    assert not coara.has_active_turn()
    event_types = [event.event_type for event in events]
    assert "turn_interrupt_requested" in event_types
    assert "turn_interrupted" in event_types
    assert event_types.index("turn_interrupt_requested") < event_types.index("turn_interrupted")
    assert events[-1].event_type == "turn_end"
    # Interrupt marker is injected as <系统消息> for the next LLM turn.
    from src.core.message_tags import system_info

    assert any(
        message.role == MessageRole.USER and message.content == system_info("当前会话已打断。")
        for message in coara.message_history
    )
    # Interrupt keeps the user turn in history (no full-turn rollback).
    assert any(message.content == "请开始" for message in coara.message_history)


@pytest.mark.asyncio
async def test_start_new_session_interrupts_active_turn(tmp_path: Path) -> None:
    provider = BlockingProvider()
    coara = make_test_coara(tmp_path, provider=provider)
    await coara.initialize()

    async def collect() -> list[str]:
        return [chunk async for chunk in coara.process_message("请开始")]

    task = asyncio.create_task(collect())
    await provider.started.wait()
    old_session = coara.session_id

    new_session = await coara.start_new_session(interrupt_source="test_new_session")
    chunks = await asyncio.wait_for(task, timeout=2)
    # turn 内分支异步等锁清状态：等延迟收尾任务落完再断言
    deferred = coara._deferred_new_session_task
    assert deferred is not None
    await asyncio.wait_for(deferred, timeout=2)

    assert chunks == []
    assert new_session != old_session
    assert coara.session_id == new_session
    # After start_new_session, message_history contains only prefix context-module
    # seeds (env + ws overview by default; no real chat).
    from src.coara.injections.context_modules import is_context_module_seed
    from src.coara.injections.environment_injector import ENV_CONTEXT_PREFIX

    assert coara.message_history
    assert all(is_context_module_seed(str(m.content)) for m in coara.message_history)
    assert any(ENV_CONTEXT_PREFIX in str(m.content) for m in coara.message_history)


@pytest.mark.asyncio
async def test_new_session_inside_turn_does_not_pollute_new_history(tmp_path: Path) -> None:
    """REMAINING_ISSUES #2：回合进行中 /new，旧回合退出前追加的工具结果
    不得落进新会话已清空的 history——延迟到旧回合退出后再清"""
    SlowEchoToolInvocation.started = asyncio.Event()
    provider = FakeProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="call-1", name="echo", arguments={"text": "alpha"})],
            )
        ]
    )
    coara = make_test_coara(tmp_path, provider=provider)
    coara.register_tool(SlowEchoTool())
    await coara.initialize()

    async def collect() -> list[str]:
        return [chunk async for chunk in coara.process_message("开始")]

    task = asyncio.create_task(collect())
    # 等工具开始执行（abort 后旧回合仍会把「已被用户取消」的工具结果入史）
    await SlowEchoToolInvocation.started.wait()

    new_session = await coara.start_new_session(interrupt_source="test_new_session")
    await asyncio.wait_for(task, timeout=2)
    deferred = coara._deferred_new_session_task
    assert deferred is not None
    await asyncio.wait_for(deferred, timeout=2)

    assert coara.session_id == new_session
    from src.coara.injections.context_modules import is_context_module_seed
    from src.coara.injections.environment_injector import ENV_CONTEXT_PREFIX

    # 新会话 history 只剩前缀上下文模块：旧回合的工具结果/闭合注记全部被清掉
    assert coara.message_history
    assert all(is_context_module_seed(str(m.content)) for m in coara.message_history)
    assert any(ENV_CONTEXT_PREFIX in str(m.content) for m in coara.message_history)
    assert all(message.role != MessageRole.TOOL_RESULT for message in coara.message_history)


@pytest.mark.asyncio
async def test_interrupt_keeps_turn_and_closes_dangling_tool_calls(tmp_path: Path) -> None:
    SlowEchoToolInvocation.started = asyncio.Event()
    provider = FakeProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="call-1", name="echo", arguments={"text": "alpha"})],
            )
        ]
    )
    coara = make_test_coara(tmp_path, provider=provider)
    coara.register_tool(SlowEchoTool())
    await coara.initialize()

    async def collect() -> list[str]:
        return [chunk async for chunk in coara.process_message("开始")]

    task = asyncio.create_task(collect())
    await SlowEchoToolInvocation.started.wait()

    assert coara.interrupt_current_turn("user_escape")
    chunks = await asyncio.wait_for(task, timeout=1)

    assert chunks[-1] == "[系统] 当前会话已打断。"
    assert any(message.content == "开始" for message in coara.message_history)
    tool_results = [message for message in coara.message_history if message.role == MessageRole.TOOL_RESULT]
    assert len(tool_results) == 1
    assert tool_results[0].tool_call_id == "call-1"
    # 被取消的工具入史的是真实取消结果（不再是合成闭合文案）
    assert "已被用户取消" in str(tool_results[0].content)
    assistant_tool_calls = [
        message for message in coara.message_history if message.role == MessageRole.ASSISTANT and message.tool_calls
    ]
    assert len(assistant_tool_calls) == 1


@pytest.mark.asyncio
async def test_interrupt_keeps_history_so_next_request_can_continue(tmp_path: Path) -> None:
    SlowEchoToolInvocation.started = asyncio.Event()
    provider = FakeProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="call-1", name="echo", arguments={"text": "alpha"})],
            ),
            LLMResponse(content="恢复正常"),
        ]
    )
    coara = make_test_coara(tmp_path, provider=provider)
    coara.register_tool(SlowEchoTool())
    await coara.initialize()

    async def collect() -> list[str]:
        return [chunk async for chunk in coara.process_message("开始")]

    task = asyncio.create_task(collect())
    await SlowEchoToolInvocation.started.wait()

    assert coara.interrupt_current_turn("user_escape")
    chunks = await asyncio.wait_for(task, timeout=1)

    assert chunks[-1] == "[系统] 当前会话已打断。"
    assert any(message.content == "开始" for message in coara.message_history)

    follow_up_chunks = [chunk async for chunk in coara.process_message("你好")]

    assert follow_up_chunks == ["恢复正常"]
    user_contents = [message.content for message in coara.message_history if message.role == MessageRole.USER]
    assert "开始" in user_contents
    assert user_contents[-1] == "你好"


@pytest.mark.asyncio
async def test_unexpected_tool_execution_failure_rolls_back_partial_turn(tmp_path: Path) -> None:
    provider = FakeProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="call-1", name="echo", arguments={"text": "alpha"})],
            ),
            LLMResponse(content="恢复正常"),
        ]
    )
    coara = make_test_coara(tmp_path, provider=provider)
    await coara.initialize()

    async def explode(*args, **kwargs):
        raise RuntimeError("boom")

    coara.tool_executor.execute = explode  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="boom"):
        _ = [chunk async for chunk in coara.process_message("开始")]

    assert all(message.content != "开始" for message in coara.message_history)
    assistant_tool_calls = [
        message for message in coara.message_history if message.role == MessageRole.ASSISTANT and message.tool_calls
    ]
    assert assistant_tool_calls == []

    follow_up_chunks = [chunk async for chunk in coara.process_message("你好")]

    assert follow_up_chunks == ["恢复正常"]


@pytest.mark.asyncio
async def test_successful_save_via_orchestrator(tmp_path: Path) -> None:
    """orchestrator save 成功路径：Mock 工具收到定义载荷（守卫机制已随技能删除）。"""
    MockWorkflowTool.saved_payloads = []
    MockWorkflowTool.source_subagents = []
    sample_definition = "name: demo-flow\ndescription: demo\nsteps: {}\nedges: []\n"
    provider = FakeProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="save-1",
                        name="orchestrator",
                        arguments={"action": "save", "definition": sample_definition},
                    )
                ],
            ),
            LLMResponse(content="已保存", tool_calls=None),
        ]
    )
    coara = make_test_coara(tmp_path, name="test-root", provider=provider)
    coara.register_tool(MockWorkflowTool(), replace=True)
    await coara.initialize()
    coara._workflow_draft_pending = True

    [chunk async for chunk in coara.process_message("制作工作流")]
    assert MockWorkflowTool.saved_payloads == [sample_definition]


def test_toolresult_error_preserves_metadata() -> None:
    result = ToolResult.error(
        "草案无效",
        metadata={"draft_id": "draft-x", "reason": "flow empty"},
    )

    assert result.is_error is True
    assert result.content == "草案无效"
    assert result.metadata == {"draft_id": "draft-x", "reason": "flow empty"}


@pytest.mark.asyncio
async def test_truncated_tool_call_recovers_with_clear_feedback(tmp_path: Path) -> None:
    """e2e: truncated tool_call is skipped, reminder injected, next round finishes."""

    class _TrackingWriteTool(BaseTool):
        name = "write"
        description = "Tracking write tool"
        display_name = "Write"
        kind = ToolKind.EXECUTE
        parameters_schema = {
            "type": "object",
            "properties": {"path": {"type": "string"}, "contents": {"type": "string"}},
            "required": ["path", "contents"],
        }
        executed = False

        def create_invocation(self, params: dict[str, object]) -> ToolInvocation:
            return _TrackingWriteInvocation(params)

    class _TrackingWriteInvocation(ToolInvocation):
        def get_description(self) -> str:
            return "Write"

        async def execute(self, signal=None) -> ToolResult:
            _TrackingWriteTool.executed = True
            return ToolResult.success("ok")

    _TrackingWriteTool.executed = False
    provider = FakeProvider(
        [
            # Round 1: truncated tool_call (finish_reason=length)
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="write",
                        arguments={"path": str(tmp_path / "out.txt"), "contents": "a" * 5000},
                    )
                ],
                finish_reason="length",
                usage={"output_tokens": 16384},
            ),
            # Round 2: normal finish
            LLMResponse(content="done"),
        ]
    )

    coara = make_test_coara(tmp_path, provider=provider)
    coara.register_tool(_TrackingWriteTool())
    await coara.initialize()

    chunks = [chunk async for chunk in coara.process_message("write a big file")]

    # Tool was never executed (truncation recovery skipped dispatch)
    assert _TrackingWriteTool.executed is False
    # Reminder was injected into message_history
    reminders = [
        m for m in coara.message_history if m.role == MessageRole.USER and "参数 JSON 不完整" in str(m.content)
    ]
    assert len(reminders) >= 1
    # Tool was never executed, but the orphan tool_call is closed with a
    # synthetic "[未执行]" result so the next provider call stays valid.
    tool_results = [m for m in coara.message_history if m.role == MessageRole.TOOL_RESULT]
    assert len(tool_results) == 1
    assert tool_results[0].tool_call_id == "call-1"
    assert "[未执行]" in str(tool_results[0].content)
    # Second round finished normally
    assert chunks[-1] == "done"


class _BlockOnceProvider(BlockingProvider):
    """首轮 complete 永久阻塞（供 /new 中断），后续轮立即返回固定文本。"""

    async def complete(self, *args, **kwargs) -> LLMResponse:
        if self.started.is_set():
            return LLMResponse(content="收到")
        self.started.set()
        await asyncio.Future()
        raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_process_message_rejected_while_deferred_new_session_pending(tmp_path: Path) -> None:
    """REMAINING #349-a：回合内 /new 的延迟清理任务存活期间，process_message
    拒绝新回合并引导用户稍候（不接受理、不写历史）。"""
    coara = make_test_coara(tmp_path)
    await coara.initialize()

    pending = asyncio.create_task(asyncio.sleep(30))
    coara._deferred_new_session_task = pending
    try:
        chunks = [chunk async for chunk in coara.process_message("你好")]
        assert chunks == ["[系统] 正在开始新会话，请稍候几秒再发送消息。"]
        assert coara.message_history == []
    finally:
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending


@pytest.mark.asyncio
async def test_deferred_new_session_blocks_new_turn_until_cleanup_done(tmp_path: Path, monkeypatch) -> None:
    """REMAINING #349-a 竞态回归：延迟清理等锁期间若放行新消息，清理会排在
    新回合之后把它的历史连同新会话一起抹掉；钉住「清理完成后再受理」。"""
    provider = _BlockOnceProvider()
    coara = make_test_coara(tmp_path, provider=provider)
    await coara.initialize()

    gate = asyncio.Event()
    original_wait = coara._wait_for_process_lock_release

    async def gated_wait(**kwargs):
        await gate.wait()
        return await original_wait(**kwargs)

    monkeypatch.setattr(coara, "_wait_for_process_lock_release", gated_wait)

    async def collect() -> list[str]:
        return [chunk async for chunk in coara.process_message("请开始")]

    task = asyncio.create_task(collect())
    await provider.started.wait()
    old_session = coara.session_id

    new_session = await coara.start_new_session(interrupt_source="test_new_session")
    chunks = await asyncio.wait_for(task, timeout=2)
    assert chunks == []
    deferred = coara._deferred_new_session_task
    assert deferred is not None and not deferred.done()

    # 延迟清理存活期间：新消息被拒，不进历史
    refused = [chunk async for chunk in coara.process_message("清理期间的追问")]
    assert refused == ["[系统] 正在开始新会话，请稍候几秒再发送消息。"]
    assert all("清理期间的追问" not in str(message.content) for message in coara.message_history)

    # 放行清理：新会话生效，之后再发消息正常受理且历史不被抹掉
    gate.set()
    await asyncio.wait_for(deferred, timeout=2)
    assert coara.session_id == new_session != old_session

    follow_up = [chunk async for chunk in coara.process_message("恢复后的消息")]
    assert follow_up == ["收到"]
    assert any(message.content == "恢复后的消息" for message in coara.message_history)


@pytest.mark.asyncio
async def test_deferred_new_session_timeout_abandons_cleanup(tmp_path: Path, monkeypatch) -> None:
    """REMAINING #349-b：等锁超时（旧回合无视 abort 存活）时放弃清理并告警，
    不再清状态；旧会话现场保留，后续新消息恢复受理。"""
    provider = FakeProvider([LLMResponse(content="收到")])
    coara = make_test_coara(tmp_path, provider=provider)
    await coara.initialize()
    original_session = coara.session_id
    original_history_len = len(coara.message_history)

    async def fast_timeout_wait(**kwargs):
        return await CoaraBase._wait_for_process_lock_release(coara, timeout_seconds=0.05)

    monkeypatch.setattr(coara, "_wait_for_process_lock_release", fast_timeout_wait)

    # 锁住 process_lock 模拟旧回合无视 abort 存活：等锁必超时
    await coara._process_lock.acquire()
    try:
        await asyncio.wait_for(
            coara._finish_new_session_after_turn_exit("abandoned-session-id", "test_new_session"),
            timeout=2,
        )
    finally:
        coara._process_lock.release()

    # 超时放弃：session 与历史均未动
    assert coara.session_id == original_session
    assert len(coara.message_history) == original_history_len
    # 延迟任务已收官：后续新消息正常受理
    chunks = [chunk async for chunk in coara.process_message("你好")]
    assert chunks == ["收到"]
