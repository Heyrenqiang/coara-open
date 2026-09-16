"""
Coara v8 - LLM Provider registry

Global singleton registry for LLM provider instances (connection pool).
"""

from __future__ import annotations

import contextlib
from typing import Any

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
            provider._config_snapshot = _config_fingerprint(provider_name, config, api_key)  # type: ignore[attr-defined]
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


async def reload_providers(config_manager: ConfigManager) -> dict[str, list[str]]:
    """按最新配置热重载 provider：新增注册、变更重建、移除关闭、未变保留。

    供「改完 providers.yaml 立即生效」的调用方（配置助手等）使用：
    先 ``await config_manager.reload()`` 重读磁盘，再调本函数。

    - 在飞回合持有的旧 provider 引用不受影响（各自跑完）；新请求走新实例
    - 变更判定：provider 配置对象与 API key 与已注册实例的快照不一致即重建；
      未注册的跳过（保持与启动时相同的容错：有 key 失败升 warning）
    - 返回 {"added": [...], "replaced": [...], "removed": [...]} 供调用方回报
    """
    added: list[str] = []
    replaced: list[str] = []
    removed: list[str] = []

    current_names = set(config_manager.list_providers())
    registered_names = set(provider_registry.list_available())

    # 移除：配置里已没有、注册表还留着的
    for name in sorted(registered_names - current_names):
        provider = provider_registry._providers.pop(name, None)
        if provider is not None:
            with contextlib.suppress(Exception):
                await provider.close()
            removed.append(name)

    # 新增与变更
    for name in config_manager.list_providers():
        config = config_manager.get_provider(name)
        try:
            api_key = config_manager.get_api_key(name)
        except ConfigError:
            api_key = ""

        existing = provider_registry._providers.get(name)
        if existing is not None and _provider_matches(existing, name, config, api_key):
            continue
        try:
            provider = create_provider_from_config(name, config, api_key)
        except Exception as e:
            if api_key:
                logger.warning(f"Failed to reload provider '{name}': {e}")
            else:
                logger.debug(f"Provider '{name}' skipped at reload (no API key): {e}")
            continue
        provider._config_snapshot = _config_fingerprint(name, config, api_key)  # type: ignore[attr-defined]

        old = provider_registry._providers.get(name)
        provider_registry.register(name, provider)
        if old is not None:
            # 旧实例延迟关闭：在飞请求还在用它，直接 close 会截断在飞流
            _schedule_delayed_close(old)
            replaced.append(name)
        else:
            added.append(name)

    from src.llm.service import llm_service

    llm_service.configure(config_manager)
    logger.info(
        f"Provider reload complete: +{len(added)} ~{len(replaced)} -{len(removed)} "
        f"(total {len(provider_registry.list_available())})"
    )
    return {"added": added, "replaced": replaced, "removed": removed}


def _provider_matches(provider: LLMProvider, name: str, config: Any, api_key: str) -> bool:
    """已注册实例与新配置是否一致（一致则复用，保留 HTTP 客户端与连接池）。"""
    snap = getattr(provider, "_config_snapshot", None)
    if snap is None:
        return False
    return snap == _config_fingerprint(name, config, api_key)


def _config_fingerprint(name: str, config: Any, api_key: str) -> tuple:
    """配置指纹：driver/base_url/key/默认模型/模型清单 全要素。"""
    models = config.models.get("available", [])
    model_ids = tuple(
        (m.get("id") if isinstance(m, dict) else str(m)) for m in models
    )
    return (
        name,
        str(config.driver or ""),
        str(config.base_url or ""),
        api_key,
        str(config.default_model or config.models.get("default", "")),
        bool(getattr(config, "enabled", True)),
        model_ids,
    )


def _schedule_delayed_close(provider: LLMProvider, delay_s: float = 120.0) -> None:
    """旧 provider 延迟关闭：给在飞请求留完成窗口，到期强制关连接。"""
    import asyncio

    async def _close_later() -> None:
        await asyncio.sleep(delay_s)
        with contextlib.suppress(Exception):
            await provider.close()

    with contextlib.suppress(RuntimeError):
        asyncio.get_running_loop().create_task(_close_later())


async def close_all() -> None:
    """Close all provider HTTP clients."""
    await provider_registry.close_all()
