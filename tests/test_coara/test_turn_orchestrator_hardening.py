"""turn_orchestrator 中断路径加固回归（REMAINING #350）：

- 中断收场时 apply_tool_results_to_history 若抛错，不得顶替原本的
  CancelledError / CoaraRunCancelledError（否则正常中断被降级成整轮回滚）
- 磁盘副作用登记对非 dict 的 tool_call.arguments 有 isinstance 守卫，
  不再因 .get AttributeError 把正常回合炸成整轮回滚
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.coara import turn_orchestrator
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


@pytest.mark.asyncio
async def test_interrupt_survives_history_apply_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#350-a：中断路径入史抛错只记 warning，中断语义不被掩盖——回合仍按
    「当前会话已打断」收口，而不是被顶替成异常整轮回滚。"""
    _EchoInvocation.finished = asyncio.Event()
    _SlowInvocation.started = asyncio.Event()

    def _exploding_apply(*args, **kwargs):
        raise RuntimeError("history write exploded")

    monkeypatch.setattr(turn_orchestrator, "apply_tool_results_to_history", _exploding_apply)

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
    # 防护生效：中断语义不被 RuntimeError 顶替，回合正常收口（不抛异常）
    chunks = await asyncio.wait_for(task, timeout=5)

    assert chunks[-1] == "[系统] 当前会话已打断。"


class _LenientWriteInvocation(ToolInvocation):
    def get_description(self) -> str:
        return "fake write"

    async def execute(self, signal=None) -> ToolResult:
        return ToolResult.success("written")


class _LenientWriteTool(BaseTool):
    name = "fake_write"
    description = "test write tool tolerating non-dict arguments"
    kind = ToolKind.EDIT

    def create_invocation(self, params) -> ToolInvocation:
        return _LenientWriteInvocation(params)


@pytest.mark.asyncio
async def test_disk_effect_recording_tolerates_non_dict_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#350-b：磁盘变更类工具的 arguments 非 dict 时副作用登记不炸——
    回合正常完成（isinstance 守卫生效，路径记为 ?）。"""
    monkeypatch.setattr(turn_orchestrator, "_DISK_MUTATING_TOOL_NAMES", frozenset({"fake_write"}))

    provider = FakeProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall.model_construct(id="call-write", name="fake_write", arguments=[["path", "a.txt"]])
                ],
            ),
            LLMResponse(content="完成"),
        ]
    )
    coara = make_test_coara(tmp_path, provider=provider)
    coara.register_tool(_LenientWriteTool())
    await coara.initialize()

    chunks = [chunk async for chunk in coara.process_message("开始")]

    assert chunks[-1] == "完成"
    tool_results = [m for m in coara.message_history if m.role == MessageRole.TOOL_RESULT]
    assert len(tool_results) == 1
    assert tool_results[0].tool_call_id == "call-write"
    assert "written" in str(tool_results[0].content)


class _FailingProvider(FakeProvider):
    """complete 直接抛 LLMError 的 provider（模拟模型调用失败）。"""

    def __init__(self, exc: LLMError):
        super().__init__([])
        self._exc = exc

    async def complete(self, *args, **kwargs):
        raise self._exc


class _FakeSessionLog:
    """捕获 turn_end reason 的假录像带。"""

    def __init__(self) -> None:
        self.turn_end_reasons: list[str] = []

    def record_turn_start(self, turn_id: str, source: str = "") -> None:
        pass

    def record_turn_end(self, turn_id: str, reason: str) -> None:
        self.turn_end_reasons.append(reason)


@pytest.mark.asyncio
async def test_llm_failure_turn_records_failed(tmp_path: Path) -> None:
    """#20：LLM 失败路径（yield Error 后正常 return、无异常传播）
    session_log 仍记 failed——显式失败标记，不依赖 sys.exc_info()。"""
    provider = _FailingProvider(LLMError("provider exploded"))
    coara = make_test_coara(tmp_path, provider=provider)
    await coara.initialize()
    fake_log = _FakeSessionLog()
    coara._session_log = fake_log

    chunks = [chunk async for chunk in coara.process_message("开始")]

    assert any(chunk.startswith("Error:") for chunk in chunks)
    assert fake_log.turn_end_reasons == ["failed"]


@pytest.mark.asyncio
async def test_unexpected_error_turn_records_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#15：未预期异常整轮回滚的回合 session_log 记 failed 而非 completed。"""
    provider = FakeProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(id="call-1", name="write", arguments={"path": str(tmp_path / "x.txt"), "contents": "y"})
                ],
            )
        ]
    )
    coara = make_test_coara(tmp_path, provider=provider)
    await coara.initialize()
    fake_log = _FakeSessionLog()
    coara._session_log = fake_log

    def _boom(*args, **kwargs):
        raise RuntimeError("history apply exploded")

    monkeypatch.setattr(turn_orchestrator, "apply_tool_results_to_history", _boom)

    with pytest.raises(RuntimeError, match="history apply exploded"):
        _ = [chunk async for chunk in coara.process_message("写文件")]

    assert fake_log.turn_end_reasons == ["failed"]


