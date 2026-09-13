"""/model persistence adopts each provider/model's best settings."""

from __future__ import annotations

from src.core.types import LLMProfileConfig, LLMProviderConfig
from src.llm.call_defaults import resolve_best_call_defaults
from src.llm.model_persist import _build_persist_payload, _resolve_persist_call_settings
from src.llm.profiles import Profile


class _FakeConfigManager:
    def __init__(
        self,
        *,
        profile: LLMProfileConfig,
        providers: dict[str, LLMProviderConfig],
        profiles: dict[str, LLMProfileConfig] | None = None,
    ) -> None:
        self._profile = profile
        self._providers = providers
        self._profiles = profiles or {}

    def get_llm_profile(self, name: str) -> LLMProfileConfig:
        if name == Profile.AGENT_MAIN:
            return self._profile
        return self._profiles[name]

    def list_llm_profiles(self) -> list[str]:
        names = [Profile.AGENT_MAIN, *self._profiles.keys()]
        return names

    def get_provider(self, name: str) -> LLMProviderConfig:
        return self._providers[name]


def test_switch_minimax_to_agnes_uses_agnes_best() -> None:
    cm = _FakeConfigManager(
        profile=LLMProfileConfig(provider="minimax", model="MiniMax-M3", max_tokens=131_072, temperature=1.0),
        providers={
            "minimax": LLMProviderConfig(name="minimax", base_url="https://x", api_key_env="K", max_tokens=131_072),
            "agnes": LLMProviderConfig(
                name="agnes",
                base_url="https://apihub.agnes-ai.cn/v1",
                api_key_env="K",
                max_tokens=16_384,
                models={
                    "available": [
                        {"id": "agnes-2.5-flash", "max_tokens": 16_384, "temperature": 1.0},
                    ]
                },
            ),
        },
    )
    max_tokens, temperature = _resolve_persist_call_settings(cm, "agnes", "agnes-2.5-flash")
    assert max_tokens == 16_384
    assert temperature == 1.0
    payload = _build_persist_payload(cm, "agnes", "agnes-2.5-flash")
    main = payload["llm_profiles"][Profile.AGENT_MAIN]
    assert main["max_tokens"] == 16_384
    assert main["temperature"] == 1.0


def test_switch_agnes_back_to_minimax_m3_restores_minimax_best() -> None:
    cm = _FakeConfigManager(
        profile=LLMProfileConfig(provider="agnes", model="agnes-2.5-flash", max_tokens=16_384, temperature=1.0),
        providers={
            "minimax": LLMProviderConfig(
                name="minimax",
                base_url="https://x",
                api_key_env="K",
                max_tokens=131_072,
                models={
                    "available": [
                        {"id": "MiniMax-M3", "max_tokens": 131_072, "temperature": 1.0},
                        {"id": "MiniMax-M2.7", "max_tokens": 65_536, "temperature": 1.0},
                    ]
                },
            ),
            "agnes": LLMProviderConfig(
                name="agnes", base_url="https://apihub.agnes-ai.cn/v1", api_key_env="K", max_tokens=16_384
            ),
        },
    )
    max_tokens, temperature = _resolve_persist_call_settings(cm, "minimax", "MiniMax-M3")
    assert max_tokens == 131_072
    assert temperature == 1.0


def test_same_provider_model_switch_uses_that_model_best() -> None:
    cm = _FakeConfigManager(
        profile=LLMProfileConfig(provider="minimax", model="MiniMax-M3", max_tokens=131_072, temperature=1.0),
        providers={
            "minimax": LLMProviderConfig(
                name="minimax",
                base_url="https://x",
                api_key_env="K",
                max_tokens=131_072,
                models={
                    "available": [
                        {"id": "MiniMax-M3", "max_tokens": 131_072, "temperature": 1.0},
                        {"id": "MiniMax-M2.7-highspeed", "max_tokens": 65_536, "temperature": 1.0},
                    ]
                },
            ),
        },
    )
    max_tokens, _ = _resolve_persist_call_settings(cm, "minimax", "MiniMax-M2.7-highspeed")
    assert max_tokens == 65_536


def test_builtin_defaults_cover_common_providers() -> None:
    assert resolve_best_call_defaults("minimax", "MiniMax-M3").max_tokens == 131_072
    assert resolve_best_call_defaults("xiaomi", "mimo-v2.5-pro").max_tokens == 16_384
    assert resolve_best_call_defaults("kimi", "k3").max_tokens == 131_072
    assert resolve_best_call_defaults("longcat", "LongCat-2.0").max_tokens == 16_384
    assert resolve_best_call_defaults("agnes", "agnes-2.5-flash").max_output_cap == 65_536
