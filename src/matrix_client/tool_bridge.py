"""工具行 → Matrix 房间的 ``[COARA_TOOL]`` 信封。

手机端聊天流里的「工具行」是内核发的一等条目，不是正文：一条工具行一条 m.text，
行首即 ``[COARA_TOOL]`` 单边信封（与 ``[COARA_TURN]`` 同款行首锚定写法），其后
紧跟一行紧凑 JSON。端上按字段渲染成一行工具调用，并按 web 端同规则隐去
``send_file`` / ``delegate wait``（真源 ``src/ui/web/src/lib/toolVisibility.ts``）。

信封增删只改真源 ``docs/protocol/coara-envelopes.json``，再用
``scripts/dev/gen_envelopes.py`` 重新生成两端常量。
"""

from __future__ import annotations

import json
from typing import Any

TOOL_ENVELOPE_PREFIX = "[COARA_TOOL]"


def build_matrix_tool_message(frame: dict[str, Any]) -> str:
    """把内核的工具帧装配成 ``[COARA_TOOL]{...}`` 消息体；帧无有效工具行时返回空串。

    ``label`` 是端上要显示的整行文本（不含 ✓/✗，前缀由端上按 ``is_error`` 加）。
    ``parent_tool_call_id`` 非空表示这是某条 delegate 行的子智能体工具行——端上
    据此折叠，不进主列表。
    """
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
