"""Rich diff rendering for CLI scrollback (kimi-cli / qwen-code inspired)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import Enum, auto
from typing import Any

from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from src.coara.frontend import get_frontend
from src.coara.tool_output.syntax import highlight_code, highlight_lines
from src.coara.tool_output.types import DiffDisplayBlock


@dataclass(frozen=True, slots=True)
class _NeutralDiffColors:
    add_bg: str = ""
    del_bg: str = ""
    add_lineno: str = ""
    del_lineno: str = ""
    add_marker: str = ""
    del_marker: str = ""
    ctx_lineno: str = ""
    header_add: str = ""
    header_del: str = ""
    header_path: str = ""


_NEUTRAL_DIFF_COLORS = _NeutralDiffColors()


def _resolve_diff_colors() -> Any:
    """get_diff_colors 可能为 None（headless/未注册），回退中性配色。"""
    colors = get_frontend().get_diff_colors()
    return colors if colors is not None else _NEUTRAL_DIFF_COLORS


MAX_PREVIEW_CHANGED_LINES = 6
MAX_SCROLLBACK_DIFF_LINES = 15
# 后台工作空间滚动区 diff 的行数预算（前台用 MAX_SCROLLBACK_DIFF_LINES）
MAX_COMPACT_SCROLLBACK_DIFF_LINES = 6

# 行号列 + 符号列占用的固定列数（Table: num_width 右对齐 + 3 列符号）
_DIFF_GUTTER_COLS = 3
# Panel 边框 + padding 占用（render_diff_panel: border 2 + padding 2）
_DIFF_PANEL_EXTRA_COLS = 4
# 屏幕行截断的最小内容列宽（防极端窄终端除零/过窄）
_DIFF_MIN_CONTENT_COLS = 10

# 大 diff 语法高亮保护阈值：超过则跳过整块高亮（词法分析开销大，拖慢显示）
_DIFF_HIGHLIGHT_MAX_BYTES = 200_000
_DIFF_HIGHLIGHT_MAX_LINES = 5_000

# 惰性终端宽度探测：rich Console 构造轻量，模块级复用避免每帧重建
_console = Console()


def _diff_content_cols(num_width: int) -> int:
    """内容列宽估算：终端宽度 - 行号/符号 gutter - Panel 边框与 padding。"""
    return max(
        _DIFF_MIN_CONTENT_COLS,
        _console.width - num_width - _DIFF_GUTTER_COLS - _DIFF_PANEL_EXTRA_COLS,
    )


class DiffLineKind(Enum):
    CONTEXT = auto()
    ADD = auto()
    DELETE = auto()


@dataclass(slots=True)
class DiffLine:
    kind: DiffLineKind
    old_num: int
    new_num: int
    code: str


def _take_last_before(lines: list[DiffLine], n: int) -> list[DiffLine]:
    """从 lines 末尾往回取最多 n 个连续非空上下文行（遇空行即停），按原文顺序返回。"""
    res: list[DiffLine] = []
    i = len(lines) - 1
    while i >= 0 and len(res) < n:
        if not lines[i].code.strip():
            break
        res.append(lines[i])
        i -= 1
    res.reverse()
    return res


def _take_first_after(lines: list[DiffLine], n: int) -> list[DiffLine]:
    """从 lines 开头取最多 n 个连续非空上下文行（遇空行即停）。"""
    res: list[DiffLine] = []
    for line in lines:
        if len(res) >= n:
            break
        if not line.code.strip():
            break
        res.append(line)
    return res


def _equalize_outer_context(lines: list[DiffLine]) -> list[DiffLine]:
    """整块首尾上下文再对称：前导语境行数与尾随语境行数互取较小值，两侧同取
    （各保留贴变更块那侧）。多变更块同 hunk 时首块上方与末块下方由不同块各自
    决定，可能一大一小，这里收口为同一个数。"""
    lead = 0
    while lead < len(lines) and lines[lead].kind == DiffLineKind.CONTEXT:
        lead += 1
    trail = 0
    while trail < len(lines) - lead and lines[-1 - trail].kind == DiffLineKind.CONTEXT:
        trail += 1
    keep = min(lead, trail)
    if lead > keep:
        del lines[: lead - keep]
    if trail > keep:
        del lines[len(lines) - (trail - keep) :]
    return lines


def _trim_symmetric_context(hunk: list[DiffLine], n_context: int) -> list[DiffLine]:
    """使每个连续变更块（add/del 连续段）上下对称：取该块紧邻上下非空上下文行数较小值，
    两侧同取。变更块之间共享的上下文行不重复（各自取贴块那侧）。最后整块首尾的
    前导/尾随上下文行数再互取较小值，显示单元整体上下对称。"""
    if not hunk:
        return hunk
    segs: list[tuple[bool, list[DiffLine]]] = []
    cur_change: bool | None = None
    buf: list[DiffLine] = []
    for line in hunk:
        change = line.kind != DiffLineKind.CONTEXT
        if cur_change is None:
            cur_change = change
        if change != cur_change:
            segs.append((cur_change, buf))
            buf = []
            cur_change = change
        buf.append(line)
    if buf:
        segs.append((cur_change, buf))
    n = len(segs)
    change_pos = [i for i, (k, _) in enumerate(segs) if k]
    if not change_pos:
        return hunk

    keep_before: dict[int, list[DiffLine]] = {}
    keep_after: dict[int, list[DiffLine]] = {}
    for pos in change_pos:
        before_lines = segs[pos - 1][1] if pos > 0 else []
        after_lines = segs[pos + 1][1] if pos + 1 < n else []
        b = _take_last_before(before_lines, n_context)
        a = _take_first_after(after_lines, n_context)
        sym = min(len(b), len(a))
        keep_before[pos] = b[-sym:] if sym else []
        keep_after[pos] = a[:sym] if sym else []

    out: list[DiffLine] = []
    for i, (k, ls) in enumerate(segs):
        if k:
            # 变更块：去掉内部空行（与旧口径一致，空行不显示）
            out.extend(line for line in ls if line.code.strip())
            continue
        prev_change = max((p for p in change_pos if p < i), default=None)
        next_change = min((p for p in change_pos if p > i), default=None)
        part_a = keep_after.get(prev_change, []) if prev_change is not None else []
        part_b = keep_before.get(next_change, []) if next_change is not None else []
        # 同一上下文段被前后两个变更块复用：只保留不重叠的尾部
        if prev_change is not None and next_change is not None and len(part_a) + len(part_b) > len(ls):
            overlap_avail = max(0, len(ls) - len(part_a))
            part_b = part_b[-overlap_avail:] if overlap_avail else []
        out.extend(part_a)
        out.extend(part_b)
    return _equalize_outer_context(out)


def _resymmetrize_visible_diff(flat: list[DiffLine], *, n_context: int = 3) -> list[DiffLine]:
    """截断后对可见行再跑同一套上下对称裁剪。

    head+tail 截断可能吃掉某变更块上方语境、却留下下方语境，屏幕上不再对称。
    仅作用于截断后的短列表；不改截断前的 hunk 裁剪结果。
    """
    if not flat:
        return flat
    return _trim_symmetric_context(flat, n_context)


def _build_diff_lines(
    old_text: str,
    new_text: str,
    old_start: int,
    new_start: int,
    *,
    n_context: int = 3,
) -> list[list[DiffLine]]:
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    matcher = SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    hunks: list[list[DiffLine]] = []
    for group in matcher.get_grouped_opcodes(n=n_context * 2):
        hunk: list[DiffLine] = []
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                for k in range(i2 - i1):
                    hunk.append(
                        DiffLine(
                            DiffLineKind.CONTEXT,
                            old_start + i1 + k,
                            new_start + j1 + k,
                            old_lines[i1 + k],
                        )
                    )
            elif tag == "delete":
                for k in range(i2 - i1):
                    hunk.append(
                        DiffLine(
                            DiffLineKind.DELETE,
                            old_start + i1 + k,
                            0,
                            old_lines[i1 + k],
                        )
                    )
            elif tag == "insert":
                for k in range(j2 - j1):
                    hunk.append(
                        DiffLine(
                            DiffLineKind.ADD,
                            0,
                            new_start + j1 + k,
                            new_lines[j1 + k],
                        )
                    )
            elif tag == "replace":
                for k in range(i2 - i1):
                    hunk.append(
                        DiffLine(
                            DiffLineKind.DELETE,
                            old_start + i1 + k,
                            0,
                            old_lines[i1 + k],
                        )
                    )
                for k in range(j2 - j1):
                    hunk.append(
                        DiffLine(
                            DiffLineKind.ADD,
                            0,
                            new_start + j1 + k,
                            new_lines[j1 + k],
                        )
                    )
        if hunk:
            # 每个连续变更块自身上下对称：取该块紧邻的上下非空上下文行数较小值，两侧同取；
            # 块之间共享的上下文只留贴块那侧；整块首尾前导/尾随再互取小值。变更块内部空行不显示。
            hunk = _trim_symmetric_context(hunk, n_context)
        if hunk:
            hunks.append(hunk)
    return hunks


def collect_diff_hunks(blocks: list[DiffDisplayBlock]) -> tuple[list[list[DiffLine]], int, int]:
    all_hunks: list[list[DiffLine]] = []
    added = removed = 0
    for block in blocks:
        if block.is_summary:
            all_hunks.append(
                [
                    DiffLine(DiffLineKind.CONTEXT, 1, 1, block.old_text),
                    DiffLine(DiffLineKind.ADD, 0, 2, block.new_text),
                ]
            )
            continue
        block_hunks = _build_diff_lines(block.old_text, block.new_text, block.old_start, block.new_start)
        for hunk in block_hunks:
            for dl in hunk:
                if dl.kind == DiffLineKind.ADD:
                    added += 1
                elif dl.kind == DiffLineKind.DELETE:
                    removed += 1
            all_hunks.append(hunk)
    return all_hunks, added, removed


def flatten_diff_lines(
    hunks: list[list[DiffLine]],
    *,
    changed_only: bool = False,
    max_lines: int | None = None,
) -> tuple[list[DiffLine], int]:
    """Flatten hunks to display lines; skip empty placeholders; optionally cap length."""
    flat: list[DiffLine] = []
    for hunk in hunks:
        for dl in hunk:
            if dl.code == "" and dl.old_num == 0 and dl.new_num == 0:
                continue
            if changed_only and dl.kind == DiffLineKind.CONTEXT:
                continue
            flat.append(dl)
    remaining = 0
    if max_lines is not None and len(flat) > max_lines:
        remaining = len(flat) - max_lines
        flat = flat[:max_lines]
    return _drop_trailing_empty_display_lines(flat), remaining


def _drop_trailing_empty_display_lines(lines: list[DiffLine]) -> list[DiffLine]:
    """Trim EOF blank rows from diff previews (trailing-newline artifact, not mid-file blanks)."""
    out = list(lines)
    while out and out[-1].code == "":
        out.pop()
    return out


def _truncate_diff_lines_screen(
    flat: list[DiffLine],
    num_width: int,
    max_rows: int,
    *,
    content_cols: int | None = None,
) -> tuple[list[DiffLine], int]:
    """屏幕行感知截断：按视口行预算截断，防止单条超长行 wrap 后刷屏。

    每行先按内容列宽估算其占用的屏幕行数（display_width / content_cols
    向上取整，至少 1），逐行累计；预留给省略提示行。**保留头部与尾部**
    （尾部常含错误/关键结果），省略计数按逻辑行算，跨终端宽度稳定。

    ``content_cols`` 可显式指定（如 Matrix 端固定手机宽度）；缺省按
    CLI 终端宽度估算。

    快速路径：逻辑行数已不超过预算时直接返回（零开销）。
    """
    if max_rows <= 0:
        return [], len(flat)
    if len(flat) <= max_rows:
        return flat, 0
    content_cols = content_cols if content_cols and content_cols > 0 else _diff_content_cols(num_width)
    budget = max_rows - 1  # 预留省略提示行
    rows_of = [max(1, math.ceil(get_frontend().display_width(dl.code) / content_cols)) for dl in flat]
    if sum(rows_of) <= max_rows:
        return flat, 0

    head_budget = budget // 2
    tail_budget = budget - head_budget
    head: list[DiffLine] = []
    used = 0
    for i, rows in enumerate(rows_of):
        if used + rows > head_budget:
            break
        head.append(flat[i])
        used += rows
    # 极端超长行：单行就超过预算时至少保留第一行（宁超不空，保证有内容可看）
    if not head and flat:
        head.append(flat[0])
        used = rows_of[0]
    tail: list[DiffLine] = []
    used_t = 0
    for i in range(len(flat) - 1, len(head) - 1, -1):
        if used_t + rows_of[i] > tail_budget:
            break
        tail.append(flat[i])
        used_t += rows_of[i]
    tail.reverse()
    remaining = len(flat) - len(head) - len(tail)
    return head + tail, remaining


def _highlight_flat(flat: list[DiffLine], path: str) -> dict[int, Text] | None:
    """整块高亮 flat 全部行的 code（一次词法分析，parser 状态跨行保留）。

    行数无法对齐（个别 lexer 特性）或超过保护阈值时返回 ``None``，
    调用方回退逐行 ``highlight_code`` / 纯文本。超阈值时不返回、不做
    任何词法分析——大 diff 保护在进入前就短路。
    """
    if not flat:
        return None
    codes = [dl.code for dl in flat]
    if not any(codes):
        return None
    block = "\n".join(codes)
    if len(block) > _DIFF_HIGHLIGHT_MAX_BYTES or len(flat) > _DIFF_HIGHLIGHT_MAX_LINES:
        return None
    highlighted = highlight_lines(block, path)
    if highlighted is None or len(highlighted) != len(flat):
        return None
    return dict(enumerate(highlighted))


def diff_line_to_matrix_payload(dl: DiffLine) -> dict[str, Any]:
    if dl.kind == DiffLineKind.ADD:
        kind, num = "add", dl.new_num
    elif dl.kind == DiffLineKind.DELETE:
        kind, num = "del", dl.old_num
    else:
        kind, num = "ctx", dl.new_num
    return {"k": kind, "n": num, "t": dl.code}


def _coerce_diff_blocks(blocks: list[Any]) -> list[DiffDisplayBlock]:
    diff_blocks: list[DiffDisplayBlock] = []
    for item in blocks:
        if isinstance(item, DiffDisplayBlock):
            diff_blocks.append(item)
        elif isinstance(item, dict) and item.get("kind") == "diff":
            diff_blocks.append(DiffDisplayBlock.from_dict(item))
    return diff_blocks


def _build_diff_header(path: str, added: int, removed: int) -> Text:
    colors = _resolve_diff_colors()
    header = Text()
    header.append(path)
    if added > 0:
        header.append(f" +{added}", style=colors.header_add)
    if removed > 0:
        header.append(f" -{removed}", style=colors.header_del)
    return header


def _line_content(path: str, code: str, *, highlight: bool) -> Text:
    if highlight and code:
        return highlight_code(code, path)
    return Text(code)


def _render_diff_table(
    path: str,
    hunks: list[list[DiffLine]],
    *,
    highlight: bool = True,
    changed_only: bool = False,
    max_lines: int | None = None,
) -> tuple[Table, int]:
    colors = _resolve_diff_colors()
    flat, _ = flatten_diff_lines(hunks, changed_only=changed_only, max_lines=None)

    max_ln = max((max(dl.old_num, dl.new_num) for dl in flat if dl.code or dl.old_num or dl.new_num), default=0)
    num_width = max(len(str(max_ln)), 2)

    # 屏幕行感知截断（防长行刷屏）；省略计数按逻辑行
    remaining = 0
    if max_lines is not None and max_lines > 0:
        flat, remaining = _truncate_diff_lines_screen(flat, num_width, max_lines)
        # 仅在真正省略中间时再对称：未截断则保持截断前 hunk 裁剪原样
        if remaining > 0:
            flat = _resymmetrize_visible_diff(flat)

    # 整块高亮（一次词法分析）；行数不对齐或超阈值时回退逐行/纯文本
    block_highlight = _highlight_flat(flat, path) if highlight else None

    table = Table(show_header=False, box=None, padding=(0, 0), show_edge=False, expand=True)
    table.add_column(justify="right", width=num_width, no_wrap=True)
    table.add_column(width=3, no_wrap=True)
    table.add_column(ratio=1)

    for idx, dl in enumerate(flat):
        if block_highlight is not None:
            line_text = block_highlight.get(idx) or Text(dl.code)
        else:
            line_text = _line_content(path, dl.code, highlight=highlight)
        if dl.kind == DiffLineKind.ADD:
            table.add_row(
                Text(str(dl.new_num), style=colors.add_lineno),
                Text(" + ", style=colors.add_marker),
                line_text,
                style=colors.add_bg,
            )
        elif dl.kind == DiffLineKind.DELETE:
            table.add_row(
                Text(str(dl.old_num), style=colors.del_lineno),
                Text(" - ", style=colors.del_marker),
                line_text,
                style=colors.del_bg,
            )
        else:
            table.add_row(Text(str(dl.new_num), style=colors.ctx_lineno), Text("   "), line_text)

    return table, remaining


def render_diff_panel(
    path: str,
    hunks: list[list[DiffLine]],
    added: int,
    removed: int,
    *,
    title_prefix: str = "",
    head_style: str | None = None,
    compact: bool = False,
) -> RenderableType:
    """滚动区 diff：去框化 delta 风格——头部标签行 + 通栏底色行，无 Panel 线框无分隔线。

    ``title_prefix``/``head_style`` 用于后台工作空间（头部带 [空间] 前缀并染
    空间色）；``compact`` 收紧行数预算。通栏底色由 Table expand + 行样式承担
    （只作用于截断后的行）。
    """
    max_lines = MAX_COMPACT_SCROLLBACK_DIFF_LINES if compact else MAX_SCROLLBACK_DIFF_LINES
    table, remaining = _render_diff_table(
        path,
        hunks,
        highlight=True,
        changed_only=False,
        max_lines=max_lines,
    )
    colors = _resolve_diff_colors()
    header = Text()
    header.append(f"{title_prefix}{path}", style=head_style or colors.header_path)
    if added > 0:
        header.append(f" +{added}", style=colors.header_add)
    if removed > 0:
        header.append(f" -{removed}", style=colors.header_del)
    body: list[Any] = [header, table]
    if remaining > 0:
        body.append(Text(f"... {remaining} more lines", style="dim italic"))
    return Group(*body)


def render_diff_preview(path: str, hunks: list[list[DiffLine]], added: int, removed: int) -> RenderableType:
    """Compact changed-lines-only preview for approval modals."""
    header = _build_diff_header(path, added, removed)
    table, remaining = _render_diff_table(
        path,
        hunks,
        highlight=True,
        changed_only=True,
        max_lines=MAX_PREVIEW_CHANGED_LINES,
    )
    parts: list[Any] = [header, table]
    if remaining > 0:
        parts.append(Text(f"... {remaining} more changed lines", style="dim italic"))
    return Panel(Group(*parts), border_style="dim", padding=(0, 1))


def render_display_blocks(
    blocks: list[Any],
    *,
    preview: bool = False,
    title_prefix: str = "",
    head_style: str | None = None,
    compact: bool = False,
) -> RenderableType | None:
    """Render CLI display blocks; returns None when empty."""
    diff_blocks = _coerce_diff_blocks(blocks)
    if not diff_blocks:
        return None
    path = diff_blocks[0].path
    hunks, added, removed = collect_diff_hunks(diff_blocks)
    if not hunks:
        return None
    if preview:
        return render_diff_preview(path, hunks, added, removed)
    return render_diff_panel(
        path,
        hunks,
        added,
        removed,
        title_prefix=title_prefix,
        head_style=head_style,
        compact=compact,
    )
