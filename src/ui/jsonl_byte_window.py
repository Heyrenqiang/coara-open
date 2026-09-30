"""Byte-range JSONL line reads shared by web_views hydrate and trajectory tape.

Same rule everywhere: for a mid-file window, drop the first (truncated) line and
an incomplete last line unless the raw chunk ends with a newline.

Callers that expand a window must re-read the whole enlarged contiguous range
instead of stitching two adjacent windows: the boundary line is incomplete in
both halves and would be dropped twice.
"""

from __future__ import annotations

from pathlib import Path


def iter_jsonl_lines_byte_range(path: Path, start: int, end: int) -> list[str]:
    """Return complete text lines in ``[start, end)``; empty on I/O errors."""
    if end <= start:
        return []
    try:
        with path.open("rb") as handle:
            handle.seek(start)
            raw = handle.read(end - start)
    except OSError:
        return []
    text = raw.decode("utf-8", errors="replace")
    lines = text.split("\n")
    if start > 0 and lines:
        lines = lines[1:]
    if lines and not text.endswith("\n"):
        lines = lines[:-1] if len(lines) > 1 else lines
    return [ln for ln in lines if ln.strip()]
