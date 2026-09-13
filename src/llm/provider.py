"""Common interfaces for LLM providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from src.core.types import Message, ToolCall


@dataclass
class ToolCallDelta:
    index: int = 0
    id: str = ""
    name: str = ""
    arguments_fragment: str = ""


class LLMResponse:
    """Normalized LLM response."""

    def __init__(
        self,
        content: str = "",
        tool_calls: list[ToolCall] | None = None,
        finish_reason: str = "stop",
        usage: dict[str, int] | None = None,
        provider_content_blocks: list[dict[str, Any]] | None = None,
        reasoning_content: str | None = None,
    ):
        self.content = content
        self.tool_calls = tool_calls or []
        self.finish_reason = finish_reason
        self.usage = usage or {}
        blocks = provider_content_blocks or []
        # MiniMax: thinking lives in blocks only. MiMo: flat reasoning_content on plain-text turns.
        self.provider_content_blocks = blocks
        self.reasoning_content = None if blocks else reasoning_content

    def assistant_storage_fields(self) -> tuple[str | list[dict[str, Any]], str | None]:
        """Return visible (content, reasoning_content) for message_history — no thinking blocks."""
        if self.provider_content_blocks:
            from src.llm.message_content import visible_text_from_blocks

            visible = visible_text_from_blocks(self.provider_content_blocks) or self.content or ""
            return visible, None
        return self.content or "", self.reasoning_content

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)

    def __repr__(self) -> str:
        return (
            "LLMResponse("
            f"content={self.content!r}, "
            f"tool_calls={len(self.tool_calls)}, "
            f"finish_reason={self.finish_reason!r})"
        )


class StreamChunk:
    """Normalized streaming chunk."""

    def __init__(
        self,
        delta_content: str = "",
        delta_reasoning: str = "",
        delta_tool_calls: list[ToolCallDelta] | None = None,
        finish_reason: str | None = None,
        usage: dict[str, int] | None = None,
    ):
        self.delta_content = delta_content
        self.delta_reasoning = delta_reasoning
        self.delta_tool_calls = delta_tool_calls or []
        self.finish_reason = finish_reason
        # Provider-reported usage snapshot (Anthropic message_start/delta, OpenAI final chunk).
        self.usage = usage


class LLMProvider(ABC):
    """Abstract provider interface."""

    DEFAULT_MAX_TOKENS = 16384

    def __init__(
        self,
        name: str,
        api_key: str,
        base_url: str | None = None,
        default_model: str = "",
        default_max_tokens: int | None = None,
    ):
        self.name = name
        self.api_key = api_key
        self.base_url = base_url
        self.default_model = default_model
        self.default_max_tokens = default_max_tokens or self.DEFAULT_MAX_TOKENS

    @abstractmethod
    async def complete(
        self,
        messages: list[Message],
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        tools: list[dict[str, Any]] | None = None,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Perform a non-streaming completion."""

    @abstractmethod
    async def stream_complete(
        self,
        messages: list[Message],
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        tools: list[dict[str, Any]] | None = None,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        """Perform a streaming completion."""

    @abstractmethod
    def get_context_window(self, model: str | None = None) -> int:
        """Return the context window size for a model."""

    def supports_tools(self, model: str | None = None) -> bool:
        """Whether the provider supports native tool calling."""
        return True

    def get_default_max_tokens(self, model: str | None = None) -> int:
        return self.default_max_tokens

    @abstractmethod
    async def close(self) -> None:
        """Release provider resources (HTTP clients, event loops, etc.)."""
        pass

    @abstractmethod
    def abort(self) -> None:
        """Forcefully abort any pending requests (close and recreate HTTP client).

        This is used for immediate interruption on platforms where asyncio
        cancellation does not promptly propagate to the HTTP layer.
        """
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.name!r}, model={self.default_model!r})"
