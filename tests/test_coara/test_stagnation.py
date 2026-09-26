"""Stagnation / progress detection for mutating tool calls."""

from __future__ import annotations

from src.agent.executor import ToolExecution
from src.coara.stagnation import execution_made_progress
from src.core.tool_base import ToolResult
from src.core.types import ToolCall


def _edit_execution(old: str, new: str, *, path: str = "D:/ws/gen_resume.py") -> ToolExecution:
    return ToolExecution(
        index=0,
        tool_call=ToolCall(
            id="edit-1",
            name="edit",
            arguments={"path": path, "old_string": old, "new_string": new},
        ),
        result=ToolResult.success(
            f"Edited {path}: replaced (1 match)",
            metadata={"path": path, "replace_all": False, "matches_found": 1},
        ),
    )


def test_consecutive_edits_with_different_strings_count_as_progress() -> None:
    """Regression: iterative StrReplace-style edits must not trip stagnation."""
    signatures: set[str] = set()
    for i in range(8):
        progressed, signatures = execution_made_progress(
            _edit_execution(f"old-{i}", f"new-{i}"),
            signatures,
        )
        assert progressed is True


def test_identical_edit_arguments_do_not_count_as_repeated_progress() -> None:
    signatures: set[str] = set()
    first, signatures = execution_made_progress(
        _edit_execution("same", "other"),
        signatures,
    )
    second, signatures = execution_made_progress(
        _edit_execution("same", "other"),
        signatures,
    )
    assert first is True
    assert second is False


def test_read_tool_still_dedupes_identical_success() -> None:
    signatures: set[str] = set()
    result = ToolResult.success("file contents", metadata={"path": "a.py"})
    for _ in range(3):
        execution = ToolExecution(
            index=0,
            tool_call=ToolCall(id="r1", name="read", arguments={"path": "a.py"}),
            result=result,
        )
        progressed, signatures = execution_made_progress(execution, signatures)
    assert progressed is False
