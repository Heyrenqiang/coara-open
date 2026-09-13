"""Per-provider / per-model call defaults."""

from __future__ import annotations

from src.core.types import LLMProfileConfig, LLMProviderConfig
from src.llm.call_defaults import (
    AGNES_MAX_OUTPUT_TOKENS,
    clamp_max_tokens_for_model,
    resolve_best_call_defaults,
)


class _FakeCM:
    def __init__(
        self,
        providers: dict[str, LLMProviderConfig],
        profiles: dict[str, LLMProfileConfig] | None = None,
    ) -> None:
        self._providers = providers
        self._profiles = profiles or {}

    def get_provider(self, name: str) -> LLMProviderConfig:
        return self._providers[name]

    def list_llm_profiles(self) -> list[str]:
        return list(self._profiles.keys())

    def get_llm_profile(self, name: str) -> LLMProfileConfig:
        return self._profiles[name]


def test_builtin_table_covers_all_configured_vendors() -> None:
    cases = {
        ("deepseek", "deepseek-flash"): (393_216, 1.0, None),
        ("minimax", "MiniMax-M3"): (131_072, 1.0, None),
        ("xiaomi", "mimo-v2.5-pro"): (16_384, 0.7, None),
        ("agnes", "agnes-2.5-flash"): (16_384, 1.0, AGNES_MAX_OUTPUT_TOKENS),
        ("kimi", "k3"): (131_072, 1.0, None),
        ("longcat", "LongCat-2.0"): (16_384, 0.7, None),
    }
    for (provider, model), (max_tokens, temperature, cap) in cases.items():
        got = resolve_best_call_defaults(provider, model)
        assert got.max_tokens == max_tokens, (provider, model)
        assert got.temperature == temperature, (provider, model)
        assert got.max_output_cap == cap, (provider, model)


def test_yaml_overrides_and_hard_cap() -> None:
    cm_ok = _FakeCM(
        {
            "agnes": LLMProviderConfig(
                name="agnes",
                base_url="https://apihub.agnes-ai.cn/v1",
                api_key_env="K",
                max_tokens=16_384,
                models={
                    "available": [
                        {"id": "agnes-2.5-flash", "max_tokens": 32_768, "temperature": 0.5},
                    ]
                },
            )
        }
    )
    got = resolve_best_call_defaults("agnes", "agnes-2.5-flash", config_manager=cm_ok)
    assert got.max_tokens == 32_768
    assert got.temperature == 0.5
    assert got.max_output_cap == AGNES_MAX_OUTPUT_TOKENS

    cm_over = _FakeCM(
        {
            "agnes": LLMProviderConfig(
                name="agnes",
                base_url="https://apihub.agnes-ai.cn/v1",
                api_key_env="K",
                models={
                    "available": [
                        {"id": "agnes-2.5-flash", "max_tokens": 200_000},
                    ]
                },
            )
        }
    )
    over = resolve_best_call_defaults("agnes", "agnes-2.5-flash", config_manager=cm_over)
    assert over.max_tokens == AGNES_MAX_OUTPUT_TOKENS

    assert clamp_max_tokens_for_model("MiniMax-M3", 200_000, provider="minimax") == 200_000
    assert clamp_max_tokens_for_model("agnes-2.5-flash", 200_000, provider="agnes") == AGNES_MAX_OUTPUT_TOKENS
    assert (
        clamp_max_tokens_for_model(
            "agnes-2.5-flash",
            200_000,
            base_url="https://apihub.agnes-ai.cn/v1",
        )
        == AGNES_MAX_OUTPUT_TOKENS
    )
