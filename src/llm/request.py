"""LLM request and resolved call descriptors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from src.core.types import Message
from src.llm.provider import LLMProvider


@dataclass
class LLMRequest:
    """Description of a single LLM completion request."""

    messages: list[Message]
    profile: str | None = None
    provider: str | None = None
    model: str | None = None
    system_prompt: str | None = None
    tools: list[dict[str, Any]] | None = None
    max_tokens: int | None = None
    temperature: float | None = None
    tool_choice: dict[str, Any] | None = None
    signal: Any | None = None
    on_assistant_delta: Callable[[str], Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedLLMCall:
    """Fully resolved provider + model + call parameters."""

    profile: str
    provider_name: str
    model: str
    provider: LLMProvider
    max_tokens: int
    temperature: float
