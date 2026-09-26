"""Thinking mode: Kimi K3 classification, effort levels, and vendor kwargs."""

from __future__ import annotations

import pytest

from src.llm.thinking_mode import (
    classify_thinking_support,
    current_level,
    describe_thinking_status,
    reset_for_tests,
    run_thinking_command,
    set_cli_thinking_enabled,
    set_cli_thinking_level,
)

KIMI_BASE = "https://api.kimi.com/coding/v1"
MINIMAX_BASE = "https://api.minimaxi.com/v1"


@pytest.fixture(autouse=True)
def _reset_thinking_state():
    reset_for_tests()
    yield
    reset_for_tests()


def test_classify_and_levels() -> None:
    assert classify_thinking_support("k3", base_url=KIMI_BASE, driver="openai") == "kimi_k3"
    assert classify_thinking_support("k2.6", base_url=KIMI_BASE, driver="openai") == "none"
    assert classify_thinking_support("k3", base_url=MINIMAX_BASE, driver="responses") == "none"
    assert classify_thinking_support("MiniMax-M3", base_url=MINIMAX_BASE, driver="responses") == "minimax_m3"

    assert current_level() == "medium"
    set_cli_thinking_level("high")
    assert current_level() == "high"
    set_cli_thinking_level(None)
    assert current_level() == "medium"

    set_cli_thinking_level("low")
    enabled, _, note = describe_thinking_status("k3", base_url=KIMI_BASE, driver="openai")
    assert enabled is True and "档位：低" in note

    set_cli_thinking_enabled(False)
    enabled, _, note = describe_thinking_status("k3", base_url=KIMI_BASE, driver="openai")
    assert enabled is False and "不传档位" in note


def test_thinking_command_parsing() -> None:
    low = run_thinking_command("/thinking low", model="k3", base_url=KIMI_BASE, driver="openai")
    assert "开" in low.lines[0] and "档位：低" in low.lines[0]
    assert current_level() == "low"

    high = run_thinking_command("/thinking 高", model="k3", base_url=KIMI_BASE, driver="openai")
    assert "档位：高" in high.lines[0] and current_level() == "high"

    unsupported = run_thinking_command("/thinking low", model="MiniMax-M3", driver="responses", base_url=MINIMAX_BASE)
    assert "仅开关生效" in unsupported.lines[0] and current_level() == "low"

    usage = run_thinking_command("/thinking bogus", model="k3", base_url=KIMI_BASE, driver="openai")
    assert "low" in usage.lines[0] and "high" in usage.lines[0]


def test_kimi_openai_chat_kwargs() -> None:
    from src.llm.vendor_options import kimi_openai_chat_kwargs

    # k3 / kimi-for-coding：temperature 恒 1.0；k3 + 思考开 → extra_body 带 reasoning_effort
    assert kimi_openai_chat_kwargs("k3", level="low") == {
        "temperature": 1.0,
        "extra_body": {"reasoning_effort": "low"},
    }
    assert kimi_openai_chat_kwargs("k3", level="high") == {
        "temperature": 1.0,
        "extra_body": {"reasoning_effort": "max"},
    }
    assert kimi_openai_chat_kwargs("kimi-for-coding") == {"temperature": 1.0}
    # 开放平台其它模型：不强制 temperature
    assert kimi_openai_chat_kwargs("moonshot-v1-8k") == {}
    set_cli_thinking_enabled(False)
    assert kimi_openai_chat_kwargs("k3") == {"temperature": 1.0}


def test_minimax_responses_reasoning() -> None:
    from src.llm.vendor_options import minimax_responses_reasoning

    # M3 思考开 → effort=low（官方：minimal~high 均开启不调节深度）；关 → none
    assert minimax_responses_reasoning("MiniMax-M3", thinking_enabled=True) == {"effort": "low"}
    assert minimax_responses_reasoning("MiniMax-M3", thinking_enabled=False) == {"effort": "none"}
    # M2.x 推理不可关，一律不传
    assert minimax_responses_reasoning("MiniMax-M2.7", thinking_enabled=False) is None
