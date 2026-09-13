"""LLM protocol driver detection and provider factory helpers."""

from __future__ import annotations

from src.core.errors import ConfigError
from src.core.types import LLMProviderConfig
from src.llm.endpoints import _OPENAI_COMPAT_HOST_MARKERS
from src.llm.provider import LLMProvider


def infer_driver(provider_name: str, base_url: str, explicit: str | None) -> str:
    """Resolve driver id: explicit config, then URL/name heuristics."""
    if explicit:
        normalized = explicit.strip().lower()
        if normalized in ("anthropic", "openai", "responses"):
            return normalized
        raise ConfigError(f"Unknown LLM driver '{explicit}' for provider '{provider_name}'")

    from src.llm.endpoints import is_deepseek_openai_endpoint

    # DeepSeek 官方端点默认 Responses（deepseek-flash）；勿落到 Chat Completions
    if is_deepseek_openai_endpoint(base_url):
        return "responses"

    base_lower = (base_url or "").lower()
    name_lower = provider_name.lower()
    if "openai" in name_lower or any(marker in base_lower for marker in _OPENAI_COMPAT_HOST_MARKERS):
        return "openai"
    if "anthropic" in base_lower or "minimax" in base_lower or "minimaxi" in base_lower:
        return "anthropic"
    raise ConfigError(
        f"Cannot infer LLM driver for provider '{provider_name}' (base_url='{base_url}'). "
        "Set the driver explicitly, e.g. `driver: openai` or `driver: anthropic` in providers.yaml."
    )


def create_provider_from_config(name: str, config: LLMProviderConfig, api_key: str) -> LLMProvider:
    """Instantiate an LLMProvider from connection config and driver id."""
    from src.llm.anthropic import AnthropicProvider
    from src.llm.openai import OpenAIProvider
    from src.llm.responses import ResponsesProvider
    from src.llm.vision import vision_model_ids_from_config

    driver = infer_driver(name, config.base_url, config.driver or None)
    default_model = config.default_model or config.models.get("default", "")
    max_tokens = config.max_tokens
    # 显式声明支持图像的模型集合（vision:true / input_modalities 含 image），
    # 注入 provider 转换层，使配置的视觉模型真正透传图片（而不只走白名单）。
    vision_model_ids = vision_model_ids_from_config(config)

    if driver == "openai":
        provider = OpenAIProvider(
            name=name,
            api_key=api_key,
            base_url=config.base_url or None,
            default_model=default_model or "",
            default_max_tokens=max_tokens,
            vision_model_ids=vision_model_ids,
        )
    elif driver == "responses":
        provider = ResponsesProvider(
            name=name,
            api_key=api_key,
            base_url=config.base_url or None,
            default_model=default_model or "deepseek-flash",
            default_max_tokens=max_tokens,
            vision_model_ids=vision_model_ids,
        )
    else:
        provider = AnthropicProvider(
            name=name,
            api_key=api_key,
            base_url=config.base_url or None,
            default_model=default_model or "",
            default_max_tokens=max_tokens,
            vision_model_ids=vision_model_ids,
        )
    # 标记协议驱动：switch_llm 据此判断跨协议切换（清历史 reasoning_content）
    provider.driver = driver
    return provider
