"""Resolve the active Root LLM provider context (CLI / Matrix)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.llm.anthropic import AnthropicProvider
from src.llm.openai import OpenAIProvider


@dataclass(frozen=True, slots=True)
class ActiveLlmContext:
    provider_name: str
    model: str
    base_url: str
    driver: str


def resolve_active_llm(root: Any) -> ActiveLlmContext:
    """Return provider/model/base_url/driver for the active Root LLM."""
    provider_name = str(getattr(root, "provider_name", "") or "")
    model_name = str(getattr(root, "model_name", "") or "")
    provider = getattr(root, "provider", None)
    base_url = str(getattr(provider, "base_url", "") or "")
    driver = ""
    if isinstance(provider, AnthropicProvider):
        driver = "anthropic"
    elif isinstance(provider, OpenAIProvider):
        driver = "openai"
    return ActiveLlmContext(provider_name, model_name, base_url, driver)
