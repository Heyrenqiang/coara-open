"""用量展示单源格式化（src/runtime/usage_display.py）的口径契约。

Web / Android / devtools 只渲染这些函数的输出，任何一端想改样式只能改这里。
"""

from __future__ import annotations

from datetime import UTC, datetime

from src.runtime.usage_display import (
    cache_hit_level,
    cost_state,
    decorate_token_totals,
    format_day_display,
    format_hit_rate,
    format_token_count,
    format_ts_display,
    format_usage_money,
    hit_rate_pct,
)


def test_money_thresholds() -> None:
    assert format_usage_money(0) == "¥0"
    assert format_usage_money(-1) == "¥0"
    assert format_usage_money(123.4) == "¥123"
    assert format_usage_money(1234.56) == "¥1,235"
    assert format_usage_money(1.25) == "¥1.25"
    assert format_usage_money(99.99) == "¥99.99"
    assert format_usage_money(0.00123) == "¥0.0012"
    assert format_usage_money(0.00001) == "<¥0.0001"


def test_token_count_always_m_above_wan() -> None:
    assert format_token_count(0) == "0"
    assert format_token_count(9999) == "9,999"
    # 一万及以上一律折算 M，不再出现 k
    assert format_token_count(10000) == "0.01M"
    assert format_token_count(12345) == "0.01M"
    assert format_token_count(123456) == "0.12M"
    assert format_token_count(1234567) == "1.2M"
    assert format_token_count(5_000_000) == "5M"
    assert "k" not in format_token_count(999_999)


def test_hit_rate_display_and_pct() -> None:
    assert format_hit_rate(0.6479) == "64.8%"
    assert format_hit_rate(0.0) == "0.0%"
    assert format_hit_rate(1.0) == "100.0%"
    assert format_hit_rate(1.5) == "100.0%"  # 截断
    assert hit_rate_pct(0.6479) == 64.8
    assert hit_rate_pct(None) == 0.0


def test_cost_state() -> None:
    assert cost_state(1.0, 0) == "priced"
    assert cost_state(0.0, 100) == "unpriced"
    assert cost_state(0.0, 0) == "zero"


def test_cache_hit_level_threshold() -> None:
    assert cache_hit_level(0.5) == "good"
    assert cache_hit_level(0.49) == "normal"
    assert cache_hit_level(None) == "normal"


def test_ts_display_local_and_fallback() -> None:
    expected = datetime(2026, 8, 8, 2, 0, tzinfo=UTC).astimezone().strftime("%Y-%m-%d %H:%M")
    assert format_ts_display("2026-08-08T02:00:00Z") == expected
    assert format_ts_display("2026-08-08") == "2026-08-08 00:00"  # naive 按本地墙钟
    assert format_ts_display("garbage") == "garbage"
    assert format_ts_display("") == "—"
    assert format_ts_display(None) == "—"


def test_day_display() -> None:
    expected = datetime(2026, 8, 2, 2, 0, tzinfo=UTC).astimezone().strftime("%Y-%m-%d")
    assert format_day_display("2026-08-02T02:00:00Z") == expected
    assert format_day_display("") == ""


def test_decorate_token_totals_adds_display_fields() -> None:
    row = decorate_token_totals(
        {
            "llm_turns": 42,
            "input_tokens": 1234567,
            "output_tokens": 23456,
            "cache_read_tokens": 800000,
            "reasoning_tokens": 1200,
            "cache_hit_rate": 0.6479,
            "cost_miss": 1.5,
            "cost_hit": 0.25,
            "cost_out": 2.0,
            "cost_total": 3.75,
        }
    )
    assert row["input_display"] == "1.2M"
    assert row["output_display"] == "0.02M"
    assert row["cache_read_display"] == "0.8M"
    assert row["reasoning_display"] == "1,200"
    assert row["cost_total_display"] == "¥3.75"
    assert row["cost_hit_display"] == "¥0.2500"  # <1 四位小数
    assert row["cache_hit_display"] == "64.8%"
    assert row["cache_hit_pct"] == 64.8
    assert row["cache_hit_level"] == "good"
    assert row["cost_state"] == "priced"
    assert row["has_cost_breakdown"] is True


def test_decorate_token_totals_zero_row() -> None:
    row = decorate_token_totals({})
    assert row["input_display"] == "0"
    assert row["cost_total_display"] == "¥0"
    assert row["cost_state"] == "zero"
    assert row["cache_hit_display"] == "0.0%"
    assert row["has_cost_breakdown"] is False
