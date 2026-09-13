"""Tool result wrapper — replaces the old `<结果>` tag with lightweight markup.

Strategy:
- Success with content → return as-is (no wrapper, saves tokens).
- Success but empty → `<系统消息>工具输出为空</系统消息>` (informational).
- Error → `<系统消息>工具执行失败：...</系统消息>` (informational, not authoritative).
- Non-text list → `<系统消息>工具返回了非文本内容</系统消息>` + raw blocks.
"""

from __future__ import annotations

from typing import Any

from src.core.message_tags import system_info
from src.core.tool_base import ToolResult


def wrap_tool_result(tool_name: str, result: ToolResult) -> list[dict[str, Any]]:
    """Wrap a tool result for insertion into message history.

    Returns a list of content blocks (dicts) to preserve multi-block structure.
    """
    content = result.content

    if result.is_cancelled:
        # 默认统一为通用取消文案；执行器对「执行中被中断」的调用经
        # preserve_cancel_content 标记显式保留其真实文案（如实入史，不谎标）
        content_text = str(content).strip() if isinstance(content, str) else ""
        if content_text and (result.metadata or {}).get("preserve_cancel_content"):
            return [
                {
                    "type": "text",
                    "text": system_info(content_text),
                }
            ]
        return [
            {
                "type": "text",
                "text": system_info(f"工具 {tool_name} 的执行已被用户取消。"),
            }
        ]

    if result.is_error:
        error_msg = str(content) if isinstance(content, str) else "工具执行失败。"
        return [
            {
                "type": "text",
                "text": system_info(f"工具执行失败：{error_msg}"),
            }
        ]

    # Success cases
    if isinstance(content, str):
        if not content.strip():
            return [{"type": "text", "text": system_info("工具输出为空")}]
        # Direct return — no "tool completed" wrapper, saves tokens per turn.
        return [{"type": "text", "text": content}]

    if isinstance(content, list):
        has_text = any(
            isinstance(block, dict) and block.get("type") == "text" and str(block.get("text", "")).strip()
            for block in content
        )
        if not has_text:
            return [{"type": "text", "text": system_info("工具返回了非文本内容")}] + list(content)
        # Direct return — keep original blocks untouched.
        return list(content)

    return [{"type": "text", "text": str(content)}]
