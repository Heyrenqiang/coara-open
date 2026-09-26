"""Normalize assistant message content for history and LLM requests.

Conversation history (``Message.content``) holds user-visible text only.
Provider thinking is stored in ``Message.reasoning_content`` and merged back
when building API payloads.
"""

from __future__ import annotations

from typing import Any


def visible_text_from_blocks(blocks: list[Any]) -> str:
    """Extract user-visible text from provider content blocks."""
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "".join(parts)


def strip_thinking_from_content(content: str | list[dict[str, Any]]) -> str | list[dict[str, Any]]:
    """Remove thinking blocks; keep text and tool_use blocks."""
    if not isinstance(content, list):
        return content

    stripped: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "thinking":
            continue
        stripped.append(block)

    if not stripped:
        return ""

    if len(stripped) == 1 and stripped[0].get("type") == "text":
        return str(stripped[0].get("text", ""))

    return stripped


def openai_assistant_content(content: str | list[dict[str, Any]]) -> str:
    """OpenAI-compatible assistant ``content`` string (MiMo/GPT); drops thinking blocks."""
    if not isinstance(content, list):
        return str(content or "")

    stripped = strip_thinking_from_content(content)
    if isinstance(stripped, str):
        return stripped
    return visible_text_from_blocks(stripped)
