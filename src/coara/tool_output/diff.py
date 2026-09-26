"""Diff display block builder — CPU-bound grouping with context windows."""

from __future__ import annotations

import asyncio
from difflib import SequenceMatcher

from src.coara.tool_output.types import DiffDisplayBlock

N_CONTEXT_LINES = 3
_HUGE_FILE_LINE_THRESHOLD = 10_000


def _build_diff_blocks_sync(path: str, old_text: str, new_text: str) -> list[DiffDisplayBlock]:
    if old_text == new_text:
        return []

    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    max_lines = max(len(old_lines), len(new_lines))

    if max_lines > _HUGE_FILE_LINE_THRESHOLD:
        old_desc = f"({len(old_lines)} lines)"
        if len(old_lines) == len(new_lines):
            new_desc = f"({len(new_lines)} lines, modified)"
        else:
            new_desc = f"({len(new_lines)} lines)"
        return [
            DiffDisplayBlock(
                path=path,
                old_text=old_desc,
                new_text=new_desc,
                is_summary=True,
            )
        ]

    is_new_file = not old_lines and bool(new_lines)
    matcher = SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    blocks: list[DiffDisplayBlock] = []
    for group in matcher.get_grouped_opcodes(n=N_CONTEXT_LINES):
        if not group:
            continue
        i1 = group[0][1]
        i2 = group[-1][2]
        j1 = group[0][3]
        j2 = group[-1][4]
        blocks.append(
            DiffDisplayBlock(
                path=path,
                old_text="\n".join(old_lines[i1:i2]),
                new_text="\n".join(new_lines[j1:j2]),
                old_start=i1 + 1,
                new_start=j1 + 1,
                is_new_file=is_new_file,
            )
        )
    return blocks


async def build_diff_display(path: str, old_text: str, new_text: str) -> list[DiffDisplayBlock]:
    """Build CLI diff blocks; runs diff in a thread to avoid blocking the loop."""
    if old_text == new_text:
        return []
    return await asyncio.to_thread(_build_diff_blocks_sync, path, old_text, new_text)


def count_diff_stats(blocks: list[DiffDisplayBlock]) -> tuple[int, int]:
    """Return (added_lines, removed_lines) for metadata headers."""
    added = removed = 0
    for block in blocks:
        if block.is_summary:
            continue
        old_lines = block.old_text.splitlines() if block.old_text else []
        new_lines = block.new_text.splitlines() if block.new_text else []
        matcher = SequenceMatcher(None, old_lines, new_lines, autojunk=False)
        for tag, _i1, _i2, _j1, _j2 in matcher.get_opcodes():
            if tag == "insert":
                added += _j2 - _j1
            elif tag == "delete":
                removed += _i2 - _i1
            elif tag == "replace":
                removed += _i2 - _i1
                added += _j2 - _j1
    return added, removed
