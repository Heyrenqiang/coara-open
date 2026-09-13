"""OpenAI-compatible provider context window registry."""

from __future__ import annotations

from src.llm.openai import OpenAIProvider


def test_glm_53_context_window_is_1m_not_output_cap() -> None:
    """UI/压缩用 get_context_window；max_tokens=128K 是输出上限，勿混为上下文。"""
    provider = OpenAIProvider(
        name="zhipu",
        api_key="test-key",
        base_url="https://open.bigmodel.cn/api/coding/paas/v4",
        default_model="glm-5.3",
    )
    assert provider.get_context_window() == 1_000_000
    assert provider.get_context_window("glm-5.3") == 1_000_000
    assert provider.get_context_window("glm-5.3[1m]") == 1_000_000
    assert provider.get_context_window("glm-5.2") == 1_000_000


def test_kimi_k3_openai_context_window() -> None:
    provider = OpenAIProvider(
        name="kimi",
        api_key="test-key",
        base_url="https://api.kimi.com/coding/v1",
        default_model="k3",
    )
    assert provider.get_context_window() == 1_048_576
    assert provider.get_context_window("k3-256k") == 262_144


def test_unknown_openai_model_falls_back_to_128k() -> None:
    provider = OpenAIProvider(name="openai", api_key="test-key", default_model="custom-model")
    assert provider.get_context_window("totally-unknown-model") == 128_000
