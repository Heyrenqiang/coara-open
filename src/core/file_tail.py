"""Bounded reads from potentially large log files."""

from __future__ import annotations

from pathlib import Path


def read_tail_text(path: Path, *, max_chars: int = 8000, read_bytes: int | None = None) -> str:
    """Read up to ``max_chars`` from the end of a UTF-8 text file without loading the whole file."""
    if not path.exists():
        return ""
    size = path.stat().st_size
    if size == 0:
        return ""
    chunk_bytes = read_bytes or max(max_chars * 4, 65_536)
    chunk_bytes = min(chunk_bytes, size)
    with path.open("rb") as handle:
        handle.seek(-chunk_bytes, 2)
        raw = handle.read()
    text = raw.decode("utf-8", errors="replace")
    if len(text) <= max_chars:
        return text
    omitted = max(0, size - chunk_bytes)
    prefix = f"... ({omitted} bytes omitted)\n" if omitted else "... (truncated)\n"
    return prefix + text[-max_chars:]
