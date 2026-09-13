"""Tests for Anthropic prompt-cache breakpoint placement."""

from __future__ import annotations

from src.core.types import Message, MessageRole
from src.llm.anthropic import AnthropicProvider

_EPHEMERAL = {"type": "ephemeral"}


def _provider(base_url: str | None) -> AnthropicProvider:
    return AnthropicProvider(name="t", api_key="test", base_url=base_url)


def test_cache_control_does_not_mutate_message_content() -> None:
    # Regression (#48): list content blocks alias Message.content — marking the
    # last block in place permanently tagged the caller's history.
    blocks = [
        {"type": "text", "text": "look"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAA"}},
    ]
    provider = _provider(None)
    _, converted = provider._convert_messages([Message(role=MessageRole.USER, content=blocks)], None)

    assert converted[-1]["content"][-1]["cache_control"] == _EPHEMERAL
    assert all("cache_control" not in block for block in blocks)


def test_real_anthropic_endpoint_enables_prompt_cache() -> None:
    # #35: real Anthropic endpoints get the same 3 breakpoints as compat ones.
    provider = _provider(None)
    system, converted = provider._convert_messages(
        [Message(role=MessageRole.USER, content="hi")],
        "sys",
    )
    assert system == [{"type": "text", "text": "sys", "cache_control": _EPHEMERAL}]
    assert converted[-1]["content"][-1]["cache_control"] == _EPHEMERAL

    tools = provider._convert_tools(
        [{"name": "t", "description": "d", "parameters": {"type": "object", "properties": {}}}]
    )
    assert tools is not None and tools[-1]["cache_control"] == _EPHEMERAL


def test_compat_endpoint_cache_control_unchanged() -> None:
    provider = _provider("https://api.minimaxi.com/anthropic")
    system, converted = provider._convert_messages(
        [Message(role=MessageRole.USER, content="hi")],
        "sys",
    )
    assert system == [{"type": "text", "text": "sys", "cache_control": _EPHEMERAL}]
    assert converted == [
        {
            "role": "user",
            "content": [{"type": "text", "text": "hi", "cache_control": _EPHEMERAL}],
        }
    ]
