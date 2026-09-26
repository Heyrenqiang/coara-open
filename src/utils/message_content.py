"""Flatten message content blocks to plain text."""

from __future__ import annotations

import json
from typing import Any


def _image_placeholder(block: dict[str, Any]) -> str:
    """简短图片占位：[图片: media_type, NKB]（N 按 base64 长度折算，永不回显 base64）。"""
    source = block.get("source")
    media_type = "unknown"
    base64_len = 0
    if isinstance(source, dict):
        media_type = str(source.get("media_type") or "unknown")
        data = source.get("data")
        if isinstance(data, str):
            base64_len = len(data)
    size_kb = round(base64_len * 3 / 4 / 1024)
    return f"[图片: {media_type}, {size_kb}KB]"


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
            elif isinstance(block, dict) and block.get("type") == "image":
                parts.append(_image_placeholder(block))
            elif isinstance(block, str):
                parts.append(block)
        if parts:
            return "\n".join(parts)
        return json.dumps(content, ensure_ascii=False)
    return str(content)
