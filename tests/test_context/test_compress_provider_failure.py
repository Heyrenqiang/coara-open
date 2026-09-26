"""压缩时 provider 抛错 → 只降级为截断，绝不把回合带走（回归 2026-09-25）。

事故：上下文压缩请求返回 403（全局默认 provider 周额度耗尽），异常文本里带
``{'error': {'message': ...}}`` 这类花括号；压缩的降级分支用 f-string 把它内联进
loguru 日志，loguru 再对该 message 做一次 ``.format()`` → ``KeyError: "'error'"``
从日志调用处抛出 → 整回合中断。

本文件钉住：provider 侧的任意异常都只导致「降级为截断」，不抛给调用方、
不改变 `compressed=True` 语义，且 `error` 字段留下原始文本供排障。
"""

from __future__ import annotations

import pytest

from src.context.window import ContextWindowManager
from src.core.types import Message, MessageRole
from src.llm.provider import LLMProvider, LLMResponse
from src.llm.registry import provider_registry
from src.llm.service import llm_service


class _RaisingProvider(LLMProvider):
    """complete 直接抛错的 provider（403 / 配额耗尽 / 网络异常的统一形态）。"""

    def __init__(self, exc: BaseException) -> None:
        super().__init__(name="raising", api_key="test", default_model="raising-model")
        self.exc = exc

    async def complete(self, *args, **kwargs) -> LLMResponse:
        raise self.exc

    async def stream_complete(self, *args, **kwargs):
        raise self.exc

    def get_context_window(self, model: str | None = None) -> int:
        return 1000

    async def close(self) -> None:
        pass

    def abort(self) -> None:
        pass


def _messages(count: int) -> list[Message]:
    return [
        Message(
            role=MessageRole.USER if idx % 2 == 0 else MessageRole.ASSISTANT,
            content=f"message-{idx}-" + "x" * 80,
        )
        for idx in range(count)
    ]


@pytest.mark.asyncio
async def test_provider_error_degrades_to_truncation_without_raising() -> None:
    """403 报文体（含花括号）→ 降级截断、不抛、状态可辨。"""
    provider_registry.clear()
    provider_registry.register(
        "raising",
        _RaisingProvider(Exception("Error code: 403 - {'error': {'message': 'quota exceeded'}}")),
    )
    llm_service.configure_test_profiles(default_provider="raising", default_model="raising-model")
    try:
        manager = ContextWindowManager(model_context_window=1000)
        messages = _messages(8)

        compressed, info = await manager.maybe_compress_messages(messages, max_tokens=200, force=True)

        assert info["compressed"] is True
        assert info["method"] == "truncation"
        assert info["status"] == "FAILED_PROVIDER_ERROR"
        assert "403" in str(info.get("error") or "")
        assert len(compressed) < len(messages)
    finally:
        provider_registry.clear()
        llm_service.reset_for_tests()


@pytest.mark.asyncio
async def test_plain_provider_error_also_degrades() -> None:
    """不带花括号的普通异常同样只降级（对照组：降级不依赖报文形态）。"""
    provider_registry.clear()
    provider_registry.register("raising", _RaisingProvider(RuntimeError("connection reset")))
    llm_service.configure_test_profiles(default_provider="raising", default_model="raising-model")
    try:
        manager = ContextWindowManager(model_context_window=1000)
        compressed, info = await manager.maybe_compress_messages(_messages(8), max_tokens=200, force=True)

        assert info["compressed"] is True
        assert info["method"] == "truncation"
        assert info["status"] == "FAILED_PROVIDER_ERROR"
        assert len(compressed) < 8
    finally:
        provider_registry.clear()
        llm_service.reset_for_tests()
