"""Provider usage normalization and prompt token accounting."""

from __future__ import annotations

from typing import Any


def _usage_detail_int(details: Any, field: str) -> int | None:
    if details is None:
        return None
    value = details.get(field) if isinstance(details, dict) else getattr(details, field, None)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def usage_dict_from_openai(usage_obj: Any) -> dict[str, int]:
    """Normalize OpenAI-compatible usage (incl. MiMo cache/reasoning details)."""
    if usage_obj is None:
        return {}
    usage: dict[str, int] = {}
    prompt_tokens = getattr(usage_obj, "prompt_tokens", None)
    completion_tokens = getattr(usage_obj, "completion_tokens", None)
    total_tokens = getattr(usage_obj, "total_tokens", None)
    if prompt_tokens is not None:
        usage["input_tokens"] = int(prompt_tokens)
    if completion_tokens is not None:
        usage["output_tokens"] = int(completion_tokens)
    if total_tokens is not None:
        usage["total_tokens"] = int(total_tokens)

    cached_tokens = _usage_detail_int(getattr(usage_obj, "prompt_tokens_details", None), "cached_tokens")
    if cached_tokens is not None:
        usage["cached_tokens"] = cached_tokens

    reasoning_tokens = _usage_detail_int(
        getattr(usage_obj, "completion_tokens_details", None),
        "reasoning_tokens",
    )
    if reasoning_tokens is not None:
        usage["reasoning_tokens"] = reasoning_tokens

    return usage


def total_prompt_tokens(usage: dict[str, int] | None) -> int:
    """Effective prompt size for context window accounting.

    MiniMax: input_tokens + cache_read + cache_creation.
    MiMo/OpenAI: input_tokens (= prompt_tokens) already includes the full prompt.
    """
    if not usage:
        return 0
    input_tokens = int(usage.get("input_tokens") or 0)
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_create = int(usage.get("cache_creation_input_tokens") or 0)
    if cache_read or cache_create:
        return input_tokens + cache_read + cache_create
    return input_tokens


def cache_read_tokens(usage: dict[str, int] | None) -> int:
    """Tokens served from prompt cache on this turn (provider-normalized)."""
    if not usage:
        return 0
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    if cache_read > 0:
        return cache_read
    return int(usage.get("cached_tokens") or 0)


def prompt_cache_hit_ratio(usage: dict[str, int] | None) -> float | None:
    """Per-turn cache hit ratio (OpenAI/MiMo ``cached_tokens`` or Anthropic ``cache_read``)."""
    if not usage:
        return None
    total = total_prompt_tokens(usage)
    cached = cache_read_tokens(usage)
    if cached > 0 and total > 0:
        return cached / total
    return None


def cumulative_prompt_cache_hit_ratio(
    *,
    cumulative_cache_read_tokens: int,
    cumulative_prompt_tokens: int,
) -> float | None:
    """Session / subtree cache hit rate: Σ cache_read / Σ prompt.

    Matches the multi-turn prefix-cache model: turn inputs ``a``, ``a+b``,
    ``a+b+c``, … with hits ``0``, ``a``, ``a+b``, … so
    ``m/z = (a + a+b + …) / (a + a+b + a+b+c + …)``.
    """
    if cumulative_prompt_tokens <= 0 or cumulative_cache_read_tokens <= 0:
        return None
    return cumulative_cache_read_tokens / cumulative_prompt_tokens
