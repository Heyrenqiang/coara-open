"""Built-in mainstream LLM vendor presets for the Web UI provider panel.

Each preset carries everything a non-developer user should NOT have to know:
protocol driver, base URL, recommended models. The user picks a vendor, pastes
their API key, and is done — base_url / driver / models come pre-filled and can
still be overridden.

Served to the frontend via ``/api/v1/provider-presets`` so web and CLI share a
single source of truth (no duplicated hard-coded vendor lists).
"""

from __future__ import annotations

from typing import Any

#: Vendor preset shape:
#:   id        – stable key used as the provider name on add
#:   label     – human-readable vendor name shown in the picker
#:   driver    – protocol driver (openai / anthropic / responses)
#:   base_url  – API endpoint
#:   models    – recommended model ids (first becomes default)
#:   max_tokens / temperature – optional provider-level defaults
PROVIDER_PRESETS: list[dict[str, Any]] = [
    {
        "id": "deepseek",
        "label": "DeepSeek 深度求索",
        "driver": "responses",
        "base_url": "https://api.deepseek.com",
        "default_model": "deepseek-flash",
        "models": ["deepseek-flash"],
    },
    {
        "id": "kimi",
        "label": "Kimi 月之暗面",
        "driver": "anthropic",
        "base_url": "https://api.kimi.com/coding",
        "default_model": "k3",
        "models": ["k3"],
        "max_tokens": 131072,
    },
    {
        "id": "zhipu",
        "label": "智谱 GLM",
        "driver": "responses",
        "base_url": "https://open.bigmodel.cn/api/v1",
        "default_model": "glm-5.3",
        "models": ["glm-5.3"],
        "max_tokens": 131072,
    },
    {
        "id": "minimax",
        "label": "MiniMax",
        "driver": "anthropic",
        "base_url": "https://api.minimaxi.com/anthropic",
        "default_model": "MiniMax-M3",
        "models": ["MiniMax-M3"],
        "max_tokens": 131072,
    },
    {
        "id": "custom",
        "label": "自定义 (OpenAI/Anthropic 兼容)",
        "driver": "openai",
        "base_url": "",
        "default_model": "",
        "models": [],
    },
]


def list_provider_presets() -> list[dict[str, Any]]:
    """Return the preset list (already JSON-serializable)."""
    return PROVIDER_PRESETS


