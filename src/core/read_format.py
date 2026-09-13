"""Read tool LLM output formatting — Cursor-aligned line-number prefix."""

from __future__ import annotations

# Cursor truncates overlong lines in tool output (keeps LLM prefix stable).
DEFAULT_MAX_LINE_CHARS = 2000
# Auto line cap when read(ref=…) omits limit on a large spilled body.
DEFAULT_REF_READ_LIMIT_LINES = 500
# Auto line cap when read(path=…) omits limit on a large file (explicit limit/offset mid-file unchanged).
DEFAULT_FILE_READ_LIMIT_LINES = 2000


def count_text_lines(content: str) -> int:
    if not content:
        return 0
    lines = content.splitlines()
    return len(lines) if lines else 1


def resolve_ref_read_limit(
    *,
    limit: int | None,
    total_lines: int,
) -> int | None:
    """Effective line limit for read(ref=…). Caps unbounded reads of large spills."""
    if limit is not None:
        return None if limit <= 0 else limit
    if total_lines <= DEFAULT_REF_READ_LIMIT_LINES:
        return None
    return DEFAULT_REF_READ_LIMIT_LINES


def resolve_file_read_limit(
    *,
    offset: int | None,
    limit: int | None,
    total_lines: int,
) -> int | None:
    """Effective line limit for read(path=…).

    - Explicit ``limit`` wins (``<=0`` means no cap).
    - Explicit ``offset`` without ``limit`` still reads through EOF (legacy).
    - Neither set: cap large files at ``DEFAULT_FILE_READ_LIMIT_LINES``.
    """
    if limit is not None:
        return None if limit <= 0 else limit
    if offset is not None:
        return None
    if total_lines <= DEFAULT_FILE_READ_LIMIT_LINES:
        return None
    return DEFAULT_FILE_READ_LIMIT_LINES


def read_slice_start_line(total_lines: int, offset: int | None) -> int:
    """1-based line number of the first line in a read slice."""
    if offset is None:
        return 1
    if offset < 0:
        return max(1, total_lines + offset + 1)
    return max(1, offset)


def truncate_line(line: str, *, max_chars: int = DEFAULT_MAX_LINE_CHARS) -> tuple[str, bool]:
    if len(line) <= max_chars:
        return line, False
    return line[:max_chars] + "…", True


def format_read_text_for_llm(
    text: str,
    *,
    start_line: int = 1,
    max_line_chars: int = DEFAULT_MAX_LINE_CHARS,
) -> str:
    """Prefix each line with ``LINE|content`` for LLM navigation (Cursor style)."""
    if not text:
        return text
    lines = text.splitlines()
    if not lines and text:
        lines = [text]
    end_line = start_line + len(lines) - 1
    gutter = len(str(end_line))
    formatted: list[str] = []
    for idx, line in enumerate(lines):
        clipped, _ = truncate_line(line, max_chars=max_line_chars)
        formatted.append(f"{start_line + idx:>{gutter}}|{clipped}")
    return "\n".join(formatted)
