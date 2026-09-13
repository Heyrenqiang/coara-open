"""Flatten message content blocks to plain text."""

from __future__ import annotations

import json
from typing import Any


def message_content_to_text(content: str | list[dict[str, Any]] | None) -> str:
    """Flatten message content blocks to plain text for trace/dashboard display."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif isinstance(block, str):
                parts.append(block)
        if parts:
            return "\n".join(parts)
        return json.dumps(content, ensure_ascii=False)
    return str(content)
