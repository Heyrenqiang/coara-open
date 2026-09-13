"""``[COARA_TOOL]`` 工具行信封的装配契约。"""

from __future__ import annotations

import json

from src.matrix_client.tool_bridge import TOOL_ENVELOPE_PREFIX, build_matrix_tool_message


def _payload(message: str) -> dict:
    assert message.startswith(TOOL_ENVELOPE_PREFIX)
    return json.loads(message[len(TOOL_ENVELOPE_PREFIX) :])


def test_build_tool_message_carries_label_and_identity() -> None:
    message = build_matrix_tool_message(
        {
            "text": "read(a.py)",
            "tool_name": "read",
            "tool_call_id": "c1",
            "turn_id": "t1",
            "is_error": False,
            "duration_ms": 42,
        }
    )
    payload = _payload(message)
    assert payload["label"] == "read(a.py)"
    assert payload["tool_name"] == "read"
    assert payload["tool_call_id"] == "c1"
    assert payload["turn_id"] == "t1"
    assert payload["is_error"] is False
    assert payload["duration_ms"] == 42
    # 主会话的工具行没有父标识（子智能体行才会有）
    assert payload["parent_tool_call_id"] == ""


def test_build_tool_message_marks_error_and_parent() -> None:
    message = build_matrix_tool_message(
        {
            "text": "✗ edit 报错",
            "tool_name": "edit",
            "is_error": True,
            "parent_tool_call_id": "delegate-1",
        }
    )
    payload = _payload(message)
    assert payload["is_error"] is True
    assert payload["parent_tool_call_id"] == "delegate-1"
    assert "duration_ms" not in payload


def test_build_tool_message_marks_running_preview() -> None:
    message = build_matrix_tool_message(
        {
            "text": "shell(make)",
            "tool_name": "shell",
            "tool_call_id": "c-slow",
            "turn_id": "t1",
            "running": True,
            "duration_ms": 999,  # running 帧忽略耗时
        }
    )
    payload = _payload(message)
    assert payload["running"] is True
    assert "duration_ms" not in payload
    assert payload["label"] == "shell(make)"


def test_build_tool_message_skips_empty_label() -> None:
    assert build_matrix_tool_message({"text": "   "}) == ""
    assert build_matrix_tool_message({}) == ""
