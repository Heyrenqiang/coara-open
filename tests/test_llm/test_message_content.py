"""Tests for assistant content normalization."""

from __future__ import annotations

from src.core.types import Message, MessageRole
from src.llm.message_content import (
    anthropic_assistant_wire_content,
    extract_thinking_wire_blocks,
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


def test_assistant_storage_fields_keep_visible_text_only() -> None:
    blocks = [
        {"type": "thinking", "thinking": "internal"},
        {"type": "text", "text": "hello"},
    ]
    response = LLMResponse(content="hello", provider_content_blocks=blocks)
    assert response.assistant_storage_fields() == ("hello", None)


def test_extract_thinking_wire_blocks_from_provider_blocks() -> None:
    blocks = [
        {"type": "thinking", "thinking": "chain"},
        {"type": "text", "text": "hello"},
    ]
    response = LLMResponse(content="hello", provider_content_blocks=blocks)
    assert extract_thinking_wire_blocks(response) == [{"type": "thinking", "thinking": "chain"}]


def test_extract_thinking_wire_blocks_from_stream_reasoning() -> None:
    response = LLMResponse(content="", reasoning_content="stream chain", tool_calls=[])
    assert extract_thinking_wire_blocks(response) == [{"type": "thinking", "thinking": "stream chain"}]


def test_anthropic_wire_content_merges_wire_blocks_with_visible_text() -> None:
    msg = Message(
        role=MessageRole.ASSISTANT,
        content="hello",
        provider_wire_blocks=[{"type": "thinking", "thinking": "chain"}],
    )
    assert anthropic_assistant_wire_content(msg) == [
        {"type": "thinking", "thinking": "chain"},
        {"type": "text", "text": "hello"},
    ]


def test_anthropic_wire_omits_empty_text_on_tool_only_turn() -> None:
    from src.core.types import ToolCall

    msg = Message(
        role=MessageRole.ASSISTANT,
        content="",
        tool_calls=[ToolCall(id="t1", name="todo", arguments={"action": "read"})],
        provider_wire_blocks=[{"type": "thinking", "thinking": "plan"}],
    )
    wire = anthropic_assistant_wire_content(msg)
    assert all(not (b.get("type") == "text" and not str(b.get("text") or "").strip()) for b in wire)
    assert wire[0]["type"] == "thinking"
    assert wire[1]["type"] == "tool_use"


def test_convert_messages_replaces_empty_tool_result() -> None:
    from src.core.types import ToolCall
    from src.llm.anthropic import AnthropicProvider

    provider = AnthropicProvider(
        name="kimi",
        api_key="x",
        base_url="https://api.kimi.com/coding",
        default_model="k3",
    )
    _, converted = provider._convert_messages(
        [
            Message(role=MessageRole.USER, content="hi"),
            Message(
                role=MessageRole.ASSISTANT,
                content="",
                tool_calls=[ToolCall(id="1", name="todo", arguments={})],
            ),
            Message(role=MessageRole.TOOL_RESULT, content="", tool_call_id="1"),
        ],
        "sys",
    )
    tool_result = converted[-1]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["content"] == "(empty)"
