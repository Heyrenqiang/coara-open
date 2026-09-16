"""LLM provider 错误包装公共件.

各 driver（anthropic/openai/responses）把 SDK 层 APIStatusError 与未知异常
统一包装为 LLMError，文案与 status_code 透传口径集中在此，避免三处样板漂移
"""

from __future__ import annotations

from src.core.errors import LLMError


def wrap_api_status_error(exc: Exception, *, prefix: str, status_code: int | None = None) -> LLMError:
    """包装 provider SDK 的 APIStatusError 为 LLMError，按需透传 status_code."""
    return LLMError(f"{prefix}: {exc}", status_code=status_code)


def wrap_unexpected_error(exc: Exception) -> LLMError:
    """包装未预期异常为 LLMError."""
    return LLMError(f"Unexpected error: {exc}")
