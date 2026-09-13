"""Agnes AI OpenAI-compatible provider wiring."""

from __future__ import annotations

import os

import pytest

from src.core.types import Message, MessageRole, ToolCall
from src.llm.drivers import infer_driver
from src.llm.endpoints import is_agnes_openai_endpoint
from src.llm.openai import OpenAIProvider


def test_agnes_endpoint_detection_and_driver() -> None:
    for url in (
        "https://apihub.agnes-ai.cn/v1",
        "https://apihub.agnes-ai.com/v1",
        "https://api.agnes-ai.cn/v1",
    ):
        assert is_agnes_openai_endpoint(url)
        assert infer_driver("agnes", url, None) == "openai"
    assert not is_agnes_openai_endpoint("https://api.openai.com/v1")


def test_agnes_context_window_and_max_tokens_clamp() -> None:
    from src.llm.call_defaults import AGNES_MAX_OUTPUT_TOKENS, clamp_max_tokens_for_model

    for model, window in (("agnes-2.5-flash", 512_000),):
        provider = OpenAIProvider(
            name="agnes",
            api_key="test-key",
            base_url="https://apihub.agnes-ai.cn/v1",
            default_model=model,
        )
        assert provider.get_context_window() == window

    assert clamp_max_tokens_for_model("agnes-2.5-flash", 131_072) == AGNES_MAX_OUTPUT_TOKENS
    assert clamp_max_tokens_for_model("MiniMax-M3", 131_072, provider="minimax") == 131_072
    provider = OpenAIProvider(
        name="agnes",
        api_key="test-key",
        base_url="https://apihub.agnes-ai.cn/v1",
        default_model="agnes-2.5-flash",
    )
    kwargs: dict = {"model": "agnes-2.5-flash"}
    provider._apply_token_limit_kwargs(kwargs, 131_072)
    assert kwargs["max_tokens"] == AGNES_MAX_OUTPUT_TOKENS


def test_agnes_thinking_wiring() -> None:
    from src.llm.thinking_mode import (
        classify_thinking_support,
        describe_thinking_status,
        reset_for_tests,
        set_cli_thinking_enabled,
    )
    from src.llm.vendor_options import agnes_chat_extra_body

    base = "https://apihub.agnes-ai.cn/v1"
    assert classify_thinking_support("agnes-2.5-flash", base_url=base, driver="openai") == "agnes_thinking"

    reset_for_tests()
    enabled, source, note = describe_thinking_status("agnes-2.5-flash", base_url=base, driver="openai")
    assert enabled is False
    assert "默认关闭" in source
    assert "Agnes" in note
    assert agnes_chat_extra_body("agnes-2.5-flash") is None

    set_cli_thinking_enabled(True)
    try:
        model = "agnes-2.5-flash"
        assert agnes_chat_extra_body(model) == {"chat_template_kwargs": {"enable_thinking": True}}
        provider = OpenAIProvider(
            name="agnes",
            api_key="test-key",
            base_url=base,
            default_model=model,
        )
        kwargs: dict = {"model": model, "messages": []}
        provider._apply_vendor_extra_body(kwargs, model=model)
        assert kwargs["extra_body"]["chat_template_kwargs"]["enable_thinking"] is True
    finally:
        reset_for_tests()


def test_convert_messages_tool_loop_for_agnes() -> None:
    provider = OpenAIProvider(
        name="agnes",
        api_key="test-key",
        base_url="https://apihub.agnes-ai.cn/v1",
        default_model="agnes-2.5-flash",
    )
    messages = [
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="tc1", name="read", arguments={"path": "a.py"})],
        ),
        Message(role=MessageRole.TOOL_RESULT, content="ok", tool_call_id="tc1"),
    ]
    converted = provider._convert_messages(messages)
    assert converted[0]["tool_calls"][0]["function"]["name"] == "read"
    assert converted[1]["role"] == "tool"
    assert converted[1]["tool_call_id"] == "tc1"


@pytest.mark.asyncio
@pytest.mark.real_env
async def test_agnes_live_chat_completion() -> None:
    """Live smoke — requires AGNES_API_KEY in ``<coara_home>/system/.env``."""
    api_key = os.environ.get("AGNES_API_KEY", "").strip()
    if not api_key:
        pytest.skip("AGNES_API_KEY not set")

    provider = OpenAIProvider(
        name="agnes",
        api_key=api_key,
        base_url="https://apihub.agnes-ai.cn/v1",
        default_model="agnes-2.5-flash",
        default_max_tokens=64,
    )
    try:
        response = await provider.complete(
            messages=[Message(role=MessageRole.USER, content="Reply with exactly: pong")],
            model="agnes-2.5-flash",
            max_tokens=32,
            temperature=0.0,
        )
    finally:
        await provider.close()

    assert response.content
    assert "pong" in response.content.lower()
