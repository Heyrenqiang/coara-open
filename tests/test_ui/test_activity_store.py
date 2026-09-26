"""Tests for activity projection (CLI live display)."""

from __future__ import annotations

from src.core.events import TraceEvent
from src.ui.activity_store import workspace_trace_event


def test_output_truncation_recovery_visibility() -> None:
    event = TraceEvent(
        coara_id="root-1",
        coara_name="root",
        event_type="output_truncation_recovery",
        message="模型输出已截断，已切换落盘模式",
        level="warning",
        payload={
            "session_id": "sess-1",
            "policy": "force_tool",
            "user_notice": "模型输出已截断（max_tokens），已切换落盘模式",
        },
    )
    activity = workspace_trace_event(event)
    assert activity.visibility == "status"
    assert "截断" in activity.summary
    assert "落盘" in activity.summary


def test_subagent_start_uses_parent_tool_call_id() -> None:
    event = TraceEvent(
        coara_id="root-1",
        coara_name="root",
        event_type="subagent_start",
        message="Subagent started",
        payload={
            "session_id": "sess-1",
            "subagent_id": "sa-research-1",
            "parent_tool_call_id": "call-delegate-9",
        },
    )
    activity = workspace_trace_event(event)
    assert activity.parent_activity_id == "call-delegate-9"
    assert activity.activity_id == "sa-research-1"
