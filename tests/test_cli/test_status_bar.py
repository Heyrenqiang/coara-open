"""CLI status bar layout."""

from __future__ import annotations

from src.cli.status_bar import render_status_bar
from src.cli.terminal_width import display_width


def _text(bar) -> str:
    return "".join(piece for _, piece in bar)


def _style_of(bar, needle: str) -> str:
    for style, piece in bar:
        if needle in piece:
            return style
    raise AssertionError(f"{needle!r} not in {list(bar)}")


def test_plan_badge_is_separate_from_model() -> None:
    bar = render_status_bar(
        columns=100,
        provider="deepseek",
        model="deepseek-flash",
        workspace=" v8 ",
        plan_mode=True,
        used_tokens=1000,
        ctx_window=100_000,
    )
    text = _text(bar)
    assert "计划模式" not in text
    assert "☰" not in text
    assert _style_of(bar, "计划") == "class:toolbar.plan-mode"
    assert "deepseek·deepseek-flash" in text
    assert "计划" not in next(piece for style, piece in bar if "deepseek" in piece)


def test_no_key_hint_sits_on_the_left_like_a_model() -> None:
    bar = render_status_bar(
        columns=80,
        provider="deepseek",
        model="deepseek-flash",
        workspace=" v8 ",
        has_key=False,
        plan_mode=True,
    )
    text = _text(bar)
    last_line = text.split("\n")[-1]
    assert "/model 配置provider" in last_line
    assert last_line.index("/model 配置provider") < last_line.index(" v8 ")
    assert "deepseek" not in text
    assert "cache" not in text
    assert "工作中" not in text
    assert _style_of(bar, "/model") == "class:toolbar"


def test_context_color_only_when_nearly_full() -> None:
    calm = render_status_bar(
        columns=120,
        provider="glm",
        model="glm-5.3",
        used_tokens=10_000,
        ctx_window=100_000,
        cache_hit_ratio=0.4,
    )
    warn = render_status_bar(
        columns=120,
        provider="glm",
        model="glm-5.3",
        used_tokens=75_000,
        ctx_window=100_000,
        cache_hit_ratio=0.4,
    )
    full = render_status_bar(
        columns=120,
        provider="glm",
        model="glm-5.3",
        used_tokens=95_000,
        ctx_window=100_000,
        cache_hit_ratio=0.4,
    )
    assert _style_of(calm, "10.0k") == "class:toolbar"
    assert _style_of(warn, "75.0k") == "class:toolbar.ctx-warn"
    assert _style_of(full, "95.0k") == "class:toolbar.ctx-full"
    assert _style_of(full, "cache") == "class:toolbar"


def test_narrow_width_keeps_cache_and_drops_workspace_detail() -> None:
    left = " deepseek·deepseek-flash"
    right_min = " cache 93% "
    columns = display_width(left) + display_width(right_min) + 4
    bar = render_status_bar(
        columns=columns,
        provider="deepseek",
        model="deepseek-flash",
        workspace=" coara-app-实验版 ",
        used_tokens=28_200,
        ctx_window=1_048_600,
        cache_hit_ratio=0.93,
    )
    text = _text(bar)
    assert "cache 93%" in text
    assert "实验版" not in text or display_width(text.split("\n")[-1]) <= columns


def test_attach_bar_matches_grammar_without_busy_flag() -> None:
    bar = render_status_bar(
        columns=80,
        provider="minimax",
        model="MiniMax-M3",
        workspace=" v8 ",
        show_metrics=False,
    )
    text = _text(bar)
    assert "minimax·MiniMax-M3" in text
    assert " v8 " in text
    assert "工作中" not in text
    assert "cache" not in text


def test_metrics_show_used_over_window_cache_and_cost() -> None:
    bar = render_status_bar(
        columns=120,
        provider="deepseek",
        model="deepseek-flash",
        workspace=" v8 ",
        used_tokens=28_200,
        ctx_window=1_048_600,
        cache_hit_ratio=0.93,
        session_cost=0.34,
    )
    text = _text(bar)
    assert "28.2k/1048.6k" in text
    assert "cache 93%" in text
    assert "¥0.3" in text
    assert "%" not in text.split("cache")[0]  # 不显示占比


def test_metrics_without_window_show_used_tokens() -> None:
    bar = render_status_bar(
        columns=120,
        provider="deepseek",
        model="deepseek-flash",
        used_tokens=15300,
        session_cost=1.2,
    )
    text = _text(bar)
    assert "15300tok" in text
    assert "¥1.2" in text


def test_narrow_width_prefers_cache_and_cost_tail() -> None:
    left = " deepseek·deepseek-flash"
    right_min = " · cache 93% · ¥0.3 "
    columns = display_width(left) + display_width(right_min) + 2
    bar = render_status_bar(
        columns=columns,
        provider="deepseek",
        model="deepseek-flash",
        used_tokens=28_200,
        ctx_window=1_048_600,
        cache_hit_ratio=0.93,
        session_cost=0.34,
    )
    text = _text(bar)
    assert "cache 93%" in text
    assert "¥0.3" in text
    assert display_width(text.split("\n")[-1]) <= columns
