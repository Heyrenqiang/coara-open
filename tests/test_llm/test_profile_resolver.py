"""Tests for LLM profile resolution."""

from __future__ import annotations

import pytest

from src.core.types import LLMProfileConfig
from src.llm.profile_resolver import ProfileResolver, build_default_profiles
from src.llm.profiles import Profile
from src.llm.provider import LLMProvider, LLMResponse
from src.llm.registry import provider_registry


class _StubProvider(LLMProvider):
    def __init__(self, name: str, model: str) -> None:
        super().__init__(name=name, api_key="test", default_model=model)

    async def complete(self, *args, **kwargs) -> LLMResponse:
        return LLMResponse()

    async def stream_complete(self, *args, **kwargs):
        yield  # pragma: no cover

    def get_context_window(self, model: str | None = None) -> int:
        return 32_000

    async def close(self) -> None:
        pass

    def abort(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _clean_registry():
    provider_registry.clear()
    provider_registry.register("minimax", _StubProvider("minimax", "MiniMax-M2.7"))
    provider_registry.register("deepseek", _StubProvider("deepseek", "deepseek-chat"))
    yield
    provider_registry.clear()


def test_profile_inherit_merges_fields() -> None:
    resolver = ProfileResolver()
    resolver.configure(
        {
            Profile.AGENT_MAIN: LLMProfileConfig(provider="minimax", model="MiniMax-M2.7", max_tokens=8000),
            Profile.AGENT_WEB_SEARCH: LLMProfileConfig(
                inherit=Profile.AGENT_MAIN,
                temperature=0.3,
                max_tokens=4096,
            ),
        }
    )
    resolved = resolver.resolve(Profile.AGENT_WEB_SEARCH)
    assert resolved.provider_name == "minimax"
    assert resolved.model == "MiniMax-M2.7"
    assert resolved.max_tokens == 4096
    assert resolved.temperature == 0.3


def test_resolve_falls_back_when_configured_provider_missing() -> None:
    resolver = ProfileResolver()
    resolver.configure(
        {
            Profile.AGENT_MAIN: LLMProfileConfig(provider="xiaomi", model="mimo-v2.5-pro"),
        }
    )
    resolved = resolver.resolve(Profile.AGENT_MAIN)
    assert resolved.provider_name in {"minimax", "deepseek"}
    assert resolved.model  # falls back to that provider's default_model


def test_resolve_explicit_missing_provider_still_raises() -> None:
    from src.core.errors import ProviderNotFoundError

    resolver = ProfileResolver()
    resolver.configure(
        {
            Profile.AGENT_MAIN: LLMProfileConfig(provider="minimax", model="MiniMax-M2.7"),
        }
    )
    with pytest.raises(ProviderNotFoundError):
        resolver.resolve(Profile.AGENT_MAIN, provider_name="xiaomi")


def test_build_default_profiles_includes_core_profiles() -> None:
    profiles = build_default_profiles(
        default_provider="minimax",
        default_model="MiniMax-M2.7",
    )
    resolver = ProfileResolver()
    resolver.configure(profiles)
    resolved = resolver.resolve(Profile.AGENT_MAIN)
    assert resolved.provider_name == "minimax"
    assert resolved.model == "MiniMax-M2.7"
    assert Profile.CONTEXT_COMPRESSION in profiles
    assert Profile.WORKFLOW_NODE in profiles
    assert "workflow.qc" not in profiles  # 已随 qc_agent 内核化删除


def test_infer_driver_openai_url() -> None:
    from src.llm.drivers import infer_driver

    assert infer_driver("openai", "https://api.openai.com/v1", None) == "openai"
    assert infer_driver("deepseek", "https://api.deepseek.com/v1", None) == "responses"
    assert infer_driver("deepseek", "https://api.deepseek.com", None) == "responses"
    assert infer_driver("vendor", "https://vendor.example.com/compatible-mode/v1", None) == "openai"
    assert infer_driver("minimax", "https://api.minimaxi.com/anthropic", None) == "anthropic"


def test_infer_driver_unknown_url_raises_config_error() -> None:
    from src.core.errors import ConfigError
    from src.llm.drivers import infer_driver

    with pytest.raises(ConfigError, match="driver"):
        infer_driver("custom", "https://llm.example.com/v1", None)

    # Explicit driver still wins over unknown URLs.
    assert infer_driver("custom", "https://llm.example.com/v1", "openai") == "openai"


def test_configure_merges_missing_default_profiles() -> None:
    from unittest.mock import MagicMock

    from src.core.types import CoaraConfig, LLMProfileConfig
    from src.llm.service import LLMService

    svc = LLMService()
    config = CoaraConfig(
        default_provider="minimax",
        default_model="MiniMax-M2.7",
        llm_profiles={
            Profile.AGENT_MAIN: LLMProfileConfig(provider="minimax", model="MiniMax-M2.7"),
        },
    )
    config_manager = MagicMock()
    config_manager.config = config
    config_manager._raw_config = {}

    svc.configure(config_manager)
    assert Profile.AGENT_WEB_SEARCH in svc.list_profiles()
    resolved = svc.resolve(Profile.AGENT_WEB_SEARCH)
    assert resolved.provider_name == "minimax"
    assert resolved.temperature == 0.3


def test_llm_service_reset_and_configure_test_profiles() -> None:
    from src.llm.service import LLMService

    svc = LLMService()
    svc.configure_test_profiles(default_provider="minimax", default_model="MiniMax-M2.7")
    assert svc.configured
    assert Profile.AGENT_WEB_SEARCH in svc.list_profiles()

    svc.reset_for_tests()
    assert not svc.configured
    assert svc.list_profiles() == []


def test_resolve_runtime_provider_override_uses_target_max_tokens() -> None:
    """工作空间 /model 覆盖 provider/model 时，勿沿用旧 profile 的 max_tokens。"""
    resolver = ProfileResolver()
    resolver.configure(
        {
            Profile.AGENT_MAIN: LLMProfileConfig(
                provider="minimax",
                model="MiniMax-M3",
                max_tokens=131072,
                temperature=1.0,
            ),
        }
    )
    resolved = resolver.resolve(
        Profile.AGENT_MAIN,
        provider_name="deepseek",
        model="deepseek-flash",
    )
    assert resolved.provider_name == "deepseek"
    assert resolved.model == "deepseek-flash"
    # 官方最大输出 384K，不得被 MiniMax profile 的 131072 盖住
    assert resolved.max_tokens == 393_216
