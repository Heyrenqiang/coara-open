"""Normalize assistant message content for history and LLM requests.

Conversation history (``Message.content``) holds user-visible text only.
Provider thinking is stored in ``Message.provider_wire_blocks`` / ``reasoning_content``
and merged back when building API payloads.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.core.types import Message
    from src.llm.provider import LLMResponse


def visible_text_from_blocks(blocks: list[Any]) -> str:
    """Extract user-visible text from provider content blocks."""
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "".join(parts)


def strip_thinking_from_content(content: str | list[dict[str, Any]]) -> str | list[dict[str, Any]]:
    """Remove thinking blocks; keep text and tool_use blocks (Anthropic shape)."""
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


def extract_thinking_wire_blocks(response: LLMResponse) -> list[dict[str, Any]] | None:
    """Extract thinking blocks for API replay (MiniMax / Kimi Anthropic wire)."""
    if response.provider_content_blocks:
        thinking = [
            block
            for block in response.provider_content_blocks
            if isinstance(block, dict) and block.get("type") == "thinking"
        ]
        return thinking or None
    if response.reasoning_content:
        return [{"type": "thinking", "thinking": response.reasoning_content}]
    return None


def anthropic_assistant_wire_content(msg: Message) -> list[dict[str, Any]]:
    """Build Anthropic assistant content blocks for API (incl. wire-only thinking).

    Never emit empty/whitespace ``text`` blocks — Kimi Code returns
    ``400 text content is empty`` when they appear (including tool-only turns).
    """
    blocks: list[dict[str, Any]] = []
    if msg.provider_wire_blocks:
        blocks.extend(_sanitize_anthropic_content_blocks(msg.provider_wire_blocks))

    visible = str(msg.content or "").strip()
    if visible:
        blocks.append({"type": "text", "text": visible})

    if msg.tool_calls:
        for tc in msg.tool_calls:
            blocks.append(
                {
                    "type": "tool_use",
                    "id": tc.id,
                    "name": tc.name,
                    "input": tc.arguments,
                }
            )

    # Anthropic requires at least one content block; prefer a non-empty placeholder
    # only when there is truly nothing else to send.
    return blocks or [{"type": "text", "text": "(empty)"}]


def _sanitize_anthropic_content_blocks(blocks: list[Any]) -> list[dict[str, Any]]:
    """Drop empty/whitespace text blocks; keep thinking / tool_use / images."""
    out: list[dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = str(block.get("text") or "").strip()
            if not text:
                continue
            cleaned = dict(block)
            cleaned["text"] = text
            out.append(cleaned)
            continue
        if block.get("type") == "thinking":
            thinking = str(block.get("thinking") or "").strip()
            if not thinking:
                continue
            cleaned = dict(block)
            cleaned["thinking"] = thinking
            out.append(cleaned)
            continue
        out.append(block)
    return out


def openai_assistant_content(content: str | list[dict[str, Any]]) -> str:
    """OpenAI-compatible assistant ``content`` string (MiMo/GPT); drops Anthropic thinking blocks."""
    if not isinstance(content, list):
        return str(content or "")

    stripped = strip_thinking_from_content(content)
    if isinstance(stripped, str):
        return stripped
    return visible_text_from_blocks(stripped)
