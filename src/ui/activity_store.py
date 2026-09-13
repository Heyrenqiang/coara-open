"""Activity projection helpers for CLI live display.

Disk dual-write to ``activity/activity_events.jsonl`` is discontinued.
Canonical persistence is ``traces/trace_events.jsonl`` via TraceStore.
CLI ``activity_live`` still uses ``workspace_trace_event`` in memory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from src.core.events import TraceEvent

ActivityPhase = Literal["start", "progress", "complete", "fail", "wait"]
ActivityVisibility = Literal["silent", "status", "expanded", "debug"]


@dataclass(slots=True)
class ActivityEvent:
    """A normalized event for activity tree rendering (in-memory / CLI)."""

    activity_id: str
    parent_activity_id: str
    session_id: str
    actor_type: str
    phase: ActivityPhase
    visibility: ActivityVisibility
    summary: str
    level: str = "info"
    timestamp: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


def workspace_trace_event(event: TraceEvent) -> ActivityEvent:
    """Project a TraceEvent into an ActivityEvent."""
    payload = dict(event.payload or {})
    session_id = str(payload.get("session_id") or "")
    source = event.event_type
    actor_type = _actor_type(event, payload)
    phase = _phase_for(source)
    visibility = _visibility_for(source)
    activity_id = _activity_id(event, payload, session_id)
    parent_activity_id = str(
        payload.get("parent_activity_id")
        or payload.get("parent_tool_call_id")
        or payload.get("parent_id")
        or session_id
        or ""
    )
    summary = _summary_for(event, payload)

    return ActivityEvent(
        activity_id=activity_id,
        parent_activity_id=parent_activity_id,
        session_id=session_id,
        actor_type=actor_type,
        phase=phase,
        visibility=visibility,
        summary=summary,
        level=event.level,
        timestamp=event.timestamp,
        payload=payload,
    )


def _actor_type(event: TraceEvent, payload: dict[str, Any]) -> str:
    if event.event_type.startswith("workflow_"):
        return "workflow"
    if event.event_type.startswith("subagent_") or event.event_type.startswith("background_agent_"):
        return "subagent"
    if event.event_type.startswith("tool_") or event.event_type == "tool_call":
        return "tool"
    origin = str(payload.get("origin_scope") or "")
    if origin == "subagent_loop":
        return "subagent"
    return "root"


def _phase_for(event_type: str) -> ActivityPhase:
    if event_type.endswith("_start") or event_type.endswith("_started") or event_type in {"tool_start"}:
        return "start"
    if event_type.endswith("_complete") or event_type.endswith("_completed") or event_type in {"tool_complete"}:
        return "complete"
    if event_type.endswith("_failed") or event_type.endswith("_error") or event_type in {"llm_error"}:
        return "fail"
    return "progress"


def _visibility_for(event_type: str) -> ActivityVisibility:
    if event_type in {"tool_start", "tool_complete", "subagent_start", "subagent_complete", "workflow_started"}:
        return "expanded"
    if event_type in {"llm_turn_start", "llm_turn_complete", "context_compressed"}:
        return "debug"
    if event_type == "output_truncation_recovery":
        return "status"
    if event_type == "shell_output_matched":
        return "status"
    if event_type == "conversation_message":
        return "silent"
    if event_type == "thinking_progress":
        return "silent"
    return "status"


def _activity_id(event: TraceEvent, payload: dict[str, Any], session_id: str) -> str:
    for key in ("activity_id", "tool_call_id", "call_id", "subagent_id", "task_id"):
        raw = payload.get(key)
        if raw:
            return str(raw)
    return f"{event.event_type}:{session_id}:{event.timestamp}"


def _summary_for(event: TraceEvent, payload: dict[str, Any]) -> str:
    notice = payload.get("user_notice")
    if isinstance(notice, str) and notice.strip():
        return notice.strip()
    message = (event.message or "").strip()
    if message:
        return message
    tool = payload.get("tool_name") or payload.get("tool")
    if tool:
        return str(tool)
    return event.event_type


__all__ = [
    "ActivityEvent",
    "ActivityPhase",
    "ActivityVisibility",
    "workspace_trace_event",
]
