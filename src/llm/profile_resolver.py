"""Resolve llm_profiles configuration into concrete provider + model calls."""

from __future__ import annotations

from typing import Any

from src.core.errors import ConfigError
from src.core.logger import logger
from src.core.types import LLMProfileConfig
from src.llm.call_defaults import clamp_max_tokens_for_model, resolve_best_call_defaults
from src.llm.profiles import Profile
from src.llm.request import ResolvedLLMCall

_FALLBACK_TEMPERATURE = 0.7


def _pick_usable_or_first(
    provider_registry: Any,
    *,
    provider_name: str | None,
    model: str | None,
) -> tuple[str, str | None]:
    """已注册 provider 中挑默认目标：有可用 key 的优先（声明序），否则首个。

    声明序按 config_manager.list_providers()（yaml 声明顺序）——
    provider_registry.list_available() 是字母序，直接拿首位会把「填了 minimax
    的新装机」绑到字母序靠前的 deepseek（发布版默认模型错配 bug 的回退侧根因）。
    返回 (provider, model)：显式传了 model 时原样透传，否则 None 交下游取
    provider 默认模型。无可用 provider 时抛 ProviderNotFoundError。
    """
    available = provider_registry.list_available()
    if not available:
        available = _retry_register_providers_from_config()
    if not available:
        from src.core.errors import ProviderNotFoundError

        raise ProviderNotFoundError(provider_name or "default")
    from src.core.api_keys import is_usable_api_key

    try:
        from src.core.config import config_manager

        declared = [n for n in config_manager.list_providers() if n in set(available)]
        ordered = [*declared, *(n for n in available if n not in set(declared))]
    except Exception:
        ordered = available

    picked = ordered[0]
    for name in ordered:
        key = str(getattr(provider_registry.get(name), "api_key", "") or "").strip()
        if is_usable_api_key(key):
            picked = name
            break
    return picked, model


def _retry_register_providers_from_config() -> list[str]:
    """initialize_providers 全失败后的补救：逐个从 config 重试注册 provider。

    即使节点无 key 也注册（LLM 调用时才报 APIKeyError），让首发/配置损坏场景
    能启动进添加模型流程，而不是崩在 ProviderNotFoundError。返回可用 provider 名。
    """
    try:
        from src.core.config import config_manager
        from src.llm.drivers import create_provider_from_config
        from src.llm.registry import provider_registry

        for name in config_manager.list_providers():
            if provider_registry.has(name):
                continue
            try:
                config = config_manager.get_provider(name)
                try:
                    api_key = config_manager.get_api_key(name)
                except ConfigError:
                    api_key = ""
                provider_registry.register(name, create_provider_from_config(name, config, api_key))
            except Exception as exc:
                logger.debug(f"retry register provider '{name}' failed: {exc}")
        return provider_registry.list_available()
    except Exception as exc:
        logger.debug(f"retry register providers from config failed: {exc}")
        return []


