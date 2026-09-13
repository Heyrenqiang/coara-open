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
from src.llm.vendor_options import kimi_messages_kwargs

KIMI_BASE = "https://api.kimi.com/coding"


@pytest.fixture(autouse=True)
def _reset_thinking_state():
    reset_for_tests()
    yield
    reset_for_tests()


def test_classify_and_levels() -> None:
    assert classify_thinking_support("k3", base_url=KIMI_BASE, driver="anthropic") == "kimi_k3"
    assert classify_thinking_support("k2.6", base_url=KIMI_BASE, driver="anthropic") == "none"
    assert classify_thinking_support("k3", base_url="https://api.minimaxi.com/anthropic", driver="anthropic") == "none"

    assert current_level() == "medium"
    set_cli_thinking_level("high")
    assert current_level() == "high"
    set_cli_thinking_level(None)
    assert current_level() == "medium"

    set_cli_thinking_level("low")
    enabled, _, note = describe_thinking_status("k3", base_url=KIMI_BASE, driver="anthropic")
    assert enabled is True and "档位：低" in note

    set_cli_thinking_enabled(False)
    enabled, _, note = describe_thinking_status("k3", base_url=KIMI_BASE, driver="anthropic")
    assert enabled is False and "K2.6" in note


def test_thinking_command_parsing() -> None:
    low = run_thinking_command("/thinking low", model="k3", base_url=KIMI_BASE, driver="anthropic")
    assert "开" in low.lines[0] and "档位：低" in low.lines[0]
    assert current_level() == "low"

    high = run_thinking_command("/thinking 高", model="k3", base_url=KIMI_BASE, driver="anthropic")
    assert "档位：高" in high.lines[0] and current_level() == "high"

    unsupported = run_thinking_command("/thinking low", model="MiniMax-M3", driver="anthropic", base_url="")
    assert "仅开关生效" in unsupported.lines[0] and current_level() == "low"

    usage = run_thinking_command("/thinking bogus", model="k3", base_url=KIMI_BASE, driver="anthropic")
    assert "low" in usage.lines[0] and "high" in usage.lines[0]


def test_kimi_vendor_kwargs() -> None:
    assert kimi_messages_kwargs("k3", thinking_enabled=True, level="low") == {"extra_body": {"reasoning_effort": "low"}}
    assert kimi_messages_kwargs("k3", thinking_enabled=True, level="medium") == {
        "extra_body": {"reasoning_effort": "high"}
    }
    assert kimi_messages_kwargs("k3", thinking_enabled=True, level="high") == {
        "extra_body": {"reasoning_effort": "max"}
    }
    assert kimi_messages_kwargs("k3-256k", thinking_enabled=True, level="medium") == {
        "extra_body": {"reasoning_effort": "high"}
    }
    assert kimi_messages_kwargs("k3", thinking_enabled=False) == {"thinking": {"type": "disabled"}}
    assert kimi_messages_kwargs("k2.6", thinking_enabled=True) == {}

    set_cli_thinking_level("high")
    assert kimi_messages_kwargs("k3") == {"extra_body": {"reasoning_effort": "max"}}


def test_kimi_openai_chat_kwargs() -> None:
    from src.llm.vendor_options import kimi_openai_chat_kwargs

    # temperature 恒 1.0（端点拒绝其它值）；k3 + 思考开 → extra_body 带 reasoning_effort
    assert kimi_openai_chat_kwargs("k3", level="low") == {
        "temperature": 1.0,
        "extra_body": {"reasoning_effort": "low"},
    }
    assert kimi_openai_chat_kwargs("k3", level="high") == {
        "temperature": 1.0,
        "extra_body": {"reasoning_effort": "max"},
    }
    # 非 k3 模型：只修 temperature
    assert kimi_openai_chat_kwargs("kimi-for-coding") == {"temperature": 1.0}


def test_preserves_anthropic_thinking_wire_for_kimi_and_minimax() -> None:
    from src.llm.endpoints import preserves_anthropic_thinking_wire

    assert preserves_anthropic_thinking_wire("https://api.kimi.com/coding")
    assert preserves_anthropic_thinking_wire("https://api.minimaxi.com/anthropic")
    assert not preserves_anthropic_thinking_wire("https://api.anthropic.com")
