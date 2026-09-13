"""Stagnation detection helpers for Coara turns.

Extracted from CoaraBase to keep the core state machine lean.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def build_error_signature(executions: list[Any]) -> str | None:
    """Return a stable signature for the first failing tool in a batch, or None.

    Used by TurnStagnationGuard to detect consecutive turns hitting the same
    tool error. Only the first error is tracked so that a batch with multiple
    errors does not reset the streak unnecessarily.
    """
    first_error = next((ex for ex in executions if ex.result.is_error), None)
    if first_error is None:
        return None

    payload = {
        "tool": first_error.tool_call.name,
        "arguments": first_error.tool_call.arguments,
        "content": str(first_error.result.content)[:500],
    }
    return hashlib.md5(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()


def execution_made_progress(execution, recent_signatures: set[str]) -> tuple[bool, set[str]]:
    """Return (made_progress, updated_signatures)."""
    if execution.result.is_error:
        return False, recent_signatures

    tool_arguments = None
    if execution.tool_call.name in _MUTATING_PROGRESS_TOOLS:
        tool_arguments = execution.tool_call.arguments

    signature = _build_tool_progress_signature(
        execution.tool_call.name,
        execution.result,
        tool_arguments=tool_arguments,
    )
    if signature is None:
        return True, recent_signatures
    if signature in recent_signatures:
        return False, recent_signatures

    recent_signatures.add(signature)
    return True, recent_signatures


# Mutating tools: identical success *messages* can still be real progress when arguments differ
# (e.g. many consecutive edit() calls on the same file each replace a different old_string).
_MUTATING_PROGRESS_TOOLS = frozenset({"edit", "write", "shell"})


def _build_tool_progress_signature(
    tool_name: str,
    result,
    tool_arguments: dict[str, Any] | None = None,
) -> str | None:
    normalized_metadata = _normalize_progress_metadata(result.metadata)
    payload: dict[str, Any] = {
        "tool": tool_name,
        "content": result.content,
        "metadata": normalized_metadata,
    }
    if tool_arguments is not None:
        payload["arguments"] = tool_arguments
    serialized = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str, separators=(",", ":"))
    if not serialized:
        return None
    return hashlib.sha1(serialized.encode("utf-8", errors="replace")).hexdigest()  # noqa: S324


def _normalize_progress_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    if not metadata:
        return {}

    ignored_keys = {
        "attempts",
        "session_id",
        "child_session_id",
        "subagent_id",
        "duration_ms",
        "cache_hit",
    }
    return {key: value for key, value in metadata.items() if key not in ignored_keys}
