"""DeepSeek Flash thinking classification（仅 Responses 驱动）。"""

from __future__ import annotations

import pytest

from src.llm.thinking_mode import (
    classify_thinking_support,
    reset_for_tests,
    set_cli_thinking_enabled,
    set_cli_thinking_level,
    supports_levels,
)
from src.llm.vendor_options import deepseek_responses_reasoning

DEEPSEEK_BASE = "https://api.deepseek.com"


@pytest.fixture(autouse=True)
def _reset_thinking():
    reset_for_tests()
    yield
    reset_for_tests()


def test_classify_deepseek_flash_responses_only() -> None:
    assert classify_thinking_support("deepseek-flash", base_url=DEEPSEEK_BASE, driver="responses") == "deepseek_v4"
    # Chat Completions / 旧型号一律不认
    assert classify_thinking_support("deepseek-flash", base_url=DEEPSEEK_BASE, driver="openai") == "none"
    assert classify_thinking_support("deepseek-v4-flash", base_url=DEEPSEEK_BASE, driver="responses") == "none"
    assert classify_thinking_support("deepseek-v4-pro", base_url=DEEPSEEK_BASE, driver="responses") == "none"
    assert (
        classify_thinking_support("deepseek-v4-flash-vision-exp", base_url=DEEPSEEK_BASE, driver="responses")
        == "none"
    )
    assert (
        classify_thinking_support("deepseek-flash", base_url="https://api.openai.com/v1", driver="responses")
        == "none"
    )


def test_supports_levels() -> None:
    assert supports_levels("deepseek_v4")


def test_responses_reasoning_default_on_high() -> None:
    assert deepseek_responses_reasoning("deepseek-flash") == {"effort": "high"}


def test_responses_reasoning_level_mapping() -> None:
    set_cli_thinking_level("low")
    assert deepseek_responses_reasoning("deepseek-flash") == {"effort": "low"}
    set_cli_thinking_level("high")
    assert deepseek_responses_reasoning("deepseek-flash") == {"effort": "max"}


def test_responses_reasoning_off() -> None:
    set_cli_thinking_enabled(False)
    assert deepseek_responses_reasoning("deepseek-flash") == {"effort": "none"}


def test_responses_reasoning_ignores_other_models() -> None:
    assert deepseek_responses_reasoning("deepseek-v4-flash") == {}
    assert deepseek_responses_reasoning("deepseek-chat") == {}
    assert deepseek_responses_reasoning("custom-model") == {}
