"""Diff 渲染层快照/行为测试（截断、整块高亮、大 diff 保护）。"""

from __future__ import annotations

from src.coara.diff_render import (
    _DIFF_HIGHLIGHT_MAX_BYTES,
    _DIFF_HIGHLIGHT_MAX_LINES,
    DiffLine,
    DiffLineKind,
    _highlight_flat,
    _truncate_diff_lines_screen,
    collect_diff_hunks,
    flatten_diff_lines,
)
from src.coara.tool_output.diff import _build_diff_blocks_sync
from src.coara.tool_output.syntax import highlight_lines


def _dl(code: str, num: int = 1, kind: DiffLineKind = DiffLineKind.ADD) -> DiffLine:
    return DiffLine(kind, num, num, code)


def _short(code: str) -> DiffLine:
    return _dl(code)


def _flatten_diff_rows(old: str, new: str) -> list[tuple[int, str, str]]:
    """按渲染管线（block 分组 → hunk 对称裁剪 → flatten）展开为 (行号, 符号, 文本) 行。"""
    blocks = _build_diff_blocks_sync("f.txt", old, new)
    hunks, _, _ = collect_diff_hunks(blocks)
    flat, _ = flatten_diff_lines(hunks)
    rows: list[tuple[int, str, str]] = []
    for dl in flat:
        sign = "+" if dl.kind == DiffLineKind.ADD else ("-" if dl.kind == DiffLineKind.DELETE else " ")
        num = dl.old_num if dl.kind == DiffLineKind.DELETE else dl.new_num
        rows.append((num, sign, dl.code))
    return rows


def test_truncate_fastpath_untouched() -> None:
    flat = [_short(f"line {i}") for i in range(4)]
    out, remaining = _truncate_diff_lines_screen(flat, num_width=2, max_rows=6)
    assert out == flat and remaining == 0


def test_truncate_keeps_head_and_tail() -> None:
    # 20 行，预算 8：应保留头部与尾部，中间省略（错误/结果常在尾部）
    flat = [_short(f"line {i:02d}") for i in range(20)]
    out, remaining = _truncate_diff_lines_screen(flat, num_width=2, max_rows=8)
    assert remaining > 0
    assert len(out) < len(flat)
    assert out[0].code == "line 00", "头部应从第一行开始"
    assert out[-1].code == "line 19", "尾部应保留最后一行"
    assert remaining == len(flat) - len(out)


def test_truncate_long_line_capped_by_screen_rows() -> None:
    # 单条超长行占多屏幕行：预算内最多容纳几行，防止刷屏
    long_url = "https://example.com/" + "x" * 400
    flat = [_dl(long_url, num=i + 1) for i in range(10)]
    out, remaining = _truncate_diff_lines_screen(flat, num_width=3, max_rows=6)
    assert remaining > 0
    assert len(out) < len(flat)
    assert out[0].code == long_url


def test_truncate_zero_rows() -> None:
    flat = [_short("a"), _short("b")]
    out, remaining = _truncate_diff_lines_screen(flat, num_width=2, max_rows=0)
    assert out == [] and remaining == 2


def test_trim_context_outer_edges_take_min() -> None:
    # 两个变更块同 hunk：首块上方仅 1 行可用、末块下方 3 行可用 → 首尾互取小值，各留 1 行
    old = "\n".join(["a", "X", "c", "d", "e", "f", "g", "h", "Y", "i", "j", "k"])
    new = old.replace("X", "X2").replace("Y", "Y2")
    rows = _flatten_diff_rows(old, new)
    assert rows[0] == (1, " ", "a"), "首行只留 1 行前导语境"
    assert rows[-1] == (10, " ", "i"), "末行只留 1 行尾随语境（11/12 行不显示）"
    assert (11, " ", "j") not in rows
    assert (12, " ", "k") not in rows


def test_trim_context_outer_edges_take_min_reversed() -> None:
    # 反向：首块上方 3 行可用、末块下方仅 1 行 → 首尾各留 1 行（保留贴变更块那侧）
    old = "\n".join(["a", "b", "c", "X", "e", "f", "g", "h", "i", "Y", "k"])
    new = old.replace("X", "X2").replace("Y", "Y2")
    rows = _flatten_diff_rows(old, new)
    assert rows[0] == (3, " ", "c"), "前导语境裁到贴变更块那 1 行"
    assert rows[-1] == (11, " ", "k")
    assert (1, " ", "a") not in rows
    assert (2, " ", "b") not in rows


def test_trim_context_single_block_unchanged() -> None:
    # 单变更块：逐块对称已收口（上可用 3、下可用 1 → 各 1 行），首尾收口不再变动
    old = "\n".join(f"line{i}" for i in range(1, 11))
    new = old.replace("line9", "CHANGED9")
    rows = _flatten_diff_rows(old, new)
    assert rows == [
        (8, " ", "line8"),
        (9, "-", "line9"),
        (9, "+", "CHANGED9"),
        (10, " ", "line10"),
    ]


