"""工具行 → Matrix 房间的 ``[COARA_TOOL]`` 信封"""

from __future__ import annotations

import json
from typing import Any

TOOL_ENVELOPE_PREFIX = "[COARA_TOOL]"


def build_matrix_tool_message(frame: dict[str, Any]) -> str:
    """把内核的工具帧装配成 ``[COARA_TOOL]{...}`` 消息体；帧无有效工具行时返回空串"""
    label = str(frame.get("text") or "").strip()
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
    return f"{TOOL_ENVELOPE_PREFIX}{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"
