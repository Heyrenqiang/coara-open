"""CLI 基础 Markdown → Rich Text（含三类 OSC 8 链接）。

支持：加粗 / 斜体 / 行内代码 / 标题 / 无序列表 / 简易表格（无竖线）/
网页·文件·文件夹链接。
不做：表格框线、围栏语法高亮、图片、HTML、嵌套列表、任务列表。

快路径：看不到基础标记、或标记未产生任何效果时返回 None，调用方原样 print。
"""

from __future__ import annotations

import re
from pathlib import Path

from rich.style import Style
from rich.text import Text

_INLINE_RE = re.compile(
    r"`([^`]+)`"
    r"|\[([^\]]+)\]\(([^)]+)\)"
    r"|\*\*([^*]+)\*\*"
    r"|__([^_]+)__"
    r"|\*(?!\*)([^*]+)\*(?!\*)"
)

# 路径段：无空白/分隔符/Windows 非法字符；允许中文（与 Web pathDetect 对齐）
_SEG = r'[^\s\\/<>"|*?]+'
_WIN_ABS = re.compile(rf"^[A-Za-z]:[\\/]{_SEG}([\\/]{_SEG})*$")
_POSIX_ABS = re.compile(rf"^/{_SEG}([\\/]{_SEG})+$")
_HOME = re.compile(rf"^~[\\/]{_SEG}([\\/]{_SEG})*$")
_REL = re.compile(rf"^(\.{{1,2}}[\\/])?{_SEG}([\\/]{_SEG})+$")
_FILE_EXT = re.compile(r"\.[A-Za-z0-9]{1,12}$")

_HEADER_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_ULIST_RE = re.compile(r"^(\s*)([-*])\s+(.*)$")
# GFM 分隔行：| --- | :---: | --- |
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")


def _relative_path_plausible(s: str) -> bool:
    """相对路径防误判：须有文件后缀，或至少一段含 ASCII（排除「扇入/顺序」类）。"""
    body = re.sub(r"^\.{1,2}[\\/]", "", s)
    parts = [p for p in re.split(r"[\\/]", body) if p]
    if len(parts) < 2:
        return False
    last = parts[-1]
    if _FILE_EXT.search(last):
        return True
    return any(any(ch.isascii() and ch.isalnum() for ch in p) for p in parts)


def looks_like_fs_path(raw: str) -> bool:
    trimmed = (raw or "").strip()
    if not trimmed:
        return False
    s = trimmed.rstrip("/\\")
    if len(s) < 3 or len(s) > 240:
        return False
    if any(ch.isspace() for ch in s):
        return False
    if "://" in s:
        return False
    if _WIN_ABS.match(s) or _POSIX_ABS.match(s) or _HOME.match(s):
        return True
    return bool(_REL.match(s) and _relative_path_plausible(s))


def _open_target(href: str) -> str | None:
    target = (href or "").strip()
    if not target:
        return None
    lower = target.lower()
    if lower.startswith("http://") or lower.startswith("https://"):
        return target
    if not looks_like_fs_path(target):
        return None
    try:
        path = Path(target).expanduser()
        path = path.resolve() if not path.is_absolute() else path.resolve(strict=False)
    except OSError:
        try:
            path = Path(target).expanduser().absolute()
        except Exception:
            return None
    from src.cli.local_open import href_for_local_path

    return href_for_local_path(path)


def _parse_base_style(style: str) -> Style:
    try:
        return Style.parse(style) if style else Style()
    except Exception:
        return Style()


def _looks_like_basic_markdown(text: str) -> bool:
    if not text:
        return False
    if "**" in text or "__" in text or "`" in text:
        return True
    if "[" in text and "](" in text:
        return True
    if "*" in text:
        return True
    if text.startswith("#") or "\n#" in text:
        return True
    if text.startswith("- ") or "\n- " in text:
        return True
    # 简易表格：至少一行含两个 |
    return "|" in text and text.count("|") >= 2


def _cell_vis_len(cell: str) -> int:
    """估算单元格终端列宽（去行内标记；CJK 按 display_width，不用 len）。"""
    from src.cli.terminal_width import display_width

    s = cell
    s = re.sub(r"`([^`]+)`", r"\1", s)
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
    s = re.sub(r"__([^_]+)__", r"\1", s)
    s = re.sub(r"\*(?!\*)([^*]+)\*(?!\*)", r"\1", s)
    return max(display_width(s), 0)


def _is_table_sep(line: str) -> bool:
    return bool(_TABLE_SEP_RE.match(line.rstrip("\n")))


def _is_table_row(line: str) -> bool:
    body = line.rstrip("\n")
    if _is_table_sep(body):
        return True
    # 至少两个竖线，避免误伤普通句子里的单个 |
    return body.count("|") >= 2


def _parse_cells(line: str) -> list[str]:
    s = line.rstrip("\n").strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _append_table(out: Text, raw_lines: list[str], base: Style) -> None:
    """无竖线：列用空格对齐；表头加粗；分隔行丢弃。"""
    rows: list[list[str]] = []
    for line in raw_lines:
        if _is_table_sep(line):
            continue
        rows.append(_parse_cells(line))
    if not rows:
        return
    ncols = max(len(r) for r in rows)
    widths = [0] * ncols
    for r in rows:
        for i in range(ncols):
            cell = r[i] if i < len(r) else ""
            widths[i] = max(widths[i], _cell_vis_len(cell))

    for ri, r in enumerate(rows):
        cell_style = base + Style(bold=True) if ri == 0 else base
        for i in range(ncols):
            cell = r[i] if i < len(r) else ""
            _append_inline(out, cell, cell_style)
            pad = widths[i] - _cell_vis_len(cell)
            # 列间距 2 空格；末列不再追加尾随空白（省宽度）
            if i < ncols - 1:
                out.append(" " * (max(pad, 0) + 2), style=base)
        out.append("\n", style=base)


