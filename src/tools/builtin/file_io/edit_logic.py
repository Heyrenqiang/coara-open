"""Shared edit/replace logic for execute, gate preview, and tests."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class EditSimulateResult:
    ok: bool
    new_content: str = ""
    matches: int = 0
    error: str = ""


def _normalize_unicode_punct(text: str) -> str:
    """Map common typographic punctuation/spaces to ASCII (Codex seek_sequence style)."""
    out: list[str] = []
    for ch in text:
        if ch in "\u2010\u2011\u2012\u2013\u2014\u2015\u2212":
            out.append("-")
        elif ch in "\u2018\u2019\u201a\u201b":
            out.append("'")
        elif ch in "\u201c\u201d\u201e\u201f":
            out.append('"')
        elif ch in "\u00a0\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000":
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def _line_match_key(line: str, level: str) -> str:
    """Normalize one line (no newline) for a fuzzy seek level."""
    if level == "rstrip":
        return line.rstrip()
    if level == "strip":
        return line.strip()
    if level == "unicode":
        return _normalize_unicode_punct(line.strip())
    return line


def _quote_variants(old_string: str) -> set[str]:
    return {
        old_string.translate(str.maketrans({"'": '"'})),  # type: ignore[arg-type]
        old_string.translate(str.maketrans({'"': "'"})),  # type: ignore[arg-type]
        old_string.translate(str.maketrans({'"': "“", "'": "’"})),  # type: ignore[arg-type]
        old_string.translate(str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"})),  # type: ignore[arg-type]
    }


def _reconstruct_window(
    content_lines: list[str],
    start: int,
    n_lines: int,
    *,
    old_string: str,
) -> str:
    """Build the exact file substring for a matched line window (preserve file whitespace)."""
    raw = [ln.rstrip("\r\n") for ln in content_lines]
    parts: list[str] = []
    want_trailing_nl = old_string.endswith("\n") or old_string.endswith("\r")
    for j in range(n_lines):
        body = raw[start + j]
        original = content_lines[start + j]
        ending = original[len(body) :]
        if j < n_lines - 1 or want_trailing_nl:
            parts.append(body + ending)
        else:
            parts.append(body)
    return "".join(parts)


def _find_unique_fuzzy_window(content: str, old_string: str, level: str) -> str | None:
    """Line-window seek under *level*; return the unique concrete file substring, else None.

    Multiple windows that normalize equally but differ in bytes → ambiguous → None.
    Multiple identical concrete substrings → return that substring (caller counts).
    """
    pattern_lines = old_string.splitlines()
    if not pattern_lines:
        return None

    content_lines = content.splitlines(keepends=True)
    if not content_lines and content:
        content_lines = [content]
    raw = [ln.rstrip("\r\n") for ln in content_lines]
    n = len(pattern_lines)
    if n > len(raw):
        return None

    pat_keys = [_line_match_key(p, level) for p in pattern_lines]
    found: list[str] = []
    for i in range(len(raw) - n + 1):
        if all(_line_match_key(raw[i + j], level) == pat_keys[j] for j in range(n)):
            found.append(_reconstruct_window(content_lines, i, n, old_string=old_string))

    if not found:
        return None
    unique = list(dict.fromkeys(found))
    if len(unique) == 1:
        return unique[0]
    return None


def find_actual_string(content: str, old_string: str) -> str | None:
    """Locate the concrete file substring to replace.

    Levels (stop at first unique hit):
    1. exact substring
    2. quote straight/curly variants
    3. ignore trailing whitespace per line
    4. ignore leading+trailing whitespace per line
    5. Unicode punctuation/space normalize (+ strip)

    Replacement always uses the returned *file* substring (not the model copy),
    so whitespace/punctuation in the file are preserved until new_string overwrites.
    """
    if old_string in content:
        return old_string

    for variant in _quote_variants(old_string):
        if variant and variant in content:
            return variant

    for level in ("rstrip", "strip", "unicode"):
        hit = _find_unique_fuzzy_window(content, old_string, level)
        if hit is not None:
            return hit

    return None


def _match_failure_context(content: str, old_string: str, *, max_lines: int = 40) -> str:
    """匹配失败时给 LLM 的自助上下文：定位疑似位置并返回其原文片段。

    定位策略（弱到强，任一命中即返回该处上下文）：
    1. old_string 首个非空行与原文某行高重合（该行 80% 字符出现在 old_string
       首行中，或反之）→ 视为疑似编辑点；
    2. 无命中时返回文件开头片段（帮助发现「文件已被改写/不是预期的文件」。

    返回带行号的原文片段（1-based，前缀「N|」），LLM 可直接据此重建
    old_string 重试，省一次 read 往返。
    """
    lines = content.splitlines()
    first = next((ln.strip() for ln in old_string.splitlines() if ln.strip()), "")

    def _line_overlap(line: str) -> bool:
        if not first:
            return False
        stripped = line.strip()
        if not stripped:
            return False
        shorter, longer = (stripped, first) if len(stripped) <= len(first) else (first, stripped)
        hits = sum(1 for ch in shorter if ch in longer)
        return hits >= max(1, int(len(shorter) * 0.8))

    start_idx = next((i for i, line in enumerate(lines) if _line_overlap(line)), 0)
    lo = max(0, start_idx - 3)
    hi = min(len(lines), lo + max_lines)
    numbered = "\n".join(f"{i + 1}|{lines[i]}" for i in range(lo, hi))
    pos_note = f"第 {start_idx + 1} 行附近" if start_idx < len(lines) else "文件开头"
    return f"下面是文件中{pos_note}的原文\n{numbered}"


def _multi_match_context(content: str, needle: str, *, max_matches: int = 5, context_lines: int = 3) -> str:
    """多处匹配时给 LLM 的自助定位：每处匹配的行号 + 上下文片段。

    与 _match_failure_context 同一哲学（省一次 read 往返）：LLM 直接据片段
    把目标处前后行并入 old_string，下一次调用一次改中，不反复试错。
    """
    import bisect

    lines = content.splitlines(keepends=True)
    line_starts: list[int] = []
    off = 0
    for ln in lines:
        line_starts.append(off)
        off += len(ln)

    total = content.count(needle)
    parts: list[str] = []
    start = 0
    shown = 0
    while shown < max_matches:
        idx = content.find(needle, start)
        if idx < 0:
            break
        line_no = bisect.bisect_right(line_starts, idx)  # 1-based
        lo = max(0, line_no - 1 - context_lines)
        hi = min(len(lines), line_no + context_lines)
        snippet = "".join(f"{i + 1}|{lines[i]}" for i in range(lo, hi))
        parts.append(f"匹配于第 {line_no} 行附近：\n{snippet}")
        shown += 1
        start = idx + len(needle)
    more = f"\n（另有 {total - shown} 处未示）" if total > shown else ""
    return "\n".join(parts) + more


def simulate_edit_replacement(
    content: str,
    old_string: str,
    new_string: str,
    *,
    replace_all: bool = False,
) -> EditSimulateResult:
    """Compute post-edit content without writing disk."""
    if old_string == new_string:
        return EditSimulateResult(ok=False, error="old_string 与 new_string 相同")

    actual_old = find_actual_string(content, old_string)
    if actual_old is None:
        return EditSimulateResult(ok=False, error="未找到匹配文本")

    matches = content.count(actual_old)

    if matches > 1 and not replace_all:
        detail = _multi_match_context(content, actual_old)
        return EditSimulateResult(
            ok=False,
            error=f"找到 {matches} 处匹配（非唯一），各匹配位置：\n{detail}",
        )

    new_content = content.replace(actual_old, new_string) if replace_all else content.replace(actual_old, new_string, 1)

    return EditSimulateResult(ok=True, new_content=new_content, matches=matches)
