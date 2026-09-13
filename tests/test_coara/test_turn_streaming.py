"""Tests for CLI assistant streaming during LLM completion."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from src.coara.turn_completion import (
    _collect_stream_interruptible,
    _complete_with_provider,
    _complete_with_provider_streaming,
    _ensure_non_empty_response,
)
from src.core.errors import LLMError
from src.core.types import Message, MessageRole, ToolCall
from src.llm.provider import LLMProvider, LLMResponse, StreamChunk, ToolCallDelta
from src.llm.stream import StreamAggregator


class DeltaStreamProvider(LLMProvider):
    def __init__(self, chunks: list[StreamChunk]):
        super().__init__(name="delta", api_key="test", default_model="delta-model")
        self._chunks = chunks
        self.base_url = "https://api.openai.com/v1"

    async def complete(self, *args, **kwargs) -> LLMResponse:
        raise AssertionError("complete should not be called")

    async def stream_complete(self, *args, **kwargs) -> AsyncIterator[StreamChunk]:
        for chunk in self._chunks:
            yield chunk

    def get_context_window(self, model: str | None = None) -> int:
        return 32_000

    async def close(self) -> None:
        pass

    def abort(self) -> None:
        pass


@pytest.mark.asyncio
async def test_streaming_completion_emits_visible_deltas_only() -> None:
    deltas: list[str] = []

    response = await _complete_with_provider_streaming(
        DeltaStreamProvider(
            [
                StreamChunk(delta_reasoning="hidden"),
                StreamChunk(delta_content="Hel"),
                StreamChunk(delta_content="lo"),
                StreamChunk(finish_reason="stop"),
            ]
        ),
        "delta-model",
        None,
        "system",
        [Message(role=MessageRole.USER, content="hi")],
        on_assistant_delta=deltas.append,
    )

    assert response.content == "Hello"
    assert deltas == ["Hel", "lo"]


def test_stream_aggregator_builds_minimax_wire_blocks_with_tools() -> None:
    aggregator = StreamAggregator()
    aggregator.add_chunk(StreamChunk(delta_reasoning="think"))
    aggregator.add_chunk(StreamChunk(delta_content="answer"))
    aggregator.add_chunk(
        StreamChunk(
            delta_tool_calls=[
                ToolCallDelta(
                    index=0,
                    id="tc-1",
                    name="read",
                    arguments_fragment='{"path":"a.py"}',
                )
            ]
        )
    )
    aggregator.add_chunk(StreamChunk(finish_reason="tool_use"))

    response = aggregator.build_response(wire_blocks=True)
    assert response.provider_content_blocks is not None
    assert response.provider_content_blocks[0] == {"type": "thinking", "thinking": "think"}
    assert response.tool_calls[0].name == "read"


def test_unsent_assistant_text_skips_streamed_prefix(tmp_path) -> None:
    from tests.helpers import make_test_coara

    coara = make_test_coara(tmp_path)
    coara._streamed_assistant_chars = 5
    assert coara._unsent_assistant_text("hello world") == " world"
    assert coara._unsent_assistant_text("hello") == ""


# ---------------------------------------------------------------------------
# EMPTY_RESPONSE：正常收尾但零内容按错误处理（参考 deepseek-harness 语义）
# ---------------------------------------------------------------------------


def test_ensure_non_empty_accepts_normal_content() -> None:
    _ensure_non_empty_response(
        LLMResponse(content="ok", finish_reason="stop"),
        operation="complete",
    )


def test_ensure_non_empty_accepts_tool_calls_only() -> None:
    _ensure_non_empty_response(
        LLMResponse(content="", tool_calls=[ToolCall(id="tc-1", name="read", arguments={})], finish_reason="tool_use"),
        operation="complete",
    )


def test_ensure_non_empty_accepts_reasoning_only() -> None:
    """思考模式：只有 reasoning 无正文不算空响应。"""
    _ensure_non_empty_response(
        LLMResponse(content="", finish_reason="stop", reasoning_content="thinking…"),
        operation="complete",
    )


def test_ensure_non_empty_accepts_truncated_empty() -> None:
    """length 截断保持 partial 语义，不按空响应报错。"""
    _ensure_non_empty_response(
        LLMResponse(content="", finish_reason="length"),
        operation="complete",
    )


def test_ensure_non_empty_rejects_stop_with_zero_content() -> None:
    with pytest.raises(LLMError, match="空响应"):
        _ensure_non_empty_response(
            LLMResponse(content="", finish_reason="stop"),
            operation="complete",
        )


@pytest.mark.asyncio
async def test_stream_empty_stop_raises_llm_error() -> None:
    """流式聚合：finish=stop 且零内容 → LLMError（零 delta 允许上层重发）。"""

    async def gen():
        yield StreamChunk(finish_reason="stop")

    with pytest.raises(LLMError, match="空响应"):
        await _collect_stream_interruptible(gen(), None, None)


class FailingStreamProvider(DeltaStreamProvider):
    """Stream yields the canned chunks, then raises LLMError mid-stream."""

    def __init__(self, chunks: list[StreamChunk], *, fallback_response: LLMResponse | None = None):
        super().__init__(chunks)
        self.complete_calls = 0
        self._fallback_response = fallback_response

    async def complete(self, *args, **kwargs) -> LLMResponse:
        self.complete_calls += 1
        if self._fallback_response is not None:
            return self._fallback_response
        raise AssertionError("complete should not be called")

    async def stream_complete(self, *args, **kwargs) -> AsyncIterator[StreamChunk]:
        for chunk in self._chunks:
            yield chunk
        raise LLMError("连接中断")


@pytest.mark.asyncio
async def test_streaming_failure_without_delta_falls_back_to_non_streaming() -> None:
    """零 delta 失败（请求未真正开始）允许回退非流式整 prompt 重发。"""
    provider = FailingStreamProvider([], fallback_response=LLMResponse(content="重发成功"))

    response = await _complete_with_provider_streaming(
        provider,
        "delta-model",
        None,
        "system",
        [Message(role=MessageRole.USER, content="hi")],
    )

    assert response.content == "重发成功"
    assert provider.complete_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("with_tools", [False, True])
async def test_streaming_failure_after_delta_raises_without_resend(with_tools: bool) -> None:
    """已出部分内容后失败：直接报错不重发（避免重复计费），部分 usage 随异常带出。"""
    provider = FailingStreamProvider(
        [
            StreamChunk(delta_content="部分"),
            StreamChunk(usage={"input_tokens": 10, "output_tokens": 3}),
        ]
    )
    tools = (
        [{"name": "echo", "description": "Echo", "parameters": {"type": "object", "properties": {}}}]
        if with_tools
        else None
    )

    with pytest.raises(LLMError) as exc_info:
        await _complete_with_provider_streaming(
            provider,
            "delta-model",
            tools,
            "system",
            [Message(role=MessageRole.USER, content="hi")],
        )

    assert exc_info.value.stream_received_delta is True
    assert exc_info.value.partial_usage == {"input_tokens": 10, "output_tokens": 3}
    assert provider.complete_calls == 0


class FailAfterDeltaCompleteProvider(LLMProvider):
    """complete() raises LLMError carrying stream breakpoint info (compat aggregate mid-stream failure)."""

    def __init__(self, exc: LLMError):
        super().__init__(name="fail", api_key="test", default_model="fail-model")
        self._exc = exc
        self.base_url = "https://api.openai.com/v1"
        self.calls = 0

    async def complete(self, *args, **kwargs) -> LLMResponse:
        self.calls += 1
        raise self._exc

    async def stream_complete(self, *args, **kwargs) -> AsyncIterator[StreamChunk]:
        raise AssertionError("stream_complete should not be called")
        yield

    def get_context_window(self, model: str | None = None) -> int:
        return 32_000

    async def close(self) -> None:
        pass

    def abort(self) -> None:
        pass


_TOOLS = [{"name": "read", "description": "read file", "parameters": {"type": "object", "properties": {}}}]


@pytest.mark.asyncio
async def test_react_fallback_declined_when_stream_already_produced_delta() -> None:
    exc = LLMError("mid-stream failure")
    exc.stream_received_delta = True
    provider = FailAfterDeltaCompleteProvider(exc)

    with pytest.raises(LLMError):
        await _complete_with_provider(
            provider,
            "fail-model",
            _TOOLS,
            "system",
            [Message(role=MessageRole.USER, content="hi")],
        )
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_zero_progress_failure_marks_stream_received_delta_false() -> None:
    exc = LLMError("connection refused")
    exc.stream_received_delta = False
    provider = FailAfterDeltaCompleteProvider(exc)

    # 零进展失败不再自动回退（ReAct 文本协议已删除）：异常带出「未收到 delta」
    # 标记，由上层决定是否整 prompt 重发。
    with pytest.raises(LLMError) as excinfo:
        await _complete_with_provider(
            provider,
            "fail-model",
            _TOOLS,
            "system",
            [Message(role=MessageRole.USER, content="hi")],
        )
    assert excinfo.value.stream_received_delta is False
    assert provider.calls == 1
