"""Line-based read-back for spilled tool outputs (shared by read(ref) and Dashboard)."""

from __future__ import annotations

from typing import Any

from src.core.read_format import (
    count_text_lines,
    read_slice_start_line,
    resolve_ref_read_limit,
)
from src.core.text import slice_text_by_lines


def read_spill_lines(
    content: str,
    *,
    offset: int | None = None,
    limit: int | None = None,
) -> tuple[str, int, int | None, bool]:
    """Slice spilled text by lines (same semantics as read(ref=…)).

    Returns ``(chunk, total_lines, effective_limit, is_partial)``.
    """
    total_lines = count_text_lines(content)
    effective_limit = resolve_ref_read_limit(limit=limit, total_lines=total_lines)
    chunk = slice_text_by_lines(content, offset, effective_limit)
    if total_lines == 0:
        return chunk, 0, effective_limit, False
    start_line = read_slice_start_line(total_lines, offset)
    shown_lines = count_text_lines(chunk)
    is_partial = start_line + shown_lines - 1 < total_lines
    return chunk, total_lines, effective_limit, is_partial


def read_spill_api_payload(
    content: str,
    *,
    line_offset: int,
    line_limit: int,
) -> dict[str, Any]:
    """Dashboard/API view of a spilled body (line pagination)."""
    chunk, total_lines, effective_limit, is_partial = read_spill_lines(
        content,
        offset=line_offset,
        limit=line_limit,
    )
    start_line = read_slice_start_line(total_lines, line_offset)
    lines_shown = count_text_lines(chunk)
    next_offset = (start_line + lines_shown) if is_partial else None
    return {
        "content": chunk,
        "offset": start_line,
        "limit": effective_limit or line_limit,
        "lines_shown": lines_shown,
        "total_lines": total_lines,
        "next_offset": next_offset,
        "truncated": is_partial,
    }
