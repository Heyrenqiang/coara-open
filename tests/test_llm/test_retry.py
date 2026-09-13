"""Unit tests for LLM call retry classification."""

from __future__ import annotations

import pytest

from src.core.errors import LLMError
from src.llm.retry import _is_retryable_exception, retry_complete, with_retry


class _FakeAPIStatusError(Exception):
    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def test_wrapped_api_status_error_is_classified() -> None:
    cause = _FakeAPIStatusError("rate limited", status_code=429)
    wrapped = LLMError(f"Anthropic API error: {cause}")
    wrapped.__cause__ = cause
    should_retry, reason = _is_retryable_exception(wrapped)
    assert should_retry is True
    assert "429" in reason


def test_wrapped_bad_request_not_retryable() -> None:
    cause = _FakeAPIStatusError("invalid_request", status_code=400)
    wrapped = LLMError(f"Anthropic API error: {cause}")
    wrapped.__cause__ = cause
    should_retry, reason = _is_retryable_exception(wrapped)
    assert should_retry is False
    assert "400" in reason


def test_http_402_quota_not_retryable() -> None:
    """DeepSeek 官方 402 = 余额不足：不可重试，理由明确标注。"""
    cause = _FakeAPIStatusError("Insufficient Balance", status_code=402)
    should_retry, reason = _is_retryable_exception(cause)
    assert should_retry is False
    assert "402" in reason


def test_http_429_with_quota_marker_not_retryable() -> None:
    """429 但错误体表明配额耗尽（insufficient_quota / exceeded quota）：不重试。"""
    cause = _FakeAPIStatusError("You exceeded your current quota, please check your plan", status_code=429)
    should_retry, reason = _is_retryable_exception(cause)
    assert should_retry is False
    assert "quota" in reason


def test_http_429_rate_limit_still_retryable() -> None:
    """正常 429 限流不受配额判定影响，仍可重试。"""
    cause = _FakeAPIStatusError("Rate limit reached for model", status_code=429)
    should_retry, reason = _is_retryable_exception(cause)
    assert should_retry is True
    assert "429" in reason


def test_llm_error_message_with_error_code() -> None:
    exc = LLMError("Error code: 503 - overloaded")
    should_retry, reason = _is_retryable_exception(exc)
    assert should_retry is True
    assert "503" in reason


def _retryable_stream_error(*, chunks_seen: int) -> LLMError:
    """模拟 collect_stream 带出的中途失败：可重试传输错误 + 断点信息"""
    exc = LLMError("Unexpected error: boom")
    exc.__cause__ = ConnectionError("boom")
    exc.stream_chunks_seen = chunks_seen
    return exc


def _zero_chunk_gate(exc: Exception) -> bool:
    return not getattr(exc, "stream_chunks_seen", 0)


async def test_with_retry_retry_if_declines_after_stream_progress(monkeypatch) -> None:
    """retry_if 拒绝（流已产 chunk）：可重试错误也不再重发，fn 只调一次"""
    monkeypatch.setattr("src.llm.retry._compute_delay", lambda attempt: 0)
    calls = 0

    async def fn() -> None:
        nonlocal calls
        calls += 1
        raise _retryable_stream_error(chunks_seen=3)

    with pytest.raises(LLMError):
        await with_retry(fn, max_retries=3, retry_if=_zero_chunk_gate)
    assert calls == 1


async def test_with_retry_retry_if_allows_zero_chunk_retry(monkeypatch) -> None:
    """retry_if 放行（零 chunk）：按既有逻辑重试直至成功"""
    monkeypatch.setattr("src.llm.retry._compute_delay", lambda attempt: 0)
    calls = 0

    async def fn() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise _retryable_stream_error(chunks_seen=0)
        return "ok"

    assert await with_retry(fn, max_retries=3, retry_if=_zero_chunk_gate) == "ok"
    assert calls == 3


async def test_retry_complete_without_retry_if_unchanged(monkeypatch) -> None:
    """不传 retry_if：行为与既有完全一致（可重试错误照常重试）"""
    monkeypatch.setattr("src.llm.retry._compute_delay", lambda attempt: 0)
    calls = 0

    async def fn() -> str:
        nonlocal calls
        calls += 1
        if calls < 2:
            raise _retryable_stream_error(chunks_seen=5)
        return "ok"

    assert await retry_complete(fn, provider_name="t", max_retries=3) == "ok"
    assert calls == 2


async def test_with_retry_intermediate_failures_log_at_info(monkeypatch) -> None:
    """可恢复重试进 INFO（文件），不打 WARNING，避免刷交互终端。"""
    monkeypatch.setattr("src.llm.retry._compute_delay", lambda attempt: 0)
    infos: list[str] = []
    warnings: list[str] = []
    monkeypatch.setattr(
        "src.llm.retry.logger.info",
        lambda msg, *args, **kwargs: infos.append(str(msg)),
    )
    monkeypatch.setattr(
        "src.llm.retry.logger.warning",
        lambda msg, *args, **kwargs: warnings.append(str(msg)),
    )
    calls = 0

    async def fn() -> str:
        nonlocal calls
        calls += 1
        if calls < 2:
            raise ConnectionError("boom")
        return "ok"

    assert await with_retry(fn, max_retries=3, operation_name="probe stream_init") == "ok"
    assert calls == 2
    assert any("retrying" in m and "probe stream_init" in m for m in infos)
    assert not any("retrying" in m for m in warnings)


async def test_recoverable_retry_hidden_from_warning_console(monkeypatch, capsys) -> None:
    """交互默认 console_level=WARNING：重试 INFO 不出现在 stderr。"""
    from src.core.logger import setup_logger

    setup_logger(
        log_level="INFO",
        console_level="WARNING",
        enable_console=True,
        enable_file_logging=False,
    )
    monkeypatch.setattr("src.llm.retry._compute_delay", lambda attempt: 0)
    calls = 0

    async def fn() -> str:
        nonlocal calls
        calls += 1
        if calls < 2:
            raise ConnectionError("boom")
        return "ok"

    assert await with_retry(fn, max_retries=3, operation_name="minimax stream_init") == "ok"
    err = capsys.readouterr().err
    assert "retrying" not in err
    assert "stream_init" not in err
    setup_logger(enable_console=False, enable_file_logging=False)