def test_resymmetrize_after_truncate_drops_orphan_trailing_context() -> None:
    """head+tail 截断吃掉变更块上方语境后，可见尾随语境也应被互取小值裁掉。"""
    from src.coara.diff_render import _resymmetrize_visible_diff

    # 模拟截断后画面：变更块直接贴在列表开头（上方语境已进 … more），下方仍有 3 行语境
    flat = [
        DiffLine(DiffLineKind.DELETE, 10, 0, "old-a"),
        DiffLine(DiffLineKind.DELETE, 11, 0, "old-b"),
        DiffLine(DiffLineKind.CONTEXT, 12, 12, "finally:"),
        DiffLine(DiffLineKind.CONTEXT, 13, 13, "    if x:"),
        DiffLine(DiffLineKind.CONTEXT, 14, 14, "        return"),
    ]
    out = _resymmetrize_visible_diff(flat)
    assert [dl.code for dl in out] == ["old-a", "old-b"]
    assert all(dl.kind == DiffLineKind.DELETE for dl in out)


def test_resymmetrize_preserves_symmetric_context() -> None:
    """上下语境都在可见区时，截断后再对称不该多裁。"""
    from src.coara.diff_render import _resymmetrize_visible_diff

    flat = [
        DiffLine(DiffLineKind.CONTEXT, 8, 8, "before"),
        DiffLine(DiffLineKind.DELETE, 9, 0, "gone"),
        DiffLine(DiffLineKind.ADD, 0, 9, "here"),
        DiffLine(DiffLineKind.CONTEXT, 10, 10, "after"),
    ]
    out = _resymmetrize_visible_diff(flat)
    assert [dl.code for dl in out] == ["before", "gone", "here", "after"]


def test_pipeline_truncate_then_resym_hides_trailing_when_lead_omitted() -> None:
    """head+tail 拼出「变更块贴头 + 尾随语境」时，再对称应裁掉尾随语境。"""
    from src.coara.diff_render import (
        _resymmetrize_visible_diff,
        _truncate_diff_lines_screen,
    )

    # 前部大段变更、尾部变更+finally：截断后中间语境消失，尾随 finally 成孤儿
    flat: list[DiffLine] = [
        *[DiffLine(DiffLineKind.ADD, 0, i + 1, f"head{i}") for i in range(12)],
        *[DiffLine(DiffLineKind.CONTEXT, 100 + i, 100 + i, f"mid{i}") for i in range(30)],
        DiffLine(DiffLineKind.DELETE, 200, 0, "old-tail"),
        DiffLine(DiffLineKind.ADD, 0, 200, "new-tail"),
        DiffLine(DiffLineKind.CONTEXT, 201, 201, "finally:"),
        DiffLine(DiffLineKind.CONTEXT, 202, 202, "    if t:"),
        DiffLine(DiffLineKind.CONTEXT, 203, 203, "        del t"),
    ]
    trunc, remaining = _truncate_diff_lines_screen(flat, num_width=3, max_rows=10)
    assert remaining > 0
    assert any(dl.code == "finally:" for dl in trunc), "截断后仍可能带着尾随语境"
    # 拼后若 mid 语境不在可见区，tail 变更块上方为空 → 再对称去掉 finally
    out = _resymmetrize_visible_diff(trunc)
    assert "finally:" not in [dl.code for dl in out]
    assert any(dl.code in {"old-tail", "new-tail", "head0"} for dl in out)


def test_highlight_lines_preserves_cross_line_state() -> None:
    # 多行字符串跨行：整块高亮保留 parser 状态（逐行高亮会丢）
    text = 'def f():\n    s = """\n    multi\n    line\n    """\n    return s\n'
    lines = highlight_lines(text, "a.py")
    assert lines is not None and len(lines) == len(text.splitlines())
    # 三引号字符串段应全部落在 String 绿色样式中
    joined = "".join(line.plain for line in lines)
    assert '"""' in joined


def test_highlight_lines_alignment_returns_none_on_empty() -> None:
    assert highlight_lines("", "a.py") == []


def test_highlight_flat_block() -> None:
    flat = [_short("def f():"), _short("    return 1")]
    block = _highlight_flat(flat, "a.py")
    assert block is not None and len(block) == len(flat)
    assert block[0].plain == "def f():"


def test_highlight_flat_large_diff_short_circuits() -> None:
    # 超阈值：不做词法分析，返回 None（大 diff 保护）
    flat = [_short("x" * 1000) for _ in range(min(_DIFF_HIGHLIGHT_MAX_LINES, 100))]
    flat = [_dl("y" * (_DIFF_HIGHLIGHT_MAX_BYTES // len(flat) + 1), num=i + 1) for i in range(len(flat))]
    assert _highlight_flat(flat, "a.py") is None


def test_highlight_flat_over_line_count_short_circuits() -> None:
    flat = [_short("x") for _ in range(_DIFF_HIGHLIGHT_MAX_LINES + 1)]
    assert _highlight_flat(flat, "a.py") is None
