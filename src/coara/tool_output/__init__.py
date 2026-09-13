"""Tool Output Framework (TOF) — shape tool results for Model / Terminal / Gate.

Architecture: ``docs/TOOL_OUTPUT_FRAMEWORK.md``
Pipeline entry: ``pipeline.*``
"""

from __future__ import annotations

from src.coara.tool_output.diff import build_diff_display, count_diff_stats
from src.coara.tool_output.gate_preview import build_gate_preview_blocks
from src.coara.tool_output.pipeline import (
    render_gate_preview,
    render_terminal_blocks,
    render_terminal_from_event,
    serialize_display_blocks,
)
from src.coara.tool_output.types import DiffDisplayBlock, DisplayBlock
from src.core.read_format import (
    count_text_lines,
    format_read_text_for_llm,
    read_slice_start_line,
    resolve_file_read_limit,
    resolve_ref_read_limit,
    truncate_line,
)

__all__ = [
    "DiffDisplayBlock",
    "DisplayBlock",
    "build_diff_display",
    "build_gate_preview_blocks",
    "count_diff_stats",
    "count_text_lines",
    "format_read_text_for_llm",
    "read_slice_start_line",
    "resolve_file_read_limit",
    "resolve_ref_read_limit",
    "render_gate_preview",
    "render_terminal_blocks",
    "render_terminal_from_event",
    "serialize_display_blocks",
    "truncate_line",
]