class ProfileResolver:
    """Resolve profile names using merged config and optional runtime overrides."""

    def __init__(self) -> None:
        self._profiles: dict[str, LLMProfileConfig] = {}
        self._default_profile = Profile.DEFAULT

    def configure(
        self,
        profiles: dict[str, LLMProfileConfig],
        *,
        default_profile: str = Profile.DEFAULT,
    ) -> None:
        self._profiles = dict(profiles)
        self._default_profile = default_profile or Profile.DEFAULT

    def resolve(
        self,
        profile: str | None = None,
        *,
        provider_name: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> ResolvedLLMCall:
        from src.llm.registry import provider_registry

        profile_name = profile or self._default_profile
        merged = self._merge_profile_chain(profile_name)

        final_provider = provider_name or merged.provider
        final_model = model or merged.model
        if not final_provider:
            # provider——典型是未配置默认的新装机（模板不再硬绑默认模型，跟随全局）。
            final_provider, final_model = _pick_usable_or_first(
                provider_registry, provider_name=provider_name, model=model
            )

        # Config may name a provider that failed to register. Fall back when possible.
        if not provider_registry.has(final_provider):
            available = provider_registry.list_available()
            if not available:
                # 现场从 config 逐个重试注册一次，能救则救；仍无则降级到 config 首个
                available = _retry_register_providers_from_config()
            if not available:
                from src.core.errors import ProviderNotFoundError

                raise ProviderNotFoundError(final_provider)
            if provider_name is not None:
                from src.core.errors import ProviderNotFoundError

                raise ProviderNotFoundError(final_provider)
            fallback = _pick_usable_or_first(provider_registry, provider_name=None, model=None)[0]
            logger.warning(f"Configured provider '{final_provider}' is not available; falling back to '{fallback}'")
            final_provider = fallback
            if model is None:
                final_model = None

        if not final_model:
            final_model = provider_registry.get(final_provider).default_model
        if not final_model:
            raise ConfigError(f"Profile '{profile_name}' has no model configured")

        provider = provider_registry.get(final_provider)
        from src.core.config import config_manager as _cm

        best = resolve_best_call_defaults(
            final_provider,
            final_model,
            config_manager=_cm if getattr(_cm, "_config", None) is not None else None,
            base_url=getattr(provider, "base_url", "") or "",
        )

        # 显式参数 > profile（仅当未换 provider/model）> 目标型号最佳默认 > provider 实例默认
        # 工作空间 /model 只覆盖 provider/model 时，须按目标型号重取 max_tokens/temperature
        target_retargeted = (provider_name is not None and provider_name != (merged.provider or "")) or (
            model is not None and model != (merged.model or "")
        )
        profile_max = None if target_retargeted else merged.max_tokens
        profile_temp = None if target_retargeted else merged.temperature

        final_max_tokens = max_tokens if max_tokens is not None else profile_max
        if final_max_tokens is None:
            final_max_tokens = best.max_tokens
        if final_max_tokens is None:
            final_max_tokens = provider.get_default_max_tokens(final_model)

        final_temperature = temperature if temperature is not None else profile_temp
        if final_temperature is None:
            final_temperature = best.temperature if best.temperature is not None else _FALLBACK_TEMPERATURE

        final_max_tokens = clamp_max_tokens_for_model(
            final_model,
            final_max_tokens,
            provider=final_provider,
            base_url=getattr(provider, "base_url", "") or "",
        )

        return ResolvedLLMCall(
            profile=profile_name,
            provider_name=final_provider,
            model=final_model,
            provider=provider,
            max_tokens=final_max_tokens,
            temperature=final_temperature,
        )

    def list_profiles(self) -> list[str]:
        return sorted(self._profiles.keys())

    def _merge_profile_chain(self, profile_name: str, *, _visited: set[str] | None = None) -> LLMProfileConfig:
        visited = _visited or set()
        if profile_name in visited:
            raise ConfigError(f"Circular inherit in llm_profiles: {profile_name}")
        visited.add(profile_name)

        raw = self._profiles.get(profile_name)
        if raw is None:
            if profile_name == Profile.DEFAULT and self._profiles:
                fallback = Profile.AGENT_MAIN if Profile.AGENT_MAIN in self._profiles else next(iter(self._profiles))
                return self._merge_profile_chain(fallback, _visited=visited)
            raise ConfigError(f"Unknown LLM profile: {profile_name}")

        if raw.inherit:
            parent = self._merge_profile_chain(raw.inherit, _visited=visited)
            return _overlay_profile(parent, raw)
        return raw


def _overlay_profile(base: LLMProfileConfig, override: LLMProfileConfig) -> LLMProfileConfig:
    data = base.model_dump()
    for key, value in override.model_dump().items():
        if key == "inherit":
            continue
        if value is not None and value != "" and value != {}:
            data[key] = value
    data["inherit"] = None
    return LLMProfileConfig(**data)


def build_default_profiles(
    *,
    default_provider: str,
    default_model: str,
) -> dict[str, LLMProfileConfig]:
    """Synthesize profiles when llm_profiles is absent from config files."""
    best = resolve_best_call_defaults(default_provider, default_model)
    main = LLMProfileConfig(
        provider=default_provider,
        model=default_model,
        max_tokens=best.max_tokens,
        temperature=best.temperature if best.temperature is not None else 0.7,
    )
    profiles: dict[str, LLMProfileConfig] = {
        Profile.AGENT_MAIN: main,
        Profile.AGENT_WEB_SEARCH: LLMProfileConfig(
            inherit=Profile.AGENT_MAIN,
            max_tokens=4096,
            temperature=0.3,
        ),
        Profile.CONTEXT_COMPRESSION: LLMProfileConfig(
            inherit=Profile.AGENT_MAIN,
            max_tokens=2048,
            temperature=0.2,
        ),
        Profile.WORKFLOW_NODE: LLMProfileConfig(
            provider=default_provider,
            model=default_model,
            temperature=0.3,
        ),
    }
    # Prefer MiniMax-M3 for workflow nodes when the default provider is minimax
    # (matches src/workflow/node_llm.py engine hard defaults).
    if default_provider == "minimax":
        profiles[Profile.WORKFLOW_NODE] = LLMProfileConfig(
            provider="minimax",
            model="MiniMax-M3",
            max_tokens=131072,
            temperature=1.0,
        )
    return profiles
