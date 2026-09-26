"""Tests for StreamAggregator tool-call accumulation."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from src.core.errors import LLMError
from src.llm.provider import StreamChunk, ToolCallDelta
from src.llm.stream import StreamAggregator, collect_stream


def test_tool_call_first_delta_without_id_then_id_arrives() -> None:
    # Regression: first delta carries no id, a later delta does. Keying by id
    # left a stale empty "_idx_0" entry → build_response emitted a broken
    # ToolCall with an empty name alongside the real one.
    aggregator = StreamAggregator()
    aggregator.add_chunk(StreamChunk(delta_tool_calls=[ToolCallDelta(index=0, arguments_fragment='{"pa')]))
    aggregator.add_chunk(
        StreamChunk(delta_tool_calls=[ToolCallDelta(index=0, id="call_1", name="read", arguments_fragment='th": "x"}')])
    )
    aggregator.add_chunk(StreamChunk(finish_reason="tool_calls"))

    response = aggregator.build_response()
    assert len(response.tool_calls) == 1
    tool_call = response.tool_calls[0]
    assert tool_call.id == "call_1"
    assert tool_call.name == "read"
    assert tool_call.arguments == {"path": "x"}


def test_tool_call_order_preserved_by_index() -> None:
    aggregator = StreamAggregator()
    aggregator.add_chunk(
        StreamChunk(
            delta_tool_calls=[
                ToolCallDelta(index=0, id="a", name="first", arguments_fragment="{}"),
                ToolCallDelta(index=1, id="b", name="second", arguments_fragment="{}"),
            ]
        )
    )

    response = aggregator.build_response()
    assert [tc.name for tc in response.tool_calls] == ["first", "second"]
    assert [tc.id for tc in response.tool_calls] == ["a", "b"]


def test_tool_call_without_any_id_gets_fallback_id() -> None:
    aggregator = StreamAggregator()
    aggregator.add_chunk(StreamChunk(delta_tool_calls=[ToolCallDelta(index=2, name="read", arguments_fragment="{}")]))

    response = aggregator.build_response()
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].id == "tc_2"
    assert response.tool_calls[0].name == "read"


async def _stream_of(chunks: list[StreamChunk], error: Exception | None = None) -> AsyncIterator[StreamChunk]:
    for chunk in chunks:
        yield chunk
    if error is not None:
        raise error


# ---------------------------------------------------------------------------
# #40 中途失败断点信息随异常带出
# ---------------------------------------------------------------------------


async def test_collect_stream_mid_failure_attaches_breakpoint_info() -> None:
    """中途失败：异常携带已产 chunk 数 / delta 标记 / partial usage（供条件重试与入账）"""
    chunks = [
        StreamChunk(delta_content="部分"),
        StreamChunk(usage={"input_tokens": 10, "output_tokens": 3}),
    ]
    with pytest.raises(LLMError) as exc_info:
        await collect_stream(_stream_of(chunks, LLMError("连接中断")))

    assert exc_info.value.stream_received_delta is True
    assert exc_info.value.stream_chunks_seen == 2
    assert exc_info.value.partial_usage == {"input_tokens": 10, "output_tokens": 3}


async def test_collect_stream_zero_chunk_failure_marks_stream_not_started() -> None:
    """零 chunk 失败：标记流未开始（允许整包重试），无 partial usage 可记"""
    with pytest.raises(LLMError) as exc_info:
        await collect_stream(_stream_of([], LLMError("连接中断")))

    assert exc_info.value.stream_received_delta is False
    assert exc_info.value.stream_chunks_seen == 0
    assert not hasattr(exc_info.value, "partial_usage")


# ---------------------------------------------------------------------------
# #46 静默截断（无 finish chunk）标 partial
# ---------------------------------------------------------------------------


def test_build_response_without_finish_chunk_marks_partial() -> None:
    """provider 干净断流（无异常无 finish chunk）不再谎报 stop"""
    aggregator = StreamAggregator()
    aggregator.add_chunk(StreamChunk(delta_content="半截内容"))

    response = aggregator.build_response()
    assert response.content == "半截内容"
    assert response.finish_reason == "partial"


async def test_collect_stream_clean_end_without_finish_chunk_marks_partial() -> None:
    """collect_stream 干净结束但无 finish chunk 同样标 partial"""
    response = await collect_stream(_stream_of([StreamChunk(delta_content="半截")]))
    assert response.finish_reason == "partial"


def test_silent_truncation_minimax_early_input_usage_retained() -> None:
    """MiniMax 模式（message_start 早给 input）：静默截断仍保留已报 input/output usage"""
    aggregator = StreamAggregator()
    aggregator.add_chunk(StreamChunk(usage={"input_tokens": 500}))
    aggregator.add_chunk(StreamChunk(delta_content="部分"))
    aggregator.add_chunk(StreamChunk(usage={"input_tokens": 500, "output_tokens": 7}))

    response = aggregator.build_response()
    assert response.finish_reason == "partial"
    assert response.usage == {"input_tokens": 500, "output_tokens": 7}


def test_silent_truncation_mimo_final_chunk_usage_missing() -> None:
    """MiMo 模式（usage 仅在最终 chunk）：静默截断时 usage 随最终 chunk 一起丢失"""
    aggregator = StreamAggregator()
    aggregator.add_chunk(StreamChunk(delta_content="部分"))

    response = aggregator.build_response()
    assert response.finish_reason == "partial"
    assert response.usage == {}


def test_normal_finish_minimax_mode_unchanged() -> None:
    """正常路径不变：MiniMax 早 input + finish chunk → 真实 finish_reason 与完整 usage"""
    aggregator = StreamAggregator()
    aggregator.add_chunk(StreamChunk(usage={"input_tokens": 500}))
    aggregator.add_chunk(StreamChunk(delta_content="完整"))
    aggregator.add_chunk(StreamChunk(usage={"input_tokens": 500, "output_tokens": 9}))
    aggregator.add_chunk(StreamChunk(finish_reason="end_turn"))

    response = aggregator.build_response()
    assert response.finish_reason == "end_turn"
    assert response.usage == {"input_tokens": 500, "output_tokens": 9}


def test_normal_finish_mimo_mode_unchanged() -> None:
    """正常路径不变：MiMo 仅最终 chunk 带 usage + finish → 真实 finish_reason 与 usage"""
    aggregator = StreamAggregator()
    aggregator.add_chunk(StreamChunk(delta_content="完整"))
    aggregator.add_chunk(StreamChunk(finish_reason="stop", usage={"input_tokens": 100, "output_tokens": 9}))

    response = aggregator.build_response()
    assert response.finish_reason == "stop"
    assert response.usage == {"input_tokens": 100, "output_tokens": 9}