@pytest.mark.asyncio
async def test_interrupt_note_lists_executed_disk_effects(tmp_path: Path) -> None:
    """#17：工具中断路径与正常完成同口径登记副作用——打断注记附本批
    已执行清单（write 已落盘不得被下一轮模型当成未发生）。"""
    _SlowInvocation.started = asyncio.Event()
    target = tmp_path / "interrupted-write.txt"

    provider = FakeProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(id="call-write", name="write", arguments={"path": str(target), "contents": "已落盘"}),
                    ToolCall(id="call-slow", name="slow_echo", arguments={"text": "beta"}),
                ],
            )
        ]
    )
    coara = make_test_coara(tmp_path, provider=provider)
    coara.register_tool(_SlowTool())
    await coara.initialize()

    async def collect() -> list[str]:
        return [chunk async for chunk in coara.process_message("开始")]

    task = asyncio.create_task(collect())
    await _SlowInvocation.started.wait()
    # 等 write 实际落盘后再打断，保证其结果进入 interrupted_executions
    for _ in range(200):
        if target.exists():
            break
        await asyncio.sleep(0.01)
    assert target.exists()

    assert coara.interrupt_current_turn("user_escape")
    chunks = await asyncio.wait_for(task, timeout=5)

    assert chunks[-1] == "[系统] 当前会话已打断。"
    note = next(m for m in reversed(coara.message_history) if m.role == MessageRole.USER and "已打断" in str(m.content))
    assert "打断前已完成的工具操作" in note.content
    assert f"- write: {target}" in note.content
    assert "仍然生效" in note.content


@pytest.mark.asyncio
async def test_continuation_injected_carries_subagent_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """子智能体回显来源契约：continuation_input_injected 的 subagent_texts
    必须带 subagent_sources（跟随注入端）——web 回合里前台 delegate 收官，
    结果来源 = web，CLI 端据此不镜像（各端显示独立的回显半边）。"""
    from src.core.message_tags import system_info

    provider = FakeProvider(
        [
            LLMResponse(content="第一轮"),
            LLMResponse(content="已处理子智能体结果"),
        ]
    )
    coara = make_test_coara(tmp_path, provider=provider)
    await coara.initialize()

    captured: dict = {}
    original_emit = coara._emit_trace

    def _capturing(event_type: str, message: str, **kwargs):
        if event_type == "continuation_input_injected":
            captured["payload"] = kwargs.get("payload") or {}
        return original_emit(event_type, message, **kwargs)

    monkeypatch.setattr(coara, "_emit_trace", _capturing)

    coara.submit_continuation_input(
        system_info("[前台子智能体已完成] [sa-coaras-ab12]\n任务：摸底仓库\n结果：找到 3 个入口文件")
    )
    _ = [chunk async for chunk in coara.process_message("开始", source="web")]

    assert captured, "continuation_input_injected 事件未发出"
    payload = captured["payload"]
    assert len(payload.get("subagent_texts") or []) == 1
    assert payload.get("subagent_sources") == ["web"]
    assert "找到 3 个入口文件" in payload["subagent_texts"][0]


