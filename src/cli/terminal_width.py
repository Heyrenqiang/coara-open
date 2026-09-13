"""Terminal column-width helpers for CLI layout.

``len(text)`` is wrong for CJK / emoji (often 1 char = 2 columns). Use these
helpers wherever a string must fit a fixed terminal width, especially the
bottom toolbar — otherwise the right side (context / cache %) gets clipped.
"""

from __future__ import annotations

from prompt_toolkit.utils import get_cwidth


def display_width(text: str) -> int:
    """Return the number of terminal columns ``text`` occupies."""
    return sum(get_cwidth(ch) for ch in text)


def format_input_separator(columns: int, *, label: str = " input ") -> str:
    """Build the ``╌╌ input ╌╌`` chrome line for the prompt message.

    Full terminal width: right edge aligns with the bottom toolbar's
    separator line. Full width + explicit newline is safe — prompt_toolkit
    only wraps lines exceeding the width, and the toolbar rule has always
    used the same full-width pattern without ghost rows.
    """
    cols = max(2, int(columns))
    label_w = display_width(label)
    budget = max(label_w, cols)
    side = max(0, (budget - label_w) // 2)
    right = max(0, budget - side - label_w)
    return f"{'╌' * side}{label}{'╌' * right}"


def tail_to_width(text: str, max_width: int) -> str:
    """Keep the trailing ``max_width`` columns of ``text`` (live-stream tail).

    The streaming pending line is pinned to one terminal row: without this,
    a line growing past full width wraps to two rows, then collapses back to
    one when the next newline commits — the input area height oscillates at
    every long-line boundary. Streaming shows the newest text (right end);
    the full line is committed to scrollback above on newline, nothing lost.
    """
    if max_width <= 0:
        return ""
    if display_width(text) <= max_width:
        return text
    out: list[str] = []
    width = 0
    for ch in reversed(text):
        cw = get_cwidth(ch)
        if width + cw > max_width:
            break
        out.append(ch)
        width += cw
    return "".join(reversed(out))


def truncate_to_width(text: str, max_width: int, *, ellipsis: str = "…") -> str:
    """Fit ``text`` into ``max_width`` columns, appending ``ellipsis`` when truncated."""
    if max_width <= 0:
        return ""
    if display_width(text) <= max_width:
        return text
    ell_w = display_width(ellipsis)
    if max_width < ell_w:
        return ""
    if max_width == ell_w:
        return ellipsis
    out: list[str] = []
    width = 0
    for ch in text:
        cw = get_cwidth(ch)
        if width + cw + ell_w > max_width:
            break
        out.append(ch)
        width += cw
    return "".join(out) + ellipsis


def truncate_preserving_trailing_paren(text: str, max_width: int, *, ellipsis: str = "…") -> str:
    """Truncate like :func:`truncate_to_width`, but keep a trailing ``  (…)` stats suffix.

    Delegate live lines look like ``delegate coaras: long desc  (12s · 2.8k tok · cache 72%)``.
    Plain right-truncation would eat the paren first; preserve it and shrink the label.
    """
    if max_width <= 0:
        return ""
    if display_width(text) <= max_width:
        return text
    idx = text.rfind("  (")
    if idx <= 0 or not text.endswith(")"):
        return truncate_to_width(text, max_width, ellipsis=ellipsis)
    suffix = text[idx:]
    # Require a compact stats paren (elapsed / tok / cache), not an arbitrary trailing group.
    if "s" not in suffix and "tok" not in suffix and "cache" not in suffix:
        return truncate_to_width(text, max_width, ellipsis=ellipsis)
    suffix_w = display_width(suffix)
    if suffix_w >= max_width:
        # Extremely narrow: prefer the stats over the label.
        return truncate_to_width(suffix.lstrip(), max_width, ellipsis=ellipsis)
    head = text[:idx]
    return truncate_to_width(head, max_width - suffix_w, ellipsis=ellipsis) + suffix


def wrap_to_width(text: str, max_width: int) -> list[str]:
    """Split ``text`` into lines that each fit ``max_width`` columns (no ellipsis)."""
    if max_width <= 0:
        return [""] if text else []
    if not text:
        return [""]
    if display_width(text) <= max_width:
        return [text]
    lines: list[str] = []
    current: list[str] = []
    width = 0
    for ch in text:
        cw = get_cwidth(ch)
        if current and width + cw > max_width:
            lines.append("".join(current))
            current = [ch]
            width = cw
        else:
            current.append(ch)
            width += cw
    if current:
        lines.append("".join(current))
    return lines


def fit_three_column(
    *,
    columns: int,
    left: str,
    mid: str,
    right: str,
) -> tuple[str, str, str, int, int]:
    """Pack left / mid / right into ``columns`` with centered mid padding.

    Returns ``(left, mid, right, left_pad, right_pad)``.

    When space is tight, truncate ``mid`` first so ``right`` (context / cache)
    stays intact. Does not shorten ``left`` or ``right``; if they alone exceed
    ``columns``, ``mid`` is dropped and pads are 0.
    """
    if columns <= 0:
        return left, "", right, 0, 0

    left_w = display_width(left)
    right_w = display_width(right)
    budget = columns - left_w - right_w
    if budget <= 0:
        return left, "", right, 0, 0

    # Keep at least a little padding when mid is shown; mid may consume the rest.
    max_mid = max(0, budget - 2) if budget >= 2 else 0
    fitted_mid = mid
    if display_width(fitted_mid) > max_mid:
        fitted_mid = truncate_to_width(fitted_mid, max_mid)
    mid_w = display_width(fitted_mid)
    pad = max(0, budget - mid_w)
    left_pad = pad // 2
    right_pad = pad - left_pad
    return left, fitted_mid, right, left_pad, right_pad


def format_cache_suffix(hit_ratio: float | None) -> str:
    """`` cache 93%`` or empty. Ratio is 0..1 from provider usage."""
    if hit_ratio is None:
        return ""
    return f" cache {hit_ratio * 100:.0f}%"


def format_context_segment(
    *,
    used_tokens: int,
    ctx_window: int,
    cache_hit_ratio: float | None,
    max_width: int | None = None,
) -> str:
    """Right-side toolbar segment for context usage + optional cache hit rate.

    Candidates are tried from most detailed to most compact. Cache % is kept
    on every candidate so a narrow terminal never clips ``cache 93%`` down to
    ``cache 9``.
    """
    cache = format_cache_suffix(cache_hit_ratio)
    if ctx_window > 0:
        pct = (used_tokens / ctx_window) * 100
        used_k = used_tokens / 1000
        ctx_k = ctx_window / 1000
        candidates = (
            f" context: {pct:.1f}% ({used_k:.1f}k/{ctx_k:.1f}k){cache} ",
            f" context: {pct:.1f}% ({used_k:.1f}k){cache} ",
            f" ctx {pct:.1f}%{cache} ",
            f"{cache} " if cache else f" ctx {pct:.1f}% ",
        )
    else:
        candidates = (
            f" context: {used_tokens}tok{cache} ",
            f" {used_tokens}tok{cache} ",
            f"{cache} " if cache else f" {used_tokens}tok ",
        )

    if max_width is None or max_width <= 0:
        return candidates[0]

    chosen = candidates[0]
    for candidate in candidates:
        if display_width(candidate) <= max_width:
            chosen = candidate
            break
    else:
        chosen = truncate_to_width(candidates[-1], max_width)
    return chosen
