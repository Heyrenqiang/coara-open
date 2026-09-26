"""switch_llm 任意切换测试（2026-08-16 起跨协议不再拦截，改为清历史思考）。

跨 driver（如 GLM openai ↔ DeepSeek responses）切换：
- 不再报错，直接生效（下一次 LLM 调用即用新 provider）
- 历史思考内容（reasoning_content）被丢弃——跨协议转出会 400 且对新模型无语义
- 对话内容与工具结果完整保留
"""

from __future__ import annotations

from src.core.types import Message, MessageRole
from tests.helpers import FakeProvider, make_test_coara


def _mk_provider(name: str, driver: str, model: str) -> FakeProvider:
    p = FakeProvider([])
    p.driver = driver
    p.name = name
    p.default_model = model
    return p


def _patch_registry(monkeypatch, providers: dict):
    from src.llm.registry import provider_registry

    monkeypatch.setattr(provider_registry, "has", lambda name: name in providers)
    monkeypatch.setattr(provider_registry, "get", lambda name: providers[name])


def test_cross_driver_switch_allowed_and_drops_thinking(tmp_path, monkeypatch) -> None:
    """openai → responses：切换成功，历史 reasoning 清空，对话保留。"""
    coara = make_test_coara(tmp_path)
    current = _mk_provider("zhipu", "openai", "glm-5.3")
    coara.provider = current
    coara.provider_name = "zhipu"
    coara.message_history.extend(
        [
            Message(role=MessageRole.USER, content="任务"),
            Message(role=MessageRole.ASSISTANT, content="开始", reasoning_content="先想一下"),
            Message(role=MessageRole.ASSISTANT, content="结果"),
        ]
    )

    target = _mk_provider("deepseek", "responses", "deepseek-flash")
    _patch_registry(monkeypatch, {"zhipu": current, "deepseek": target})

    applied = coara.switch_llm("deepseek")

    assert applied == ("deepseek", "deepseek-flash")
    assert coara.provider_name == "deepseek"
    assert coara.provider is target
    # 思考被丢弃，对话内容保留
    assert all(msg.reasoning_content is None for msg in coara.message_history)
    contents = [str(m.content) for m in coara.message_history]
    assert contents == ["任务", "开始", "结果"]


def test_same_driver_switch_keeps_thinking(tmp_path, monkeypatch) -> None:
    """同 driver（openai → openai）切换：不触发清理，reasoning 保留。"""
    coara = make_test_coara(tmp_path)
    current = _mk_provider("zhipu", "openai", "glm-5.3")
    coara.provider = current
    coara.provider_name = "zhipu"
    coara.message_history.append(Message(role=MessageRole.ASSISTANT, content="带思考", reasoning_content="思考内容"))

    target = _mk_provider("kimi", "openai", "k3")
    _patch_registry(monkeypatch, {"zhipu": current, "kimi": target})

    applied = coara.switch_llm("kimi")
    assert applied == ("kimi", "k3")
    assert coara.message_history[-1].reasoning_content == "思考内容"


def test_unknown_provider_still_rejected(tmp_path, monkeypatch) -> None:
    from src.core.errors import ProviderNotFoundError

    coara = make_test_coara(tmp_path)
    current = _mk_provider("zhipu", "openai", "glm-5.3")
    coara.provider = current
    coara.provider_name = "zhipu"
    _patch_registry(monkeypatch, {"zhipu": current})

    import pytest

    with pytest.raises(ProviderNotFoundError):
        coara.switch_llm("nope")
    assert coara.provider_name == "zhipu"
