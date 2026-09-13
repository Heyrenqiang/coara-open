"""统一视觉门控（src/llm/vision.py）与三 provider 图片块视觉门行为测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.core.types import Message, MessageRole
from src.llm.anthropic import AnthropicProvider
from src.llm.openai import OpenAIProvider
from src.llm.vision import (
    IMAGE_OMITTED_PLACEHOLDER,
    is_known_vision_model,
    model_supports_vision,
    vision_model_ids_from_config,
)

_IMAGE_BLOCK = {
    "type": "image",
    "source": {"type": "base64", "media_type": "image/png", "data": "aGVsbG8="},
}


def _fake_config(available: list[dict]) -> SimpleNamespace:
    """构造带 models.available 的假 config_manager。"""
    cfg = SimpleNamespace(models={"available": available})
    manager = SimpleNamespace()
    manager.get_provider = lambda _name: cfg
    return manager


# ── 白名单 / 显式配置 / fail-closed ────────────────────────────


def test_is_known_vision_model_whitelist():
    assert is_known_vision_model("acme-vision-pro")
    # 具体品牌/旧视觉模型不再由白名单识别（按配置 + 通用 *vision* marker 兜底）
    assert not is_known_vision_model("minimax-vl")
    assert not is_known_vision_model("deepseek-flash")
    assert not is_known_vision_model("MiniMax-M3")
    assert not is_known_vision_model("agnes-2.5-flash")
    assert not is_known_vision_model("")


def test_model_supports_vision_whitelist_default():
    assert model_supports_vision("acme-vision-pro")
    assert not model_supports_vision("deepseek-flash")
    assert not model_supports_vision("")


def test_model_supports_vision_explicit_config_overrides_whitelist():
    # 显式 vision: true 使白名单外的型号也支持
    manager = _fake_config([{"id": "my-vision-model", "vision": True}])
    assert model_supports_vision("my-vision-model", provider_name="deepseek", config_manager=manager)
    # 显式 vision: false 覆盖白名单命中
    manager2 = _fake_config([{"id": "acme-vision-pro", "vision": False}])
    assert not model_supports_vision("acme-vision-pro", provider_name="acme", config_manager=manager2)


def test_model_supports_vision_input_modalities():
    manager = _fake_config([{"id": "glm-5.3", "input_modalities": ["image", "text"]}])
    assert model_supports_vision("glm-5.3", provider_name="zhipu", config_manager=manager)
    manager2 = _fake_config([{"id": "glm-5.3", "modalities": "image,text"}])
    assert model_supports_vision("glm-5.3", provider_name="zhipu", config_manager=manager2)


def test_model_supports_vision_entry_without_declaration_falls_back_to_whitelist():
    # 配置里有该模型但没声明 vision → 继续走白名单判断
    manager = _fake_config([{"id": "acme-vision-pro", "function_calling": True}])
    assert model_supports_vision("acme-vision-pro", provider_name="acme", config_manager=manager)


# ── OpenAI 转换层视觉门 ─────────────────────────────────────────


@pytest.fixture
def openai_provider():
    return OpenAIProvider(name="openai", api_key="sk-test", base_url="https://api.openai.com/v1")


def test_openai_image_passthrough_for_vision_model(openai_provider):
    msgs = [Message(role=MessageRole.USER, content=[{"type": "text", "text": "看"}, _IMAGE_BLOCK])]
    out = openai_provider._convert_messages(msgs, model="acme-vision-pro")
    assert out[0]["content"][0] == {"type": "text", "text": "看"}
    assert out[0]["content"][1]["type"] == "image_url"
    assert out[0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_openai_image_placeholder_for_non_vision_model(openai_provider):
    msgs = [Message(role=MessageRole.USER, content=[{"type": "text", "text": "看"}, _IMAGE_BLOCK])]
    out = openai_provider._convert_messages(msgs, model="agnes-2.5-flash")
    assert out[0]["content"][0] == {"type": "text", "text": "看"}
    assert out[0]["content"][1] == {"type": "text", "text": IMAGE_OMITTED_PLACEHOLDER}


def test_openai_tool_result_image_placeholder_for_non_vision_model(openai_provider):
    from src.core.types import ToolCall

    msgs = [
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="c1", name="view_image", arguments={})],
        ),
        Message(
            role=MessageRole.TOOL_RESULT,
            content=[_IMAGE_BLOCK],
            tool_call_id="c1",
            name="view_image",
        ),
    ]
    out = openai_provider._convert_messages(msgs, model="agnes-2.5-flash")
    user_msgs = [m for m in out if m["role"] == "user"]
    assert any(
        any(b.get("text") == IMAGE_OMITTED_PLACEHOLDER for b in m["content"]) for m in user_msgs
    )


# ── Anthropic 转换层视觉门 ──────────────────────────────────────


@pytest.fixture
def anthropic_provider():
    return AnthropicProvider(name="anthropic", api_key="sk-test", base_url="https://api.anthropic.com")


def test_anthropic_user_image_passthrough_for_vision_model(anthropic_provider):
    msgs = [Message(role=MessageRole.USER, content=[{"type": "text", "text": "看"}, _IMAGE_BLOCK])]
    _system, out = anthropic_provider._convert_messages(msgs, model="acme-vision-pro")
    assert out[0]["content"][0] == {"type": "text", "text": "看"}
    img = out[0]["content"][1]
    assert img["type"] == "image"
    assert img["source"] == _IMAGE_BLOCK["source"]


def test_anthropic_user_image_placeholder_for_non_vision_model(anthropic_provider):
    msgs = [Message(role=MessageRole.USER, content=[{"type": "text", "text": "看"}, _IMAGE_BLOCK])]
    _system, out = anthropic_provider._convert_messages(msgs, model="k3")
    assert out[0]["content"][0] == {"type": "text", "text": "看"}
    ph = out[0]["content"][1]
    assert ph["type"] == "text"
    assert ph["text"] == IMAGE_OMITTED_PLACEHOLDER


def test_anthropic_tool_result_image_placeholder_for_non_vision_model(anthropic_provider):
    from src.core.types import ToolCall

    msgs = [
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="c1", name="view_image", arguments={})],
        ),
        Message(
            role=MessageRole.TOOL_RESULT,
            content=[{"type": "text", "text": "见附件"}, _IMAGE_BLOCK],
            tool_call_id="c1",
            name="view_image",
        ),
    ]
    _system, out = anthropic_provider._convert_messages(msgs, model="k3")
    tool_result_msg = next(m for m in out if any(b.get("type") == "tool_result" for b in m["content"]))
    payload = next(b for b in tool_result_msg["content"] if b.get("type") == "tool_result")
    assert payload["type"] == "tool_result"
    texts = [b["text"] for b in payload["content"] if b.get("type") == "text"]
    assert IMAGE_OMITTED_PLACEHOLDER in texts
    assert all(b.get("type") != "image" for b in payload["content"])


# ── 显式配置视觉集合注入（漏洞修复：配置的视觉模型透传而非占位）────────


def test_vision_model_ids_from_config():
    from types import SimpleNamespace

    config = SimpleNamespace(
        models={"available": [{"id": "m1", "vision": True}, {"id": "m2", "function_calling": True}]}
    )
    assert vision_model_ids_from_config(config) == frozenset({"m1"})
    assert vision_model_ids_from_config(SimpleNamespace(models={})) == frozenset()


def test_openai_passthrough_with_injected_vision_model_ids():
    p = OpenAIProvider(
        name="openai",
        api_key="sk-test",
        base_url="https://api.openai.com/v1",
        vision_model_ids=frozenset({"my-vendor-flagship"}),
    )
    msgs = [Message(role=MessageRole.USER, content=[{"type": "text", "text": "看"}, _IMAGE_BLOCK])]
    out = p._convert_messages(msgs, model="my-vendor-flagship")
    assert out[0]["content"][1]["type"] == "image_url"
    assert out[0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_openai_placeholder_without_injected_vision_model_ids():
    # 未注入 vision_model_ids 且不在白名单 → 占位（fail-closed）
    p = OpenAIProvider(name="openai", api_key="sk-test", base_url="https://api.openai.com/v1")
    msgs = [Message(role=MessageRole.USER, content=[{"type": "text", "text": "看"}, _IMAGE_BLOCK])]
    out = p._convert_messages(msgs, model="my-vendor-flagship")
    assert out[0]["content"][1] == {"type": "text", "text": IMAGE_OMITTED_PLACEHOLDER}
