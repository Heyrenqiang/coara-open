"""
Coara v8 - LLM Provider registry

Global singleton registry for LLM provider instances (connection pool).
"""

from __future__ import annotations

import contextlib

from src.core.config import ConfigManager
from src.core.errors import ConfigError, ProviderNotFoundError
from src.core.logger import logger
from src.llm.drivers import create_provider_from_config
from src.llm.provider import LLMProvider


class ProviderRegistry:
    """Registry of LLM provider instances keyed by configured provider name."""

    def __init__(self) -> None:
        self._providers: dict[str, LLMProvider] = {}

    def register(self, name: str, provider: LLMProvider) -> None:
        self._providers[name] = provider
        logger.debug(f"Registered provider: {name}")

    def get(self, name: str) -> LLMProvider:
        if name in self._providers:
            return self._providers[name]

        raise ProviderNotFoundError(name)

    def has(self, name: str) -> bool:
        return name in self._providers

    def list_available(self) -> list[str]:
        return sorted(self._providers.keys())

    async def close_all(self) -> None:
        for name, provider in list(self._providers.items()):
            try:
                await provider.close()
                logger.debug(f"Provider closed: {name}")
            except Exception as e:
                logger.warning(f"Error closing provider '{name}': {e}")

    def clear(self) -> None:
        self._providers.clear()


provider_registry = ProviderRegistry()


async def initialize_providers(config_manager: ConfigManager) -> None:
    """Load all providers from config and configure LLMService profiles.

    Providers are registered even when the API key env var is empty, so CLI/UI
    can start; the first LLM call then raises ``APIKeyError``.
    """
    # provider 构造是纯计算（HTTP client 懒加载、无网络、无共享写），并行创建
    # 显著缩短启动（串行各 150-800ms → 并行取最大者）；注册仍在主线程按序进行。
    import asyncio

    names = list(config_manager.list_providers())

    def _build(provider_name: str):
        config = config_manager.get_provider(provider_name)
        try:
            api_key = config_manager.get_api_key(provider_name)
        except ConfigError:
            api_key = ""
        provider = create_provider_from_config(provider_name, config, api_key)
        return provider_name, config, api_key, provider

    built = await asyncio.gather(
        *(asyncio.to_thread(_build, name) for name in names),
        return_exceptions=True,
    )

    for name, result in zip(names, built, strict=False):
        try:
            if isinstance(result, Exception):
                raise result
            provider_name, config, api_key, provider = result
            provider_registry.register(provider_name, provider)
            model = config.default_model or config.models.get("default", "")
            if api_key:
                logger.info(
                    f"Provider initialized: {provider_name} "
                    f"(driver={config.driver or 'inferred'}, model={model}, key=set)"
                )
            else:
                logger.debug(
                    f"Provider registered without key: {provider_name} "
                    f"(driver={config.driver or 'inferred'}, model={model})"
                )
        except Exception as e:
            # 与串行版一致的容错：无 key 静默降级，有 key 升 warning（节点格式
            # 不兼容被跳过时用户必须看得见，避免 0 provider 启动崩在下游不知所云）。
            has_key = False
            with contextlib.suppress(Exception):
                has_key = bool((config_manager.get_api_key(name) or "").strip())
            if not has_key:
                logger.debug(f"Provider '{name}' skipped at startup (no API key): {e}")
            else:
                logger.warning(f"Failed to initialize provider '{name}': {e}")

    from src.llm.service import llm_service

    llm_service.configure(config_manager)
    logger.info(
        f"Provider initialization complete: {len(provider_registry.list_available())} providers, "
        f"{len(llm_service.list_profiles())} llm profiles"
    )


async def close_all() -> None:
    """Close all provider HTTP clients."""
    await provider_registry.close_all()
