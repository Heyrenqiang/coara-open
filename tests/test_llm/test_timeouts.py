"""Tests for LLM request timeouts (src/llm/timeouts.py + retry total budget)."""

from __future__ import annotations

import asyncio

import pytest

from src.core.errors import LLMError
from src.llm import timeouts
from src.llm.retry import with_retry


def test_defaults(monkeypatch) -> None:
    monkeypatch.setattr(timeouts, "_configured", lambda key: None)
    assert timeouts.llm_request_timeout_seconds() == 300.0
    assert timeouts.llm_total_timeout_seconds() == 300.0


def test_config_override_and_floor(monkeypatch) -> None:
    values = {"request_timeout_seconds": 120, "total_timeout_seconds": 900}
    monkeypatch.setattr(timeouts, "_configured", values.get)
    assert timeouts.llm_request_timeout_seconds() == 120.0
    assert timeouts.llm_total_timeout_seconds() == 900.0

    monkeypatch.setattr(timeouts, "_configured", lambda key: 1)
    assert timeouts.llm_request_timeout_seconds() == 30.0
    assert timeouts.llm_total_timeout_seconds() == 60.0


async def test_retry_gives_up_after_total_budget(monkeypatch) -> None:
    monkeypatch.setattr(timeouts, "_configured", lambda key: None)
    calls = 0

    async def always_timeout() -> None:
        nonlocal calls
        calls += 1
        raise TimeoutError("read timeout")

    # 预算 0.2s：第一次失败耗时 0.15s，第二次失败后累计超预算 → 不再重试
    import src.llm.retry as retry_mod

    monkeypatch.setattr(retry_mod, "_compute_delay", lambda attempt: 0.01)

    real_sleep = asyncio.sleep
    sleeps = 0

    async def fake_sleep(seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        await real_sleep(0.01)

    monkeypatch.setattr(retry_mod.asyncio, "sleep", fake_sleep)

    async def slow_timeout() -> None:
        nonlocal calls
        calls += 1
        await real_sleep(0.15)
        raise TimeoutError("read timeout")

    monkeypatch.setattr(timeouts, "_configured", lambda key: 0.2 if key == "total_timeout_seconds" else None)
    # with_retry 内部直接 import 的是 timeouts 模块函数，patch 其引用点
    monkeypatch.setattr("src.llm.timeouts.llm_total_timeout_seconds", lambda: 0.2)

    with pytest.raises(LLMError, match="已中止本次调用"):
        await with_retry(slow_timeout, max_retries=5, operation_name="test call")
    assert calls == 2  # 预算耗尽即停，不打满 5 次重试


async def test_retry_kills_single_hanging_attempt_at_budget(monkeypatch) -> None:
    """单次 attempt 永不返回（滴漏/挂死）：wait_for 在预算处杀死，不再重试."""
    monkeypatch.setattr("src.llm.timeouts.llm_total_timeout_seconds", lambda: 0.2)
    calls = 0

    async def hangs_forever() -> None:
        nonlocal calls
        calls += 1
        await asyncio.sleep(3600)

    with pytest.raises(LLMError, match="已中止本次调用"):
        await with_retry(hangs_forever, max_retries=3, operation_name="test call")
    assert calls == 1


async def test_fn_own_read_timeout_inside_budget_still_retries(monkeypatch) -> None:
    """fn 自己在预算内抛读超时 → 走正常重试分类，不误判为预算耗尽."""
    monkeypatch.setattr("src.llm.timeouts.llm_total_timeout_seconds", lambda: 5.0)
    calls = 0

    async def fail_once() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("read timeout")
        return "ok"

    result = await with_retry(fail_once, max_retries=2, operation_name="test call")
    assert result == "ok"
    assert calls == 2


async def test_stream_collection_drip_feed_killed_at_deadline() -> None:
    """滴漏式流式分片：每片都重置 read 超时也逃不过墙钟 deadline."""
    import time

    from src.coara.turn_completion import _collect_stream_interruptible
    from src.llm.provider import StreamChunk

    class _FakeProvider:
        aborted = False

        def abort(self) -> None:
            self.aborted = True

    async def drip_stream():
        yield StreamChunk(delta_content="第一片")
        await asyncio.sleep(3600)  # 第二片永远不来（滴漏的极端形态）
        yield StreamChunk(delta_content="永远到不了")

    provider = _FakeProvider()
    deadline = time.monotonic() + 0.2
    with pytest.raises(LLMError, match="已中止本次调用"):
        await _collect_stream_interruptible(
            drip_stream(),
            None,
            None,
            deadline=deadline,
            provider=provider,
        )
    assert provider.aborted


async def test_stream_collection_without_deadline_keeps_legacy_behavior() -> None:
    """不传 deadline：维持原有行为（仅 abort 竞速），正常聚合完整流."""
    from src.coara.turn_completion import _collect_stream_interruptible
    from src.llm.provider import StreamChunk

    async def normal_stream():
        yield StreamChunk(delta_content="你")
        yield StreamChunk(delta_content="好", finish_reason="stop")

    response = await _collect_stream_interruptible(normal_stream(), None, None)
    assert response.content == "你好"
