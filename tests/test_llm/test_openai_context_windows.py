"""Context window registry — ≠ max_tokens (output cap)."""

from __future__ import annotations

from types import SimpleNamespace

from src.llm.context_windows import (
    DEFAULT_CONTEXT_WINDOW,
    OPENAI_CONTEXT_WINDOWS,
    context_windows_from_config,
    resolve_context_window,
)
from src.llm.openai import OpenAIProvider
from src.llm.responses import ResponsesProvider


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
    assert provider.get_context_window("glm-5.3-flash") == 1_000_000


def test_kimi_k3_openai_context_window() -> None:
    provider = OpenAIProvider(
        name="kimi",
        api_key="test-key",
        base_url="https://api.kimi.com/coding/v1",
        default_model="k3",
    )
    assert provider.get_context_window() == 1_048_576
    assert provider.get_context_window("k3-256k") == 262_144
    assert provider.get_context_window("kimi-for-coding") == 1_048_576


def test_minimax_m3_context_window_is_1m() -> None:
    provider = ResponsesProvider(
        name="minimax",
        api_key="test-key",
        base_url="https://api.minimaxi.com/v1",
        default_model="MiniMax-M3",
    )
    assert provider.get_context_window() == 1_000_000
    assert provider.get_context_window("MiniMax-M3") == 1_000_000
    assert provider.get_context_window("MiniMax-M2.7-highspeed") == 204_800


def test_yaml_context_window_overrides_builtin() -> None:
    provider = ResponsesProvider(
        name="minimax",
        api_key="test-key",
        base_url="https://api.minimaxi.com/v1",
        default_model="MiniMax-M3",
        context_windows={"minimax-m3": 512_000},
    )
    assert provider.get_context_window("MiniMax-M3") == 512_000


def test_context_windows_from_config() -> None:
    cfg = SimpleNamespace(
        models={
            "available": [
                {"id": "MiniMax-M3", "context_window": 1_000_000, "max_tokens": 131_072},
                {"id": "skip-me", "max_tokens": 1},
            ]
        }
    )
    assert context_windows_from_config(cfg) == {"minimax-m3": 1_000_000}


def test_unknown_openai_model_falls_back_to_128k() -> None:
    provider = OpenAIProvider(name="openai", api_key="test-key", default_model="custom-model")
    assert provider.get_context_window("totally-unknown-model") == DEFAULT_CONTEXT_WINDOW
    assert resolve_context_window("totally-unknown-model") == DEFAULT_CONTEXT_WINDOW
    assert "minimax-m3" in OPENAI_CONTEXT_WINDOWS
