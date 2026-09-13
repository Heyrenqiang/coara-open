"""#40：``_stream_aggregate`` 整包重试只在零 chunk 时允许（防重复计费）"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest

from src.core.errors import LLMError
from src.core.types import Message, MessageRole
from src.llm.anthropic import AnthropicProvider
from src.llm.provider import LLMResponse, StreamChunk


def _compat_provider() -> AnthropicProvider:
    # compat 端点 + max_tokens > 8192 → use_compat_stream 聚合路径
    return AnthropicProvider(
        name="minimax",
        api_key="test",
        base_url="https://api.minimaxi.com/anthropic",
        default_model="MiniMax-M3",
    )


def _sdk_stream_provider() -> AnthropicProvider:
    return AnthropicProvider(name="anthropic", api_key="test", default_model="test-model")


def _messages() -> list[Message]:
    return [Message(role=MessageRole.USER, content="hi")]


def _retryable_mid_stream_error() -> LLMError:
    exc = LLMError("Unexpected error: boom")
    exc.__cause__ = ConnectionError("boom")
    return exc


async def test_compat_stream_aggregate_mid_stream_failure_not_retried(monkeypatch) -> None:
    """已出 chunk 后中途失败：不整包重试（避免重复计费），partial usage 随异常带出"""
    monkeypatch.setattr("src.llm.retry._compute_delay", lambda attempt: 0)
    provider = _compat_provider()
    calls = 0

    async def fake_stream(**kwargs) -> AsyncIterator[StreamChunk]:
        nonlocal calls
        calls += 1
        yield StreamChunk(delta_content="部分")
        yield StreamChunk(usage={"input_tokens": 500, "output_tokens": 3})
        raise _retryable_mid_stream_error()

    provider.stream_complete = fake_stream  # type: ignore[assignment]
    try:
        with pytest.raises(LLMError) as exc_info:
            await provider.complete(_messages(), max_tokens=16384)
    finally:
        await provider.close()

    assert calls == 1
    assert exc_info.value.stream_chunks_seen == 2
    assert exc_info.value.partial_usage == {"input_tokens": 500, "output_tokens": 3}


async def test_zero_chunk_fallback_blocked_when_streaming_required(monkeypatch) -> None:
    """互递归护栏：streaming-required 模型零 chunk 失败时不回退 _call（防无限互递归洪峰）。"""
    monkeypatch.setattr("src.llm.retry._compute_delay", lambda attempt: 0)
    monkeypatch.setattr("src.llm.anthropic._requires_streaming_for_create", lambda model, max_tokens: True)
    provider = _sdk_stream_provider()
    calls = 0

    def _zero_chunk_index_error() -> LLMError:
        exc = LLMError("Unexpected error: list index out of range")
        exc.__cause__ = IndexError("list index out of range")
        exc.stream_chunks_seen = 0
        return exc

    async def fake_stream(**kwargs) -> AsyncIterator[StreamChunk]:
        nonlocal calls
        calls += 1
        raise _zero_chunk_index_error()
        yield  # pragma: no cover

    provider.stream_complete = fake_stream  # type: ignore[assignment]
    try:
        with pytest.raises(LLMError):
            await provider.complete(_messages(), max_tokens=4096)
    finally:
        await provider.close()

    # 护栏生效：不回退 _call 重打 HTTP，仅 retry_complete 的重试次数内
    assert calls <= 4


async def test_compat_stream_aggregate_zero_chunk_failure_retried(monkeypatch) -> None:
    """零 chunk 失败（请求未真正开始）：允许整包重试直至成功"""
    monkeypatch.setattr("src.llm.retry._compute_delay", lambda attempt: 0)
    provider = _compat_provider()
    calls = 0

    async def fake_stream(**kwargs) -> AsyncIterator[StreamChunk]:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise _retryable_mid_stream_error()
        yield StreamChunk(delta_content="完整")
        yield StreamChunk(finish_reason="end_turn", usage={"input_tokens": 500, "output_tokens": 9})

    provider.stream_complete = fake_stream  # type: ignore[assignment]
    try:
        response = await provider.complete(_messages(), max_tokens=16384)
    finally:
        await provider.close()

    assert calls == 3
    assert response.content == "完整"
    assert response.finish_reason == "end_turn"


async def test_sdk_parsed_stream_aggregate_mid_stream_failure_not_retried(monkeypatch) -> None:
    """SDK parsed stream 包裹点同样适用：已出 chunk 不整包重试"""
    monkeypatch.setattr("src.llm.retry._compute_delay", lambda attempt: 0)
    monkeypatch.setattr("src.llm.anthropic._requires_streaming_for_create", lambda model, max_tokens: True)
    provider = _sdk_stream_provider()
    calls = 0

    async def fake_stream(**kwargs) -> AsyncIterator[StreamChunk]:
        nonlocal calls
        calls += 1
        yield StreamChunk(delta_content="部分")
        raise _retryable_mid_stream_error()

    provider.stream_complete = fake_stream  # type: ignore[assignment]
    try:
        with pytest.raises(LLMError) as exc_info:
            await provider.complete(_messages(), max_tokens=16384)
    finally:
        await provider.close()

    assert calls == 1
    assert exc_info.value.stream_chunks_seen == 1


def _index_error_stream_failure() -> LLMError:
    exc = LLMError("Unexpected error: list index out of range")
    exc.__cause__ = IndexError("list index out of range")
    return exc


async def test_parse_fallback_declined_after_stream_progress(monkeypatch) -> None:
    """IndexError 解析回退同样守零 chunk 闸门：已出 chunk 不回退整 prompt 重发"""
    monkeypatch.setattr("src.llm.retry._compute_delay", lambda attempt: 0)
    provider = _compat_provider()
    calls = 0

    async def fake_stream(**kwargs) -> AsyncIterator[StreamChunk]:
        nonlocal calls
        calls += 1
        yield StreamChunk(delta_content="部分")
        raise _index_error_stream_failure()

    provider.stream_complete = fake_stream  # type: ignore[assignment]
    try:
        with pytest.raises(LLMError) as exc_info:
            await provider.complete(_messages(), max_tokens=16384)
    finally:
        await provider.close()

    # IndexError 不可重试，也不允许回退重发：仅一次流尝试
    assert calls == 1
    assert exc_info.value.stream_chunks_seen == 1


async def test_parse_fallback_allowed_on_zero_chunk(monkeypatch) -> None:
    """零 chunk 的 IndexError 解析失败：回退非流式 create 的既有行为不变"""
    monkeypatch.setattr("src.llm.retry._compute_delay", lambda attempt: 0)
    provider = _compat_provider()

    async def fake_stream(**kwargs) -> AsyncIterator[StreamChunk]:
        raise _index_error_stream_failure()
        yield  # pragma: no cover - 使函数成为异步生成器

    class _FakeMessages:
        @staticmethod
        async def create(**kwargs):
            return object()

    provider.stream_complete = fake_stream  # type: ignore[assignment]
    provider.client = SimpleNamespace(messages=_FakeMessages())
    monkeypatch.setattr(
        AnthropicProvider,
        "_parse_response",
        lambda self, response: LLMResponse(content="回退成功", finish_reason="end_turn"),
    )
    try:
        response = await provider.complete(_messages(), max_tokens=16384)
    finally:
        await provider.close()

    assert response.content == "回退成功"
    assert response.finish_reason == "end_turn"
