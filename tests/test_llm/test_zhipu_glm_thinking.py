"""智谱 GLM thinking / reasoning_effort wiring (docs.bigmodel.cn)."""

from __future__ import annotations

import pytest

from src.llm.endpoints import is_zhipu_openai_endpoint
from src.llm.thinking_mode import (
    classify_thinking_support,
    describe_thinking_status,
    reset_for_tests,
    run_thinking_command,
    set_cli_thinking_enabled,
    set_cli_thinking_level,
)
from src.llm.vendor_options import glm_forces_thinking, zhipu_glm_chat_kwargs

ZHIPU_CODING = "https://open.bigmodel.cn/api/coding/paas/v4"
ZHIPU_PAAS = "https://open.bigmodel.cn/api/paas/v4"


@pytest.fixture(autouse=True)
def _reset_thinking():
    reset_for_tests()
    yield
    reset_for_tests()


def test_zhipu_endpoint_detection() -> None:
    assert is_zhipu_openai_endpoint(ZHIPU_CODING)
    assert is_zhipu_openai_endpoint(ZHIPU_PAAS)
    assert is_zhipu_openai_endpoint("https://open.bigmodel.cn/api/v1")
    assert not is_zhipu_openai_endpoint("https://api.deepseek.com")


def test_classify_zhipu_glm() -> None:
    assert classify_thinking_support("glm-5.3", base_url=ZHIPU_CODING, driver="openai") == "zhipu_glm"
    assert classify_thinking_support("glm-5.3[1m]", base_url=ZHIPU_CODING, driver="openai") == "zhipu_glm"
    assert classify_thinking_support("glm-5.3", base_url=ZHIPU_CODING, driver="anthropic") == "none"
    assert classify_thinking_support("custom-model", base_url=ZHIPU_CODING, driver="openai") == "none"


def test_glm_forces_thinking() -> None:
    assert glm_forces_thinking("glm-5.3")
    assert glm_forces_thinking("glm-5.3[1m]")
    assert glm_forces_thinking("glm-4.7")
    assert not glm_forces_thinking("glm-5.2")
    assert not glm_forces_thinking("glm-5")


def test_kwargs_default_enabled_high() -> None:
    kwargs = zhipu_glm_chat_kwargs("glm-5.3")
    assert kwargs["extra_body"] == {"thinking": {"type": "enabled"}}
    assert kwargs["reasoning_effort"] == "high"


def test_kwargs_level_mapping() -> None:
    set_cli_thinking_level("low")
    assert zhipu_glm_chat_kwargs("glm-5.3")["reasoning_effort"] == "low"
    set_cli_thinking_level("medium")
    assert zhipu_glm_chat_kwargs("glm-5.3")["reasoning_effort"] == "high"
    set_cli_thinking_level("high")
    assert zhipu_glm_chat_kwargs("glm-5.3")["reasoning_effort"] == "max"


def test_kwargs_off_never_disabled_on_53() -> None:
    set_cli_thinking_enabled(False)
    kwargs = zhipu_glm_chat_kwargs("glm-5.3")
    assert kwargs["extra_body"] == {"thinking": {"type": "enabled"}}
    assert kwargs["reasoning_effort"] == "low"
    assert "disabled" not in str(kwargs)


def test_kwargs_off_disabled_on_optional_models() -> None:
    set_cli_thinking_enabled(False)
    kwargs = zhipu_glm_chat_kwargs("glm-5.2")
    assert kwargs == {"extra_body": {"thinking": {"type": "disabled"}}}


def test_responses_reasoning_default_high() -> None:
    from src.llm.vendor_options import zhipu_glm_responses_reasoning

    assert zhipu_glm_responses_reasoning("glm-5.3") == {"effort": "high"}
    set_cli_thinking_level("low")
    assert zhipu_glm_responses_reasoning("glm-5.3") == {"effort": "low"}
    set_cli_thinking_enabled(False)
    assert zhipu_glm_responses_reasoning("glm-5.3") == {"effort": "low"}
    set_cli_thinking_enabled(True)
    set_cli_thinking_level("high")
    assert zhipu_glm_responses_reasoning("glm-5.3") == {"effort": "max"}


@pytest.mark.asyncio
async def test_responses_provider_builds_reasoning() -> None:
    from src.core.types import Message, MessageRole
    from src.llm.responses import ResponsesProvider

    p = ResponsesProvider(
        name="zhipu",
        api_key="sk-test",
        base_url=ZHIPU_CODING.replace("/api/coding/paas/v4", "/api/v1"),
        default_model="glm-5.3",
    )
    req = await p._build_request(
        [Message(role=MessageRole.USER, content="hi")],
        model="glm-5.3",
        max_tokens=256,
        temperature=1.0,
        tools=None,
        system_prompt=None,
        stream=False,
        extra={},
    )
    assert req["reasoning"] == {"effort": "high"}
    assert req["model"] == "glm-5.3"


def test_classify_responses_driver() -> None:
    assert (
        classify_thinking_support(
            "glm-5.3",
            base_url="https://open.bigmodel.cn/api/v1",
            driver="responses",
        )
        == "zhipu_glm"
    )


def test_thinking_command_off_on_53() -> None:
    result = run_thinking_command(
        "/thinking off",
        model="glm-5.3",
        base_url=ZHIPU_CODING,
        driver="openai",
    )
    assert any("不可关闭" in line or "低档" in line for line in result.lines)
    kwargs = zhipu_glm_chat_kwargs("glm-5.3")
    assert kwargs["reasoning_effort"] == "low"
    assert kwargs["extra_body"]["thinking"]["type"] == "enabled"


def test_thinking_command_levels() -> None:
    low = run_thinking_command("/thinking low", model="glm-5.3", base_url=ZHIPU_CODING, driver="openai")
    assert any("低" in line for line in low.lines)
    assert zhipu_glm_chat_kwargs("glm-5.3")["reasoning_effort"] == "low"
    high = run_thinking_command("/thinking 高", model="glm-5.3", base_url=ZHIPU_CODING, driver="openai")
    assert any("高" in line for line in high.lines)
    assert zhipu_glm_chat_kwargs("glm-5.3")["reasoning_effort"] == "max"


def test_describe_default() -> None:
    enabled, source, note = describe_thinking_status("glm-5.3", base_url=ZHIPU_CODING, driver="openai")
    assert enabled is True
    assert source == "默认开启"
    assert "思考已开" in note
