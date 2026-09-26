"""Tests for assistant content normalization."""

from __future__ import annotations

from src.llm.message_content import (
    openai_assistant_content,
    strip_thinking_from_content,
    visible_text_from_blocks,
)
from src.llm.provider import LLMResponse


def test_visible_text_from_blocks() -> None:
    blocks = [
        {"type": "thinking", "thinking": "hidden"},
        {"type": "text", "text": "hello"},
    ]
    assert visible_text_from_blocks(blocks) == "hello"


def test_strip_thinking_from_content_returns_plain_text() -> None:
    content = [
        {"type": "thinking", "thinking": "drop"},
        {"type": "text", "text": "visible"},
    ]
    assert strip_thinking_from_content(content) == "visible"


def test_openai_assistant_content_strips_thinking_blocks() -> None:
    content = [
        {"type": "thinking", "thinking": "internal"},
        {"type": "text", "text": "reply"},
    ]
    assert openai_assistant_content(content) == "reply"


def test_assistant_storage_fields_pass_through() -> None:
    response = LLMResponse(content="hello", reasoning_content="chain")
    assert response.assistant_storage_fields() == ("hello", "chain")
