"""语义化合并（merge_adjacent_tool_blocks）行为测试。"""

from __future__ import annotations

from src.cli.activity_types import ToolCallBlock, merge_adjacent_tool_blocks


def _blk(tid: str, name: str, label: str, *, depth: int = 1, err: bool = False) -> ToolCallBlock:
    return ToolCallBlock(tid, name, label, is_error=err, finished=True, depth=depth)


def test_merge_adjacent_same_kind() -> None:
    blocks = [_blk("a", "read", "read(a.py)"), _blk("b", "read", "read(b.py)"), _blk("c", "read", "read(c.py)")]
    merged = merge_adjacent_tool_blocks(blocks)
    assert len(merged) == 1
    assert merged[0].label == "read ×3"
    assert merged[0].tool_call_id == "a"


def test_merge_does_not_join_different_kinds() -> None:
    blocks = [_blk("a", "read", "read(a.py)"), _blk("b", "shell", "shell(ls)"), _blk("c", "read", "read(c.py)")]
    merged = merge_adjacent_tool_blocks(blocks)
    assert len(merged) == 3


def test_merge_does_not_join_errors() -> None:
    blocks = [
        _blk("a", "read", "read(a.py)"),
        _blk("b", "read", "read(b.py)", err=True),
        _blk("c", "read", "read(c.py)"),
    ]
    merged = merge_adjacent_tool_blocks(blocks)
    assert len(merged) == 3


def test_merge_does_not_join_across_depth() -> None:
    blocks = [_blk("a", "read", "read(a.py)", depth=1), _blk("b", "read", "read(b.py)", depth=2)]
    merged = merge_adjacent_tool_blocks(blocks)
    assert len(merged) == 2


def test_merge_empty_and_single() -> None:
    assert merge_adjacent_tool_blocks([]) == []
    single = merge_adjacent_tool_blocks([_blk("a", "read", "read(a.py)")])
    assert len(single) == 1 and single[0].label == "read(a.py)"


def test_merge_preserves_others_in_order() -> None:
    blocks = [
        _blk("a", "read", "read(a.py)"),
        _blk("b", "read", "read(b.py)"),
        _blk("c", "shell", "shell(x)"),
        _blk("d", "grep", "grep(y)"),
        _blk("e", "grep", "grep(z)"),
    ]
    merged = merge_adjacent_tool_blocks(blocks)
    assert [b.label for b in merged] == ["read ×2", "shell(x)", "grep ×2"]
