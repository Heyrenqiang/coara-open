"""Todo trace event payload helpers (CLI / Dashboard; not model-facing)."""

from __future__ import annotations

from typing import Any


def build_todo_update_trace_payload(metadata: dict[str, Any], session_id: str) -> dict[str, Any]:
    """Shape EventBus `todo_update` payload from tool result metadata."""
    payload: dict[str, Any] = {
        "summary": metadata.get("summary", ""),
        "todos": metadata.get("todos", []),
        "session_id": session_id,
    }
    action = metadata.get("action")
    if action:
        payload["action"] = action
    changes = metadata.get("changes")
    if changes:
        payload["changes"] = changes
    return payload
