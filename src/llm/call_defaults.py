"""Per-provider / per-model best call settings (max_tokens, temperature, hard caps).

Single source of truth used by:
- ``/model`` persistence (``model_persist``)
- profile resolution (``profile_resolver``)
- request-time safety clamps (OpenAI / Anthropic providers)

Precedence for ``resolve_best_call_defaults`` (first hit wins per field):
1. ``providers.yaml`` → ``models.available[].max_tokens|temperature``
2. Built-in per-model table
3. Matching ``llm_profiles`` ``agent.*`` (exact model, else same provider)
4. Built-in per-provider table
5. ``providers.yaml`` provider-level ``max_tokens``

Hard caps (``max_output_cap``) only apply where the vendor API rejects larger values
(e.g. Agnes ≤ 65536). MiniMax and others keep their high practical defaults.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.llm.endpoints import is_agnes_openai_endpoint

# Agnes API hard-rejects max_tokens above this (error code invalid_request).
AGNES_MAX_OUTPUT_TOKENS = 65_536


@dataclass(frozen=True, slots=True)
class ModelCallDefaults:
    """Best-practice call settings for a provider/model pair."""

    max_tokens: int | None = None
    temperature: float | None = None
    max_output_cap: int | None = None


_MODEL_DEFAULTS: dict[str, ModelCallDefaults] = {
    # MiniMax — M3 official output ceiling ~512K. Practical agent defaults:
    "minimax-m3": ModelCallDefaults(131_072, 1.0),
    # Xiaomi MiMo — modest for Token Plan latency
    "mimo-v2.5-pro": ModelCallDefaults(16_384, 0.7),
    "mimo-v2.5": ModelCallDefaults(16_384, 0.7),
    # Agnes Flash — API hard cap 65536
    "agnes-2.5-flash": ModelCallDefaults(16_384, 1.0, AGNES_MAX_OUTPUT_TOKENS),
    # Kimi K3 Coding
    "k3": ModelCallDefaults(131_072, 1.0),
    "k3-256k": ModelCallDefaults(65_536, 1.0),
    # DeepSeek Flash（官方上限 384K output = 393216；思考模式忽略采样参数，temperature 仅非思考生效）
    "deepseek-flash": ModelCallDefaults(393_216, 1.0),
    # LongCat
    "longcat-2.0": ModelCallDefaults(16_384, 0.7),
}

_PROVIDER_DEFAULTS: dict[str, ModelCallDefaults] = {
    "minimax": ModelCallDefaults(131_072, 1.0),
    "xiaomi": ModelCallDefaults(16_384, 0.7),
    "agnes": ModelCallDefaults(16_384, 1.0, AGNES_MAX_OUTPUT_TOKENS),
    "kimi": ModelCallDefaults(131_072, 1.0),
    "longcat": ModelCallDefaults(16_384, 0.7),
    "deepseek": ModelCallDefaults(393_216, 1.0),
}


def _model_key(model: str) -> str:
    return (model or "").lower()


def model_call_defaults(model: str) -> ModelCallDefaults | None:
    """Return built-in best defaults for *model*, or None."""
    return _MODEL_DEFAULTS.get(_model_key(model))


def provider_call_defaults(provider: str) -> ModelCallDefaults | None:
    """Return built-in best defaults for *provider*, or None."""
    return _PROVIDER_DEFAULTS.get((provider or "").lower())


def _yaml_model_entry_defaults(provider_cfg: Any, model: str) -> ModelCallDefaults | None:
    """Optional per-model fields from ``providers.*.models.available[]``."""
    models = getattr(provider_cfg, "models", None) or {}
    available = models.get("available") if isinstance(models, dict) else None
    if not isinstance(available, list):
        return None
    model_lower = _model_key(model)
    for entry in available:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("id") or "").lower() != model_lower:
            continue
        max_tokens = entry.get("max_tokens")
        temperature = entry.get("temperature")
        if max_tokens is None and temperature is None:
            return None
        return ModelCallDefaults(
            int(max_tokens) if max_tokens is not None else None,
            float(temperature) if temperature is not None else None,
        )
    return None


def _profile_defaults_for_target(config_manager: Any, provider: str, model: str) -> ModelCallDefaults | None:
    """Match ``llm_profiles`` agent.* entries for the same provider (+ preferred model)."""
    list_fn = getattr(config_manager, "list_llm_profiles", None)
    get_fn = getattr(config_manager, "get_llm_profile", None)
    if not callable(list_fn) or not callable(get_fn):
        return None
    provider_lower = (provider or "").lower()
    model_lower = _model_key(model)
    exact: ModelCallDefaults | None = None
    provider_only: ModelCallDefaults | None = None
    for name in list_fn():
        if not str(name).startswith("agent."):
            continue
        try:
            profile = get_fn(name)
        except Exception:
            continue  # 单个 profile 读取失败：跳过该项，其余 profile 照常匹配
        if (profile.provider or "").lower() != provider_lower:
            continue
        defaults = ModelCallDefaults(profile.max_tokens, profile.temperature)
        if defaults.max_tokens is None and defaults.temperature is None:
            continue
        if (profile.model or "").lower() == model_lower:
            exact = defaults
            break
        if provider_only is None:
            provider_only = defaults
    return exact or provider_only


def _first_int(*candidates: int | None) -> int | None:
    for value in candidates:
        if value is not None:
            return int(value)
    return None


def _first_float(*candidates: float | None) -> float | None:
    for value in candidates:
        if value is not None:
            return float(value)
    return None


def _resolve_output_cap(
    provider: str,
    model: str,
    *,
    model_builtin: ModelCallDefaults | None,
    provider_builtin: ModelCallDefaults | None,
    base_url: str = "",
) -> int | None:
    for source in (model_builtin, provider_builtin):
        if source and source.max_output_cap is not None:
            return source.max_output_cap
    if (provider or "").lower() == "agnes" or _model_key(model).startswith("agnes-2."):
        return AGNES_MAX_OUTPUT_TOKENS
    if is_agnes_openai_endpoint(base_url):
        return AGNES_MAX_OUTPUT_TOKENS
    return None


def resolve_best_call_defaults(
    provider: str,
    model: str,
    *,
    config_manager: Any | None = None,
    base_url: str = "",
) -> ModelCallDefaults:
    """Resolve best max_tokens/temperature/hard-cap for *provider*/*model*."""
    yaml_model: ModelCallDefaults | None = None
    provider_max: int | None = None
    resolved_base = base_url
    if config_manager is not None:
        try:
            provider_cfg = config_manager.get_provider(provider)
        except Exception:
            provider_cfg = None  # 配置读取回落：有意静默用内置默认（热路径，不记日志）
        if provider_cfg is not None:
            yaml_model = _yaml_model_entry_defaults(provider_cfg, model)
            provider_max = getattr(provider_cfg, "max_tokens", None)
            if not resolved_base:
                resolved_base = getattr(provider_cfg, "base_url", "") or ""

    model_builtin = model_call_defaults(model)
    profile = _profile_defaults_for_target(config_manager, provider, model) if config_manager else None
    provider_builtin = provider_call_defaults(provider)

    max_tokens = _first_int(
        yaml_model.max_tokens if yaml_model else None,
        model_builtin.max_tokens if model_builtin else None,
        profile.max_tokens if profile else None,
        provider_builtin.max_tokens if provider_builtin else None,
        provider_max,
    )
    temperature = _first_float(
        yaml_model.temperature if yaml_model else None,
        model_builtin.temperature if model_builtin else None,
        profile.temperature if profile else None,
        provider_builtin.temperature if provider_builtin else None,
    )
    cap = _resolve_output_cap(
        provider,
        model,
        model_builtin=model_builtin,
        provider_builtin=provider_builtin,
        base_url=resolved_base,
    )
    if max_tokens is not None and cap is not None:
        max_tokens = min(max_tokens, cap)
    return ModelCallDefaults(max_tokens, temperature, cap)


def clamp_max_tokens_for_model(
    model: str,
    max_tokens: int,
    *,
    base_url: str = "",
    provider: str = "",
) -> int:
    """Clamp *max_tokens* to known vendor hard limits (no-op when none apply)."""
    if max_tokens <= 0:
        return max_tokens
    defaults = resolve_best_call_defaults(provider, model, base_url=base_url)
    if defaults.max_output_cap is not None:
        return min(max_tokens, defaults.max_output_cap)
    return max_tokens
