"""LLM vendor endpoint detection (MiMo, MiniMax, etc.)."""

from __future__ import annotations

from urllib.parse import urlparse

_MIMO_HOST_MARKERS = ("xiaomimimo.com",)
# Agnes hosts (OpenAI-compatible): apihub.agnes-ai.cn, apihub.agnes-ai.com, api.agnes-ai.cn
_AGNES_OPENAI_MARKERS = ("agnes-ai.com", "agnes-ai.cn")
_DEEPSEEK_HOST_MARKERS = ("deepseek.com",)
_ZHIPU_HOST_MARKERS = ("bigmodel.cn", "bigmodel.com", "zhipuai.cn")
_MINIMAX_HOST_MARKERS = ("minimaxi.com", "minimax.chat", "minimax.io")
_KIMI_HOST_MARKERS = ("kimi.com",)

# Single source of truth for OpenAI-compatible hosts; imported by drivers.py.
_OPENAI_COMPAT_HOST_MARKERS = (
    "api.openai.com",
    "openai.azure.com",
    "deepseek.com",
    "dashscope.aliyuncs.com",
    "compatible-mode",
    "openrouter.ai",
    "together.xyz",
    "groq.com",
    "mistral.ai",
    "x.ai",
    "moonshot.cn",
    "siliconflow.cn",
    *_MIMO_HOST_MARKERS,
    *_AGNES_OPENAI_MARKERS,
    *_ZHIPU_HOST_MARKERS,
)


def _endpoint_hostname(base_url: str | None) -> str:
    base = (base_url or "").strip()
    if not base:
        return ""
    if "://" not in base:
        base = f"https://{base}"
    return (urlparse(base).hostname or "").lower()


def _hostname_matches(hostname: str, domains: tuple[str, ...]) -> bool:
    return any(hostname == domain or hostname.endswith(f".{domain}") for domain in domains)


def is_mimo_openai_endpoint(base_url: str | None) -> bool:
    return _hostname_matches(_endpoint_hostname(base_url), _MIMO_HOST_MARKERS)


def is_agnes_openai_endpoint(base_url: str | None) -> bool:
    return _hostname_matches(_endpoint_hostname(base_url), _AGNES_OPENAI_MARKERS)


def is_deepseek_openai_endpoint(base_url: str | None) -> bool:
    return _hostname_matches(_endpoint_hostname(base_url), _DEEPSEEK_HOST_MARKERS)


def is_deepseek_reasoning_model(model: str | None) -> bool:
    """True only for official ``deepseek-flash`` (Responses API)."""
    return (model or "").lower().strip() == "deepseek-flash"


def is_zhipu_openai_endpoint(base_url: str | None) -> bool:
    """智谱 OpenAI 兼容端（Coding Plan /paas/v4 与标准 paas/v4）。"""
    return _hostname_matches(_endpoint_hostname(base_url), _ZHIPU_HOST_MARKERS)


def is_minimax_anthropic_endpoint(base_url: str | None) -> bool:
    base = (base_url or "").lower()
    return _hostname_matches(_endpoint_hostname(base_url), _MINIMAX_HOST_MARKERS) and "anthropic" in base


def is_kimi_anthropic_endpoint(base_url: str | None) -> bool:
    return _hostname_matches(_endpoint_hostname(base_url), _KIMI_HOST_MARKERS)


def is_kimi_openai_endpoint(base_url: str | None) -> bool:
    """Kimi Code OpenAI 兼容端（api.kimi.com/coding/v1）。"""
    return _hostname_matches(_endpoint_hostname(base_url), _KIMI_HOST_MARKERS)


def preserves_anthropic_thinking_wire(base_url: str | None) -> bool:
    """Whether assistant thinking blocks must be echoed on later tool turns.

    MiniMax and Kimi Code both require prior-turn thinking on the Anthropic-compatible
    wire when thinking mode is on; dropping it yields HTTP 400 on the next request.
    """
    return is_minimax_anthropic_endpoint(base_url) or is_kimi_anthropic_endpoint(base_url)