def _valid_table_block(lines: list[str]) -> bool:
    """表头 + 分隔行（可再跟数据行）。"""
    if len(lines) < 2:
        return False
    return _is_table_sep(lines[1]) and lines[0].count("|") >= 2


def split_commit_holding_open_table(text: str) -> tuple[str, str]:
    """把待提交文本拆成 (可立即写出, 暂扣)。

    流式按行 flush 时，若末尾仍是「可能继续长高的 GFM 表」，暂扣到出现
    非表行或回合结束再整表写出——否则每行单独进渲染器，对不齐也去不掉竖线。
    """
    if not text or "|" not in text:
        return text, ""
    lines = text.splitlines(keepends=True)
    if not lines:
        return text, ""

    ready: list[str] = []
    i = 0
    while i < len(lines):
        if not _is_table_row(lines[i]):
            ready.append(lines[i])
            i += 1
            continue
        j = i
        block: list[str] = []
        while j < len(lines) and _is_table_row(lines[j]):
            block.append(lines[j])
            j += 1
        # 块后面还有非表行：整块可交出去（是否合法表由渲染器决定）
        if j < len(lines):
            ready.extend(block)
            i = j
            continue
        # 块贴到文本末尾：未成形的表（缺分隔行）或已成形但仍可能追加数据行 → 暂扣
        if len(block) >= 2 and not _is_table_sep(block[1]):
            ready.extend(block)
            return "".join(ready), ""
        return "".join(ready), "".join(block)

    return "".join(ready), ""


def _append_inline(out: Text, text: str, base: Style) -> bool:
    """Return True if any markdown effect applied."""
    if not text:
        return False
    if "`" not in text and "[" not in text and "*" not in text and "__" not in text:
        out.append(text, style=base)
        return False
    pos = 0
    changed = False
    for m in _INLINE_RE.finditer(text):
        if m.start() > pos:
            out.append(text[pos : m.start()], style=base)
        if m.group(1) is not None:
            from src.cli.theme import get_theme

            out.append(m.group(1), style=base + Style(bold=True, color=get_theme().text.code))
            changed = True
        elif m.group(2) is not None:
            label, target = m.group(2), m.group(3)
            url = _open_target(target)
            if url is not None:
                # OSC 8 可点；常规超链接蓝 + 实线下划线（盖住终端 OSC 8 虚线观感）
                from src.cli.theme import get_theme

                link_color = get_theme().text.link
                out.append(label, style=base + Style(link=url, color=link_color, underline=True))
                changed = True
            else:
                out.append(m.group(0), style=base)
        elif m.group(4) is not None:
            out.append(m.group(4), style=base + Style(bold=True))
            changed = True
        elif m.group(5) is not None:
            out.append(m.group(5), style=base + Style(bold=True))
            changed = True
        elif m.group(6) is not None:
            out.append(m.group(6), style=base + Style(italic=True))
            changed = True
        pos = m.end()
    if pos < len(text):
        out.append(text[pos:], style=base)
    elif pos == 0:
        out.append(text, style=base)
    return changed


def _code_style(base: Style) -> Style:
    from src.cli.theme import get_theme

    return base + Style(color=get_theme().text.code)


def _append_line(out: Text, line: str, base: Style, *, in_fence: bool) -> tuple[bool, bool]:
    """Returns (in_fence, changed)."""
    newline = ""
    body = line
    if line.endswith("\n"):
        newline = "\n"
        body = line[:-1]
    stripped = body.lstrip()
    if stripped.startswith("```"):
        out.append(body, style=_code_style(base))
        if newline:
            out.append(newline, style=base)
        return (not in_fence), True
    if in_fence:
        out.append(body, style=_code_style(base))
        if newline:
            out.append(newline, style=base)
        return True, True

    hm = _HEADER_RE.match(body)
    if hm:
        level = len(hm.group(1))
        header_style = base + Style(bold=True, dim=level >= 3)
        _append_inline(out, hm.group(2), header_style)
        if newline:
            out.append(newline, style=base)
        return False, True

    lm = _ULIST_RE.match(body)
    if lm:
        indent, _bullet, rest = lm.group(1), lm.group(2), lm.group(3)
        out.append(f"{indent}• ", style=base)
        _append_inline(out, rest, base)
        if newline:
            out.append(newline, style=base)
        return False, True

    changed = _append_inline(out, body, base)
    if newline:
        out.append(newline, style=base)
    return False, changed


def try_rich_basic_markdown(text: str, *, style: str = "") -> Text | None:
    """基础 Markdown → Rich Text；无需渲染时返回 None。"""
    if not text or not _looks_like_basic_markdown(text):
        return None
    stripped = text.lstrip()
    if stripped.startswith("✓") or stripped.startswith("✗"):
        return None

    base = _parse_base_style(style)
    out = Text()
    in_fence = False
    any_changed = False
    lines = text.splitlines(keepends=True)
    if not lines:
        lines = [text]

    i = 0
    while i < len(lines):
        # 简易表格块：连续含 | 的行，且第 2 行是分隔行
        if not in_fence and _is_table_row(lines[i]):
            j = i
            block: list[str] = []
            while j < len(lines) and _is_table_row(lines[j]):
                block.append(lines[j])
                j += 1
            if _valid_table_block(block):
                _append_table(out, block, base)
                any_changed = True
                i = j
                continue
        in_fence, changed = _append_line(out, lines[i], base, in_fence=in_fence)
        any_changed = any_changed or changed
        i += 1

    if not any_changed:
        return None
    return out

