"""工具行 → Matrix 房间的 ``[COARA_TOOL]`` 信封"""

from __future__ import annotations

import json
from typing import Any

TOOL_ENVELOPE_PREFIX = "[COARA_TOOL]"


def build_matrix_tool_message(frame: dict[str, Any]) -> str:
    """把内核的工具帧装配成 ``[COARA_TOOL]{...}`` 消息体；帧无有效工具行时返回空串"""
    from src.coara.display import strip_tool_error_suffix

    label = strip_tool_error_suffix(str(frame.get("text") or "").strip())
    if not label:
        return ""
    payload: dict[str, Any] = {
        "tool_call_id": str(frame.get("tool_call_id") or ""),
        "tool_name": str(frame.get("tool_name") or ""),
        "label": label,
        "is_error": bool(frame.get("is_error", False)),
        "turn_id": str(frame.get("turn_id") or ""),
        "parent_tool_call_id": str(frame.get("parent_tool_call_id") or ""),
    }
    # 慢工具预览：running=true 时端上转圈；完成后的帧不带此字段（或 false），
    # 端上按 tool_call_id 覆盖同一行。
    if bool(frame.get("running")):
        payload["running"] = True
    duration = frame.get("duration_ms")
    if isinstance(duration, (int, float)) and not payload.get("running"):
        payload["duration_ms"] = int(duration)
    # 与 Web 折叠过程条目同形：depth / 节点身份（有则带，端上缩进与编排分组）
    depth = frame.get("depth")
    if isinstance(depth, int) and depth > 0:
        payload["depth"] = depth
    for key in ("subagent_id", "coara_id"):
        value = str(frame.get(key) or "").strip()
        if value:
            payload[key] = value
    return f"{TOOL_ENVELOPE_PREFIX}{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"
