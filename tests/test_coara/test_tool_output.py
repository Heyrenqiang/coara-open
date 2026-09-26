"""Tests for tool output framework (read format, diff, syntax, diff render)."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.coara.diff_render import DiffLine, DiffLineKind, flatten_diff_lines
from src.coara.tool_output.diff import build_diff_display, count_diff_stats
from src.coara.tool_output.syntax import highlight_code
from src.coara.tool_output.types import DiffDisplayBlock
from src.core.read_format import (
    DEFAULT_MAX_LINE_CHARS,
    format_read_text_for_llm,
    read_slice_start_line,
    truncate_line,
)
from src.tools.builtin.file_io.write import WriteTool


def test_format_read_text_adds_line_numbers() -> None:
    text = "alpha\nbeta\ngamma"
    out = format_read_text_for_llm(text, start_line=1)
    assert out.splitlines()[0].endswith("|alpha")
    assert "2|beta" in out
    assert "3|gamma" in out


def test_read_slice_start_line_negative_offset() -> None:
    assert read_slice_start_line(10, -1) == 10
    assert read_slice_start_line(10, -3) == 8


@pytest.mark.asyncio
async def test_build_diff_display_counts_changes() -> None:
    blocks = await build_diff_display("a.txt", "a\nb\n", "a\nc\n")
    added, removed = count_diff_stats(blocks)
    assert added == 1
    assert removed == 1


def test_flatten_diff_lines_edge_cases() -> None:
    assert flatten_diff_lines([[DiffLine(DiffLineKind.CONTEXT, 0, 0, "")]])[0] == []
    flat, remaining = flatten_diff_lines(
        [[DiffLine(DiffLineKind.ADD, 0, 14, "**关键词**"), DiffLine(DiffLineKind.ADD, 0, 15, "")]]
    )
    assert len(flat) == 1 and flat[0].new_num == 14 and remaining == 0
    hunks = [[DiffLine(DiffLineKind.ADD, 0, i, "" if i == 15 else f"line {i}") for i in range(1, 169)]]
    flat, remaining = flatten_diff_lines(hunks, max_lines=15)
    assert len(flat) == 14 and flat[-1].code == "line 14" and remaining == 153


def test_scrollback_diff_truncates_at_cap() -> None:
    from src.coara.diff_render import MAX_SCROLLBACK_DIFF_LINES, _render_diff_table, collect_diff_hunks

    new_text = "\n".join(f"line {i}" for i in range(1, 41))
    blocks = [
        DiffDisplayBlock(
            path="game.html",
            old_text="",
            new_text=new_text,
            old_start=1,
            new_start=1,
            is_new_file=True,
        )
    ]
    hunks, added, removed = collect_diff_hunks(blocks)
    assert added == 40
    _table, remaining = _render_diff_table(
        "game.html",
        hunks,
        highlight=False,
        changed_only=False,
        max_lines=MAX_SCROLLBACK_DIFF_LINES,
    )
    # 屏幕行感知截断保留头尾并为省略提示行预留 1 行，实际展示 max_lines - 1 行
    assert remaining == 40 - (MAX_SCROLLBACK_DIFF_LINES - 1)


def test_read_truncates_long_lines() -> None:
    long = "x" * (DEFAULT_MAX_LINE_CHARS + 50)
    clipped, truncated = truncate_line(long)
    assert truncated is True
    assert clipped.endswith("…")
    assert len(clipped) == DEFAULT_MAX_LINE_CHARS + 1

    formatted = format_read_text_for_llm("short\n" + long, start_line=1)
    assert "…" in formatted


def test_syntax_highlight_python() -> None:
    text = highlight_code("def foo(): pass", "main.py")
    assert "def" in text.plain


def test_syntax_highlight_html_strips_trailing_newline() -> None:
    text = highlight_code("<!DOCTYPE html>", "index.html")
    assert text.plain == "<!DOCTYPE html>"
    assert not text.plain.endswith("\n")


def test_scrollback_diff_html_has_no_blank_rows() -> None:
    from rich.console import Console

    from src.coara.diff_render import collect_diff_hunks, render_diff_panel

    new_text = "\n".join(
        [
            "<!DOCTYPE html>",
            '<html lang="zh-CN">',
            "<head>",
            '    <meta charset="UTF-8">',
        ]
    )
    blocks = [
        DiffDisplayBlock(
            path="snake.html",
            old_text="",
            new_text=new_text,
            old_start=1,
            new_start=1,
            is_new_file=True,
        )
    ]
    hunks, added, removed = collect_diff_hunks(blocks)
    panel = render_diff_panel("snake.html", hunks, added, removed)

    console = Console(width=100, force_terminal=True, record=True)
    console.print(panel)
    for line in console.export_text().splitlines():
        if line.startswith("|") and line.endswith("|") and not line.strip("| ").strip():
            raise AssertionError(f"blank diff row: {line!r}")


@pytest.mark.asyncio
async def test_gate_preview_for_edit(tmp_path: Path) -> None:
    from src.coara.tool_output.gate_preview import build_gate_preview_blocks
    from src.tools.builtin.file_io.edit import EditTool

    target = tmp_path / "sample.py"
    target.write_text("value = 1\n", encoding="utf-8")
    tool = EditTool(read_state_store={}, workspace_root=tmp_path)
    inv = tool.create_invocation({"path": str(target), "old_string": "value = 1", "new_string": "value = 2"})
    blocks = await build_gate_preview_blocks(inv)
    assert blocks
    assert blocks[0].path == str(target)


@pytest.mark.asyncio
async def test_gate_preview_skips_write(tmp_path: Path) -> None:
    from src.coara.tool_output.gate_preview import build_gate_preview_blocks

    target = tmp_path / "sample.py"
    target.write_text("value = 1\n", encoding="utf-8")
    tool = WriteTool(read_state_store={}, workspace_root=tmp_path)
    inv = tool.create_invocation({"path": str(target), "contents": "value = 2\n"})
    assert await build_gate_preview_blocks(inv) is None
