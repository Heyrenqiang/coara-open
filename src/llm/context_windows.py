"""Model context-window sizes (input budget) — not max_tokens (output cap).

Resolution order for ``resolve_context_window``:
1. Per-provider overrides from ``providers.yaml`` ``models.available[].context_window``
2. Built-in ``OPENAI_CONTEXT_WINDOWS`` (factory / known models)
3. Default 128_000
"""

from __future__ import annotations

from typing import Any

# Default when model is unknown (OpenAI-compatible fallback).
DEFAULT_CONTEXT_WINDOW = 128_000

# Keys are lowercase model ids (bracket suffixes stripped before lookup).
OPENAI_CONTEXT_WINDOWS: dict[str, int] = {
    # DeepSeek
    "deepseek-flash": 1_000_000,
    "deepseek-v4-pro": 1_000_000,
    # Kimi Coding
    "k3": 1_048_576,
    "k3-256k": 262_144,
    "kimi-for-coding": 1_048_576,
    "kimi-for-coding-highspeed": 1_048_576,
    # 智谱
    "glm-5.3": 1_000_000,
    "glm-5.3-flash": 1_000_000,
    "glm-5.2": 1_000_000,
    "glm-5": 200_000,
    "glm-5-turbo": 200_000,
    # MiniMax（官方：M3=1M；M2.x=204800）
    "minimax-m3": 1_000_000,
    "minimax-m2.7": 204_800,
    "minimax-m2.7-highspeed": 204_800,
    "minimax-m2.5": 204_800,
    "minimax-m2.5-highspeed": 204_800,
    "minimax-m2.1": 204_800,
    "minimax-m2.1-highspeed": 204_800,
    "minimax-m2": 204_800,
    # Xiaomi MiMo
    "mimo-v2.5-pro": 1_048_576,
    "mimo-v2.5": 1_048_576,
    # Agnes
    "agnes-2.5-flash": 512_000,
    # LongCat
    "longcat-2.0": 1_048_576,
}


def normalize_model_id(model: str | None) -> str:
    """Lowercase id; strip Coding Plan style suffixes like ``glm-5.3[1m]``."""
    raw = (model or "").strip()
    if not raw:
        return ""
    base = raw.split("[", 1)[0].strip()
    return base.lower()


def resolve_context_window(
    model: str | None,
    *,
    overrides: dict[str, int] | None = None,
) -> int:
    """Return context window tokens for *model*."""
    key = normalize_model_id(model)
    if not key:
        return DEFAULT_CONTEXT_WINDOW
    if overrides:
        hit = overrides.get(key)
        if hit is not None and hit > 0:
            return int(hit)
    return OPENAI_CONTEXT_WINDOWS.get(key, DEFAULT_CONTEXT_WINDOW)


def context_windows_from_config(config: Any) -> dict[str, int]:
    """Parse ``models.available[].context_window`` from an LLMProviderConfig."""
    models = getattr(config, "models", None) or {}
    available = models.get("available") or []
    out: dict[str, int] = {}
    if not isinstance(available, list):
        return out
    for entry in available:
        if not isinstance(entry, dict):
            continue
        mid = normalize_model_id(str(entry.get("id") or ""))
        if not mid:
            continue
        raw = entry.get("context_window")
        if raw is None:
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            out[mid] = value
    return out
