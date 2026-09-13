"""Gate-channel preview builders (before execute, no disk write)."""

from __future__ import annotations

from typing import Any

from src.coara.tool_output.diff import build_diff_display
from src.coara.tool_output.types import DiffDisplayBlock
from src.tools.builtin.file_io.edit_logic import simulate_edit_replacement
from src.tools.builtin.file_io.file_support import load_text_with_snapshot_fallback, read_state_key


async def build_gate_preview_blocks(invocation: Any) -> list[DiffDisplayBlock] | None:
    """Compact diff preview for edit confirmations only."""
    if not (hasattr(invocation, "old_string") and hasattr(invocation, "new_string")):
        return None

    path, path_error = invocation._resolve_file_path_or_error(invocation.path, "Edit", allow_missing=True)
    if path_error or path is None or not path.exists():
        return None

    read_state = invocation._read_states.get(read_state_key(path)) if invocation._read_states is not None else None
    try:
        content, _ = load_text_with_snapshot_fallback(path, read_state, tool_name="edit")
    except (UnicodeDecodeError, OSError):
        return None

    simulated = simulate_edit_replacement(
        content,
        invocation.old_string,
        invocation.new_string,
        replace_all=bool(invocation.replace_all),
    )
    if not simulated.ok:
        return None

    return await build_diff_display(str(path), content, simulated.new_content)
