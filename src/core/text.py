"""文本与路径原语（core 层，零上层依赖）.

runtime 溢出读取（runtime/spill_read）、workspace 虚拟路径（workspace/vfs）与文件
工具（tools/builtin/file_io）共用的按行切片与绝对路径校验。只依赖标准库。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def slice_text_by_lines(content: str, offset: int | None, limit: int | None) -> str:
    """Return a slice of text by 1-based line offset (negative counts from end)."""
    if not content:
        return ""
    lines = content.splitlines(keepends=True)
    if not lines and content:
        lines = [content]

    total = len(lines)
    if offset is None or offset == 0:
        start_line = 1
    elif offset < 0:
        start_line = max(1, total + offset + 1)
    else:
        start_line = offset

    start_idx = min(max(0, start_line - 1), total)
    end_idx = total if limit is None or limit <= 0 else min(total, start_idx + limit)
    return "".join(lines[start_idx:end_idx])


def is_absolute_path(candidate: str) -> bool:
    """Return True for absolute filesystem paths."""
    stripped = str(candidate or "").strip()
    if not stripped:
        return False
    return Path(stripped).expanduser().is_absolute()


def absolute_path_error(raw_path: str, action: str) -> str | None:
    """Return an error message when *raw_path* is not an absolute path."""
    candidate = str(raw_path or "").strip()
    if not candidate:
        return f"{action} 路径不能为空"
    if not is_absolute_path(candidate):
        return f"路径必须是绝对路径，不支持相对路径: {raw_path}"
    return None


def preview_line(text: Any, limit: int) -> str:
    """Collapse newlines to spaces and truncate to *limit* chars with a trailing ``...``."""
    collapsed = str(text or "").replace("\r", " ").replace("\n", " ").strip()
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[:limit]}..."


def normalize_lock_key(path: Any) -> str | None:
    """Normalize a file path into a stable write-lock key (resolved, lowercased).

    Mirrors the file tools' ``get_write_lock`` contract: ``None`` for
    non-string/empty input, otherwise the resolved absolute path lowercased
    (raw string lowercased when resolution fails).
    """
    if not isinstance(path, str) or not path:
        return None
    try:
        return str(Path(path).expanduser().resolve()).lower()
    except Exception:
        return str(path).lower()


def render_table_as_markdown(table) -> str:
    """Render a duck-typed table (rows → cells → text) as a markdown table string.

    Shared by the Office readers (python-pptx / python-docx) for LLM-facing
    text extraction. Cell text is flattened (newlines → spaces) and padded to
    a uniform width; the first row is treated as the header.
    """
    rows: list[list[str]] = []
    for row in table.rows:
        cells = [cell.text.replace("\n", " ").strip() for cell in row.cells]
        rows.append(cells)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]

    header = rows[0]
    lines = ["| " + " | ".join(header) + " |"]
    lines.append("| " + " | ".join("---" for _ in range(width)) + " |")
    for body_row in rows[1:]:
        lines.append("| " + " | ".join(body_row) + " |")
    return "\n".join(lines)


def format_elapsed(seconds: float) -> str:
    """Format a duration in seconds as a compact human-readable string."""
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        minutes, secs = divmod(total, 60)
        return f"{minutes}m {secs:02d}s"
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}h {minutes:02}m {secs:02d}s"
