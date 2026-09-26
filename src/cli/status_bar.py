"""CLI 底栏：稳定的一行状态。

输入区与后台指示都不在这里（后台指示在 spinner 状态槽，见 spinner.py）。
"""

from __future__ import annotations

from prompt_toolkit.formatted_text import FormattedText

from src.cli.terminal_width import display_width, fit_three_column, format_cache_suffix, truncate_to_width

_CTX_WARN = 70.0
_CTX_FULL = 90.0


def format_status_metrics(
    *,
    used_tokens: int,
    ctx_window: int,
    cache_hit_ratio: float | None,
    session_cost: float | None = None,
    max_width: int | None = None,
    estimated: bool = False,
) -> tuple[str, str, float | None]:
    """上下文段与「缓存·费用」尾段拆开，窄宽度时先缩短上下文、再丢掉它、尾段尽量留下"""
    cache = format_cache_suffix(cache_hit_ratio)
    tail = (f" ·{cache}" if cache else "") + (f" · ¥{session_cost:.1f}" if session_cost is not None else "")
    if tail:
        tail += " "
    tilde = "~" if estimated else ""
    if ctx_window > 0:
        pct = (used_tokens / ctx_window) * 100
        used_k = used_tokens / 1000
        ctx_k = ctx_window / 1000
        ctx_opts = (
            f" {tilde}{used_k:.1f}k/{ctx_k:.1f}k",
            f" {tilde}{used_k:.1f}k",
        )
    else:
        pct = None
        ctx_opts = (
            f" {tilde}{used_tokens}tok",
            f" {tilde}{used_tokens / 1000:.1f}k",
        )

    if max_width is None or max_width <= 0:
        return ctx_opts[0], tail, pct

    for ctx in ctx_opts:
        if display_width(ctx + tail) <= max_width:
            return ctx, tail, pct
    if tail and display_width(tail) <= max_width:
        return "", tail, pct
    if tail:
        return "", truncate_to_width(tail, max_width), pct
    return truncate_to_width(ctx_opts[-1], max_width), "", pct


def _ctx_style(pct: float | None) -> str:
    if pct is None or pct < _CTX_WARN:
        return "class:toolbar"
    if pct < _CTX_FULL:
        return "class:toolbar.ctx-warn"
    return "class:toolbar.ctx-full"


def render_status_bar(
    *,
    columns: int,
    provider: str = "",
    model: str = "",
    workspace: str = "",
    has_key: bool = True,
    plan_mode: bool = False,
    used_tokens: int = 0,
    ctx_window: int = 0,
    cache_hit_ratio: float | None = None,
    session_cost: float | None = None,
    estimated: bool = False,
    show_metrics: bool = True,
) -> FormattedText:
    """底栏片段"""
    cols = max(1, int(columns))
    parts: list[tuple[str, str]] = []

    parts.append(("class:toolbar.separator", "─" * cols))
    parts.append(("", "\n"))

    model_name = ""
    if has_key:
        provider_s = provider.strip()
        model_s = model.strip()
        model_name = f"{provider_s}·{model_s}" if provider_s and model_s else provider_s or model_s

    hint = "" if has_key else " /model 配置provider"

    slot = model_name if model_name else hint.strip()

    left_plain = ""
    if plan_mode:
        left_plain = " 计划"
        if slot:
            left_plain += f" │ {slot}"
    elif slot:
        left_plain = f" {slot}"

    ctx_text = ""
    cache_text = ""
    pct: float | None = None
    if has_key and show_metrics:
        left_w = display_width(left_plain)
        budget = max(12, cols - left_w)
        ctx_text, cache_text, pct = format_status_metrics(
            used_tokens=used_tokens,
            ctx_window=ctx_window,
            cache_hit_ratio=cache_hit_ratio,
            session_cost=session_cost,
            max_width=budget,
            estimated=estimated,
        )

    right_plain = f"{ctx_text}{cache_text}"
    _, mid, right, left_pad, right_pad = fit_three_column(
        columns=cols,
        left=left_plain,
        mid=workspace,
        right=right_plain,
    )

    if plan_mode:
        parts.append(("class:toolbar.plan-mode", " 计划"))
        if slot:
            parts.append(("class:toolbar.separator", " │"))
            parts.append(("class:toolbar", f" {slot}"))
    elif slot:
        parts.append(("class:toolbar", f" {slot}"))

    if left_pad:
        parts.append(("class:toolbar.separator", " " * left_pad))
    if mid:
        parts.append(("class:toolbar", mid))
    if right_pad:
        parts.append(("class:toolbar.separator", " " * right_pad))
    if ctx_text:
        parts.append((_ctx_style(pct), ctx_text))
    if cache_text:
        parts.append(("class:toolbar", cache_text))

    return FormattedText(parts)
