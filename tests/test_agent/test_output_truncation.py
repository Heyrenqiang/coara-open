"""Tests for LLM output truncation detection and recovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.agent.output_truncation import (
    OutputTruncationPolicy,
    OutputTruncationSettings,
    TruncationRecoveryState,
    agent_can_materialize_to_disk,
    format_truncation_recovery_notice,
    is_output_truncated,
    try_output_truncation_recovery,
)
from src.coara.base import CoaraBase
from src.core.types import CoaraPersona, Message, MessageRole
from src.llm.provider import LLMResponse
from src.tools.builtin.file_io.write import WriteTool
from tests.helpers import FakeProvider


def test_is_output_truncated_detection() -> None:
    from src.core.types import ToolCall

    assert is_output_truncated(
        LLMResponse(content="partial", finish_reason="max_tokens", usage={"output_tokens": 100}),
        requested_max_tokens=16384,
    )
    assert is_output_truncated(
        LLMResponse(content="partial", finish_reason="stop", usage={"output_tokens": 16300}),
        requested_max_tokens=16384,
        output_token_ratio=0.98,
    )
    assert is_output_truncated(
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="1", name="read", arguments={})],
            finish_reason="max_tokens",
            usage={"output_tokens": 16384},
        ),
        requested_max_tokens=16384,
    )
    assert not is_output_truncated(
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="1", name="read", arguments={})],
            finish_reason="stop",
            usage={"output_tokens": 16384},
        ),
        requested_max_tokens=16384,
    )


def test_partial_finish_reason_truncation_recovery_reaction() -> None:
    """#46 新形态确认：静默截断标 partial 后，截断恢复门的当前反应

    - partial 不在 TRUNCATED_FINISH_REASONS：标签本身不触发恢复（既往
      max_tokens/length/model_length 触发路径不受影响）
    - 文本路径的 token 比例启发式对 partial 仍然生效：逼近 max_tokens 的
      静默截断仍会触发恢复
    """
    from src.core.types import ToolCall

    # 标签本身不触发（低 output 占比）
    assert not is_output_truncated(
        LLMResponse(content="半截", finish_reason="partial", usage={"output_tokens": 100}),
        requested_max_tokens=16384,
    )
    # token 比例启发式对 partial 仍然生效
    assert is_output_truncated(
        LLMResponse(content="半截", finish_reason="partial", usage={"output_tokens": 16300}),
        requested_max_tokens=16384,
        output_token_ratio=0.98,
    )
    # tool_calls 路径只信显式截断信号，partial 不误伤正常工具派发
    assert not is_output_truncated(
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="1", name="read", arguments={})],
            finish_reason="partial",
            usage={"output_tokens": 16384},
        ),
        requested_max_tokens=16384,
    )


@pytest.mark.asyncio
async def test_force_tool_recovery_injects_reminder(tmp_path: Path) -> None:
    provider = FakeProvider(
        [LLMResponse(content="x" * 5000, finish_reason="max_tokens", usage={"output_tokens": 16384})]
    )
    coara = CoaraBase(
        name="test",
        persona=CoaraPersona(name="test", role="test"),
        provider=provider,
        workspace_dir=tmp_path,
    )
    coara.register_tool(WriteTool(coara))
    await coara.bootstrap_tools()
    await coara.initialize()
    coara.message_history.append(Message(role=MessageRole.USER, content="write report"))

    settings = OutputTruncationSettings(default_policy=OutputTruncationPolicy.FORCE_TOOL)
    state = TruncationRecoveryState()
    response = LLMResponse(content="x" * 5000, finish_reason="max_tokens", usage={"output_tokens": 16384})

    result = try_output_truncation_recovery(
        coara=coara,
        response=response,
        settings=settings,
        state=state,
        requested_max_tokens=16384,
        iteration=2,
    )

    assert result.handled is True
    assert result.action == "continue"
    assert result.policy == OutputTruncationPolicy.FORCE_TOOL
    assert len(coara.message_history) == 2
    assert coara.message_history[-1].role == MessageRole.USER
    assert "禁止在对话回复中继续输出大段正文" in coara.message_history[-1].content
    assert agent_can_materialize_to_disk(coara)


@pytest.mark.asyncio
async def test_auto_continue_when_no_write_tool(tmp_path: Path) -> None:
    provider = FakeProvider(
        [LLMResponse(content="x" * 5000, finish_reason="max_tokens", usage={"output_tokens": 8000})]
    )
    coara = CoaraBase(
        name="explore-like",
        persona=CoaraPersona(name="explore", role="explore"),
        provider=provider,
        workspace_dir=tmp_path,
    )
    coara._tool_manager.set_whitelist({"read", "grep"})
    await coara.bootstrap_tools()
    await coara.initialize()

    settings = OutputTruncationSettings(default_policy=OutputTruncationPolicy.FORCE_TOOL)
    response = LLMResponse(content="x" * 5000, finish_reason="max_tokens", usage={"output_tokens": 8000})
    result = try_output_truncation_recovery(
        coara=coara,
        response=response,
        settings=settings,
        state=TruncationRecoveryState(),
        requested_max_tokens=8192,
        iteration=1,
    )

    assert result.action == "continue"
    assert result.policy == OutputTruncationPolicy.AUTO_CONTINUE
    assert "续写" in coara.message_history[-1].content


def test_format_truncation_recovery_notice() -> None:
    force = format_truncation_recovery_notice(
        policy=OutputTruncationPolicy.FORCE_TOOL,
        finish_reason="max_tokens",
        requested_max_tokens=16384,
        output_tokens=16384,
        draft_path="D:/ws/_draft/report.md",
    )
    assert "截断" in force and "落盘" in force and "report.md" in force

    cont = format_truncation_recovery_notice(
        policy=OutputTruncationPolicy.AUTO_CONTINUE,
        finish_reason="length",
        requested_max_tokens=8192,
    )
    assert "续写" in cont


def test_warn_only_policy() -> None:
    provider = FakeProvider([])
    coara = CoaraBase(
        name="test",
        persona=CoaraPersona(name="test", role="test"),
        provider=provider,
        workspace_dir=Path("."),
    )
    settings = OutputTruncationSettings(default_policy=OutputTruncationPolicy.WARN_ONLY)
    response = LLMResponse(content="partial", finish_reason="length", usage={"output_tokens": 4096})
    result = try_output_truncation_recovery(
        coara=coara,
        response=response,
        settings=settings,
        state=TruncationRecoveryState(),
        requested_max_tokens=4096,
        iteration=1,
    )
    assert result.action == "warn_finish"
    assert "截断" in result.warning


@pytest.mark.asyncio
async def test_tool_call_truncation_recovery_injects_reminder_and_skips_dispatch(tmp_path: Path) -> None:
    from src.coara.workspace_switch_history import close_unmatched_tool_calls
    from src.core.types import ToolCall

    provider = FakeProvider([])
    coara = CoaraBase(
        name="test",
        persona=CoaraPersona(name="test", role="test"),
        provider=provider,
        workspace_dir=tmp_path,
    )
    coara.register_tool(WriteTool(coara))
    await coara.bootstrap_tools()
    await coara.initialize()
    # Match orchestrator order: assistant with tool_calls is already in history.
    coara.message_history.append(Message(role=MessageRole.USER, content="write big file"))
    tool_calls = [ToolCall(id="todo:27", name="todo", arguments={"_raw": '{"action":'})]
    coara.message_history.append(Message(role=MessageRole.ASSISTANT, content="", tool_calls=tool_calls))

    settings = OutputTruncationSettings(default_policy=OutputTruncationPolicy.FORCE_TOOL)
    state = TruncationRecoveryState()
    response = LLMResponse(
        content="",
        tool_calls=tool_calls,
        finish_reason="max_tokens",
        usage={"output_tokens": 16384},
    )

    result = try_output_truncation_recovery(
        coara=coara,
        response=response,
        settings=settings,
        state=state,
        requested_max_tokens=16384,
        iteration=1,
    )

    assert result.handled is True
    assert result.action == "continue"
    assert result.policy == OutputTruncationPolicy.WARN_ONLY
    tool_results = [m for m in coara.message_history if m.role == MessageRole.TOOL_RESULT]
    assert len(tool_results) == 1
    assert tool_results[0].tool_call_id == "todo:27"
    assert "截断" in str(tool_results[0].content)
    assert coara.message_history[-1].role == MessageRole.USER
    content = coara.message_history[-1].content
    assert "参数 JSON 不完整" in content
    assert "共享" in content
    assert "16384" not in content
    # History is provider-valid: no remaining unmatched tool_calls.
    assert close_unmatched_tool_calls(list(coara.message_history)) == (0, 0)


@pytest.mark.asyncio
async def test_tool_call_truncation_recovery_respects_max_continuations(tmp_path: Path) -> None:
    from src.core.types import ToolCall

    provider = FakeProvider([])
    coara = CoaraBase(
        name="test",
        persona=CoaraPersona(name="test", role="test"),
        provider=provider,
        workspace_dir=tmp_path,
    )
    coara.register_tool(WriteTool(coara))
    await coara.bootstrap_tools()
    await coara.initialize()
    tool_calls = [ToolCall(id="1", name="write", arguments={"path": "/x", "contents": "y"})]
    coara.message_history.append(Message(role=MessageRole.USER, content="write big file"))
    coara.message_history.append(Message(role=MessageRole.ASSISTANT, content="", tool_calls=tool_calls))

    settings = OutputTruncationSettings(default_policy=OutputTruncationPolicy.FORCE_TOOL, max_continuations=1)
    state = TruncationRecoveryState()
    response = LLMResponse(
        content="",
        tool_calls=tool_calls,
        finish_reason="length",
        usage={"output_tokens": 16384},
    )

    # First call: within limit
    result1 = try_output_truncation_recovery(
        coara=coara,
        response=response,
        settings=settings,
        state=state,
        requested_max_tokens=16384,
        iteration=1,
    )
    assert result1.handled is True
    assert result1.action == "continue"

    # Second call: exceeds max_continuations but still "continue" (must not fall back to
    # warn_finish, which would dispatch the truncated tool_call and re-trigger the bug)
    result2 = try_output_truncation_recovery(
        coara=coara,
        response=response,
        settings=settings,
        state=state,
        requested_max_tokens=16384,
        iteration=2,
    )
    assert result2.handled is True
    assert result2.action == "continue"
    assert result2.warning  # non-empty warning when limit exceeded
