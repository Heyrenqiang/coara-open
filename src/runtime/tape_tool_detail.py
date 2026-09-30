"""录像带 tool 帧详情字段：参数 / 结果预览 / spill 引用。

落带与端通道共用：聊天行不读这些字段；详情面板与投影读它们。
结果预览有硬上限，避免把整段 tool_output 打进 WS / 视图带。
"""

from __future__ import annotations

from typing import Any

# 落带/出站预览上限（完整正文走 tool_output_ref → spill）
_TAPE_OUTPUT_CHARS = 16_384


def pick_tape_arguments(source: dict[str, Any]) -> dict[str, Any] | None:
    """参数优先级：tape_args > arguments > usage_args。"""
    for key in ("tape_args", "arguments", "usage_args"):
        value = source.get(key)
        if isinstance(value, dict) and value:
            return value
    return None


def apply_tape_tool_detail(target: dict[str, Any], source: dict[str, Any]) -> None:
    """把详情字段写入 target（原地）。source 可以是 tool_complete payload 或已映射的 tool 帧。"""
    args = pick_tape_arguments(source)
    if args:
        target["arguments"] = args

    raw = str(source.get("tool_output") or "")
    truncated = bool(source.get("tool_output_truncated"))
    if raw:
        if len(raw) > _TAPE_OUTPUT_CHARS:
            target["tool_output"] = raw[:_TAPE_OUTPUT_CHARS] + "\n…[截断]"
            truncated = True
        else:
            target["tool_output"] = raw

    ref = str(source.get("tool_output_ref") or "").strip()
    if ref:
        target["tool_output_ref"] = ref
    if truncated:
        target["tool_output_truncated"] = True


__all__ = ["apply_tape_tool_detail", "pick_tape_arguments"]
