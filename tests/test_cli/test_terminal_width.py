"""Terminal display-width and bottom-toolbar layout."""

from __future__ import annotations

from src.cli.terminal_width import (
    display_width,
    fit_three_column,
    format_cache_suffix,
    format_context_segment,
    format_input_separator,
    truncate_to_width,
    wrap_to_width,
)
from src.llm.usage import prompt_cache_hit_ratio, total_prompt_tokens


def test_display_width_counts_cjk_as_two() -> None:
    assert display_width("a") == 1
    assert display_width("实") == 2
    assert display_width(" coara-app-实验版 ") == 18
    assert len(" coara-app-实验版 ") == 15


def test_format_input_separator_full_width_aligns_with_toolbar() -> None:
    for cols in (40, 80, 120):
        border = format_input_separator(cols)
        assert "input" in border
        assert display_width(border) == cols


def test_tail_to_width_pins_streaming_line_to_one_row() -> None:
    from src.cli.terminal_width import tail_to_width

    short = "短行"
    assert tail_to_width(short, 10) == short
    assert tail_to_width("", 10) == ""
    assert tail_to_width("abc", 0) == ""

    # 超宽：保留最新尾部（流式看右端），宽度钉死一行
    long_text = "0123456789ABCDEFGHIJ" * 3  # 60 cols
    out = tail_to_width(long_text, 20)
    assert display_width(out) == 20
    assert out == long_text[-20:]

    # CJK 整字截断：不劈半字
    cjk = "中文混排abcdefghij" * 4
    out = tail_to_width(cjk, 11)
    assert display_width(out) <= 11
    assert display_width(out) >= 9  # 半个 CJK 不进（2 列放不下）


def test_truncate_to_width_respects_columns() -> None:
    text = " coara-app-实验版 "
    out = truncate_to_width(text, 10)
    assert display_width(out) <= 10
    assert out.endswith("…")


def test_wrap_to_width_keeps_full_text() -> None:
    text = "你好世界ABCD"
    parts = wrap_to_width(text, 4)
    assert "".join(parts) == text
    assert all(display_width(p) <= 4 for p in parts)


def test_fit_three_column_truncates_mid_before_right() -> None:
    left = " kimi·k3 "
    mid = " coara-app-实验版 "
    right = " context: 2.7% (28.2k/1048.6k) cache 93% "
    # Force a narrow terminal: left+right leave little room for mid.
    columns = display_width(left) + display_width(right) + 8
    _left, fitted_mid, fitted_right, left_pad, right_pad = fit_three_column(
        columns=columns,
        left=left,
        mid=mid,
        right=right,
    )
    assert fitted_right == right
    assert "cache 93%" in fitted_right
    assert display_width(fitted_mid) <= 6  # budget - 2 pads
    total = display_width(left) + left_pad + display_width(fitted_mid) + right_pad + display_width(fitted_right)
    assert total == columns


def test_format_context_keeps_cache_when_compacting() -> None:
    full = format_context_segment(
        used_tokens=28200,
        ctx_window=1_048_576,
        cache_hit_ratio=0.931,
    )
    assert "cache 93%" in full
    assert "1048.6k" in full

    compact = format_context_segment(
        used_tokens=28200,
        ctx_window=1_048_576,
        cache_hit_ratio=0.931,
        max_width=22,
    )
    assert "cache 93%" in compact
    assert display_width(compact) <= 22


def test_format_cache_suffix() -> None:
    assert format_cache_suffix(None) == ""
    assert format_cache_suffix(0.931) == " cache 93%"


def test_truncate_preserving_trailing_paren_keeps_delegate_stats() -> None:
    from src.cli.terminal_width import truncate_preserving_trailing_paren

    line = "◌ delegate explore: 核验多工作空间并发改造以及很长很长的描述  (2m 17s · 2.8k tok · cache 72%)"
    fitted = truncate_preserving_trailing_paren(line, 48)
    assert fitted.endswith("(2m 17s · 2.8k tok · cache 72%)")
    assert "cache 72%" in fitted


def test_kimi_log_cache_hit_formats_as_93_percent() -> None:
    """Values from android-app llm-request-latest.json usage block."""
    usage = {
        "input_tokens": 2053,
        "output_tokens": 814,
        "cache_read_input_tokens": 27904,
    }
    assert total_prompt_tokens(usage) == 2053 + 27904
    ratio = prompt_cache_hit_ratio(usage)
    assert ratio is not None
    assert format_cache_suffix(ratio) == " cache 93%"
