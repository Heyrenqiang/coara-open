"""Responses API driver offline tests (conversion / reasoning kwargs / stream)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.core.types import Message, MessageRole, ToolCall
from src.llm.responses import ResponsesProvider, _usage_from_responses
from src.llm.thinking_mode import reset_for_tests, set_cli_thinking_enabled, set_cli_thinking_level

BASE = "https://api.deepseek.com"


@pytest.fixture(autouse=True)
def _reset_thinking():
    reset_for_tests()
    yield
    reset_for_tests()


@pytest.fixture
def provider():
    return ResponsesProvider(name="deepseek", api_key="sk-test", base_url=BASE)


@pytest.fixture
def vision_provider():
    return ResponsesProvider(
        name="deepseek",
        api_key="sk-test",
        base_url=BASE,
        vision_model_ids=frozenset({"deepseek-flash"}),
    )


@pytest.mark.asyncio
async def test_convert_input_roles_and_tool_chain(provider):
    messages = [
        Message(role=MessageRole.SYSTEM, content="系统提示"),
        Message(role=MessageRole.USER, content="查一下天气"),
        Message(
            role=MessageRole.ASSISTANT,
            content="好的",
            reasoning_content="需要先定位",
            tool_calls=[ToolCall(id="call_1", name="get_weather", arguments={"city": "赣州"})],
        ),
        Message(role=MessageRole.TOOL_RESULT, content="晴 30°C", tool_call_id="call_1", name="get_weather"),
    ]
    items = await provider._convert_input(messages)

    assert items[0] == {"type": "message", "role": "system", "content": "系统提示"}
    assert items[1]["role"] == "user"
    assert items[1]["content"][0]["type"] == "input_text"
    # reasoning item 在 assistant message 之前回传
    assert items[2]["type"] == "reasoning"
    assert items[2]["content"][0]["text"] == "需要先定位"
    assert items[3]["role"] == "assistant"
    assert items[3]["content"][0]["type"] == "output_text"
    assert items[4]["type"] == "function_call"
    assert items[4]["call_id"] == "call_1"
    assert '"赣州"' in items[4]["arguments"]
    assert items[5] == {"type": "function_call_output", "call_id": "call_1", "output": "晴 30°C"}


@pytest.mark.asyncio
async def test_convert_input_image_placeholder(provider):
    content = [
        {"type": "text", "text": "看图"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "xx"}},
    ]
    messages = [Message(role=MessageRole.USER, content=content)]
    items = await provider._convert_input(messages, model="deepseek-flash")
    blocks = items[0]["content"]
    assert blocks[0] == {"type": "input_text", "text": "看图"}
    assert blocks[1]["type"] == "input_text"
    assert "不支持图像输入" in blocks[1]["text"]
    assert "xx" not in str(blocks)


@pytest.mark.asyncio
async def test_convert_input_image_passthrough_for_vision_model(vision_provider, monkeypatch):
    async def _no_file_id(*_args, **_kwargs):
        return None

    monkeypatch.setattr("src.llm.responses.get_file_id", _no_file_id)
    content = [
        {"type": "text", "text": "看图"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "xx"}},
    ]
    messages = [Message(role=MessageRole.USER, content=content)]
    items = await vision_provider._convert_input(messages, model="deepseek-flash")
    blocks = items[0]["content"]
    assert blocks[0] == {"type": "input_text", "text": "看图"}
    assert blocks[1] == {"type": "input_image", "image_url": "data:image/png;base64,xx"}


@pytest.mark.asyncio
async def test_convert_input_image_file_api(vision_provider, monkeypatch):
    async def _fake_file_id(*_args, **_kwargs):
        return "file-api-abc123"

    monkeypatch.setattr("src.llm.responses.get_file_id", _fake_file_id)
    content = [
        {"type": "text", "text": "看图"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "xx"}},
    ]
    messages = [Message(role=MessageRole.USER, content=content)]
    items = await vision_provider._convert_input(messages, model="deepseek-flash")
    blocks = items[0]["content"]
    assert blocks[0] == {"type": "input_text", "text": "看图"}
    assert blocks[1] == {"type": "input_image", "file_id": "file-api-abc123"}


def test_convert_tools_function_only(provider):
    tools = [{"name": "read", "description": "读文件", "parameters": {"type": "object", "properties": {}}}]
    converted = provider._convert_tools(tools)
    expected = [
        {
            "type": "function",
            "name": "read",
            "description": "读文件",
            "parameters": {"type": "object", "properties": {}},
        }
    ]
    assert converted == expected
    assert provider._convert_tools(None) is None


@pytest.mark.asyncio
async def test_reasoning_kwargs_levels(provider):
    request = await provider._build_request(
        [Message(role=MessageRole.USER, content="hi")],
        model="deepseek-flash",
        max_tokens=1024,
        temperature=0.7,
        tools=None,
        system_prompt=None,
        stream=True,
        extra={},
    )
    # 默认档 medium → high
    assert request["reasoning"] == {"effort": "high"}
    assert request["max_output_tokens"] == 1024
    assert request["stream"] is True

    set_cli_thinking_level("low")
    assert provider._reasoning_kwargs("deepseek-flash")["reasoning"] == {"effort": "low"}
    set_cli_thinking_level("high")
    assert provider._reasoning_kwargs("deepseek-flash")["reasoning"] == {"effort": "max"}
    set_cli_thinking_enabled(False)
    assert provider._reasoning_kwargs("deepseek-flash")["reasoning"] == {"effort": "none"}


@pytest.mark.asyncio
async def test_instructions_and_tool_choice(provider):
    request = await provider._build_request(
        [Message(role=MessageRole.USER, content="hi")],
        model="deepseek-flash",
        max_tokens=100,
        temperature=0.5,
        tools=[{"name": "t", "description": "d", "parameters": {"type": "object"}}],
        system_prompt="你是助手",
        stream=False,
        extra={},
    )
    assert request["instructions"] == "你是助手"
    assert request["tool_choice"] == "auto"
    assert request["tools"][0]["type"] == "function"


def test_parse_response_with_tool_calls(provider):
    response = SimpleNamespace(
        status="completed",
        output_text="正文",
        output=[
            SimpleNamespace(type="reasoning", content=[SimpleNamespace(text="思考过程")]),
            SimpleNamespace(type="function_call", call_id="c1", id="fc1", name="read", arguments='{"path": "a.py"}'),
        ],
        usage=SimpleNamespace(
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
            input_tokens_details=SimpleNamespace(cached_tokens=60),
            output_tokens_details=SimpleNamespace(reasoning_tokens=20),
        ),
    )
    result = provider._parse_response(response)
    assert result.content == "正文"
    assert result.reasoning_content == "思考过程"
    assert result.tool_calls[0].id == "c1"
    assert result.tool_calls[0].name == "read"
    assert result.tool_calls[0].arguments == {"path": "a.py"}
    assert result.usage["cached_tokens"] == 60
    assert result.usage["reasoning_tokens"] == 20
    assert result.finish_reason == "stop"


def test_parse_response_failed_raises(provider):
    response = SimpleNamespace(status="failed", error={"message": "boom"}, output=[], output_text="")
    with pytest.raises(Exception, match="boom"):
        provider._parse_response(response)


def test_usage_from_responses_dict_details():
    usage = SimpleNamespace(
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        input_tokens_details={"cached_tokens": 3},
        output_tokens_details={"reasoning_tokens": 2},
    )
    result = _usage_from_responses(usage)
    assert result == {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "cached_tokens": 3,
        "reasoning_tokens": 2,
    }
    assert _usage_from_responses(None) == {}


@pytest.mark.asyncio
async def test_stream_event_translation(provider, monkeypatch):
    events = [
        SimpleNamespace(type="response.output_text.delta", delta="你好"),
        SimpleNamespace(type="response.reasoning_text.delta", delta="想一下"),
        SimpleNamespace(
            type="response.output_item.added",
            output_index=0,
            item=SimpleNamespace(type="function_call", call_id="c1", id="fc1", name="read"),
        ),
        SimpleNamespace(type="response.function_call_arguments.delta", output_index=0, delta='{"path"'),
        SimpleNamespace(
            type="response.completed",
            response=SimpleNamespace(
                usage=SimpleNamespace(
                    input_tokens=1,
                    output_tokens=2,
                    total_tokens=3,
                    input_tokens_details=None,
                    output_tokens_details=None,
                )
            ),
        ),
    ]

    class _FakeStream:
        def __aiter__(self):
            async def _gen():
                for e in events:
                    yield e

            return _gen()

    async def _fake_create(**_kwargs):
        return _FakeStream()

    monkeypatch.setattr(provider.client.responses, "create", _fake_create)

    chunks = [chunk async for chunk in provider.stream_complete([Message(role=MessageRole.USER, content="hi")])]
    assert chunks[0].delta_content == "你好"
    assert chunks[1].delta_reasoning == "想一下"
    assert chunks[2].delta_tool_calls[0].id == "c1"
    assert chunks[2].delta_tool_calls[0].name == "read"
    assert chunks[3].delta_tool_calls[0].arguments_fragment == '{"path"'
    assert chunks[-1].finish_reason == "stop"
    assert chunks[-1].usage == {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}


def test_context_window(provider):
    assert provider.get_context_window("deepseek-flash") == 1_000_000
    assert provider.get_context_window("unknown-model") == 128_000