@pytest.mark.asyncio
async def test_plan_pending_approval_lock_full_turn_cycle(tmp_path: Path) -> None:
    """待批准锁整回合集成：submit 成功 → PlanSubmittedError 正常收尾（非打断，
    历史闭合、不注入打断注记）；用户下一条消息（带来源的新回合）清锁后
    exit 恢复可用。"""
    plan_file = tmp_path / "plan.md"
    provider = FakeProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call-submit",
                        name="plan_mode",
                        arguments={"action": "submit", "content": "## 方案 v1"},
                    )
                ],
            ),
            # 用户下一条消息后的新回合：exit 放行
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="call-exit", name="plan_mode", arguments={"action": "exit"})],
            ),
            LLMResponse(content="已批准，开始执行"),
        ]
    )
    coara = make_test_coara(tmp_path, provider=provider)
    coara.identity.is_owner_context = True  # plan_mode 是 owner_only 工具
    from src.tools.builtin.planning.plan_mode import PlanModeTool

    coara.register_tool(PlanModeTool(parent_coara=coara))
    coara.enter_plan_mode(plan_file)
    await coara.initialize()

    # 回合一：submit 成功置锁 → PlanSubmittedError 正常收尾（非打断注记）
    chunks = [chunk async for chunk in coara.process_message("提交计划", source="cli")]
    assert "[系统] 当前会话已打断。" not in chunks
    assert coara._plan_pending_approval is True
    assert coara.is_plan_mode is True
    assert plan_file.read_text(encoding="utf-8") == "## 方案 v1"
    # 历史闭合：submit 的 tool_call 有匹配的 plan_mode 结果（非悬空）
    tool_results = [m for m in coara.message_history if m.role == MessageRole.TOOL_RESULT and m.name == "plan_mode"]
    assert any(m.tool_call_id == "call-submit" for m in tool_results)

    # 回合二：用户消息（带来源）清锁，exit 放行
    _ = [chunk async for chunk in coara.process_message("批准，开始", source="cli")]
    assert coara._plan_pending_approval is False
    assert coara.is_plan_mode is False


@pytest.mark.asyncio
async def test_plan_submit_closes_concurrent_tool_calls(tmp_path: Path) -> None:
    """submit 与无锁只读工具同轮并发：submit 收尾后，兄弟工具的 tool_call 被
    闭合为「未执行」（不留悬空——provider 会因 orphan tool_call 拒掉下一轮）。"""
    plan_file = tmp_path / "plan.md"
    provider = FakeProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(id="call-echo", name="echo", arguments={"text": "hi"}),
                    ToolCall(
                        id="call-submit",
                        name="plan_mode",
                        arguments={"action": "submit", "content": "## 方案"},
                    ),
                ],
            ),
        ]
    )
    coara = make_test_coara(tmp_path, provider=provider)
    coara.identity.is_owner_context = True
    from src.tools.builtin.planning.plan_mode import PlanModeTool

    coara.register_tool(PlanModeTool(parent_coara=coara))
    coara.register_tool(_EchoTool())
    coara.enter_plan_mode(plan_file)
    await coara.initialize()

    _ = [chunk async for chunk in coara.process_message("提交计划", source="cli")]
    assert coara._plan_pending_approval is True

    # 两个 tool_call 都必须有匹配结果：submit=成功，echo=「未执行」闭合
    by_id: dict[str, str] = {}
    for m in coara.message_history:
        if m.role == MessageRole.TOOL_RESULT and m.tool_call_id:
            by_id[m.tool_call_id] = str(m.content or "")
    assert "call-submit" in by_id and "计划已提交" in by_id["call-submit"]
    assert "call-echo" in by_id and "未执行" in by_id["call-echo"]


@pytest.mark.asyncio
async def test_plan_submit_emits_completed_turn_end_signal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """plan submit 收尾与正常回合一致：发 "completed"（attach 回合结束信号的
    topic 白名单 / workspace registry 都以它为终点），不再发端不认识的
    "turn_completed"——否则 attach spinner/状态行收不了尾。"""
    plan_file = tmp_path / "plan.md"
    provider = FakeProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call-submit",
                        name="plan_mode",
                        arguments={"action": "submit", "content": "## 方案"},
                    )
                ],
            ),
        ]
    )
    coara = make_test_coara(tmp_path, provider=provider)
    coara.identity.is_owner_context = True
    from src.tools.builtin.planning.plan_mode import PlanModeTool

    coara.register_tool(PlanModeTool(parent_coara=coara))
    coara.enter_plan_mode(plan_file)
    await coara.initialize()

    seen: list[tuple[str, str]] = []
    original_emit = coara._emit_trace

    def _capturing(event_type: str, message: str, **kwargs):
        seen.append((event_type, message))
        return original_emit(event_type, message, **kwargs)

    monkeypatch.setattr(coara, "_emit_trace", _capturing)

    _ = [chunk async for chunk in coara.process_message("提交计划", source="cli")]

    topics = [topic for topic, _ in seen]
    assert "completed" in topics
    assert "turn_completed" not in topics
