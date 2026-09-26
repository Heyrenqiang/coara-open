"""Session-global thinking / reasoning mode for LLM requests.

Models with optional thinking (MiniMax M3) default to **on** (``adaptive``).
MiMo returns ``reasoning_content`` in responses, which is preserved and echoed
back on tool-call turns; there is no request-side thinking parameter for MiMo.
Agnes 2.x Flash uses ``chat_template_kwargs.enable_thinking`` when enabled.
Kimi K3 supports effort levels (low / high / max); the generic 低/中/高 levels
map to low / high / max. Turning thinking off on K3 only omits reasoning_effort
(Coding endpoints still default to thinking on; there is no hard off).
DeepSeek ``deepseek-flash`` defaults to thinking on (effort ``high``) via
Responses ``reasoning.effort`` (none / low / high / max). Tool-call turns must
echo ``reasoning_content`` back or the API returns 400.
智谱 GLM（OpenAI 兼容）：显式传 ``thinking`` + ``reasoning_effort``；GLM-5.3 /
4.7 强制思考，``/thinking off`` 降为 ``low`` 而非 ``disabled``（否则 400）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from src.llm.endpoints import (
    is_agnes_openai_endpoint,
    is_deepseek_openai_endpoint,
    is_deepseek_reasoning_model,
    is_kimi_openai_endpoint,
    is_mimo_openai_endpoint,
    is_minimax_endpoint,
    is_zhipu_openai_endpoint,
)

_cli_override: bool | None = None
DEFAULT_ENABLED = True

ThinkingKind = Literal[
    "minimax_m3",
    "mimo_tools",
    "agnes_thinking",
    "kimi_k3",
    "deepseek_v4",
    "zhipu_glm",
    "none",
]
ThinkingLevel = Literal["low", "medium", "high"]

DEFAULT_LEVEL: ThinkingLevel = "medium"
_LEVEL_ALIASES: dict[str, ThinkingLevel] = {
    "low": "low",
    "低": "low",
    "medium": "medium",
    "mid": "medium",
    "中": "medium",
    "high": "high",
    "高": "high",
}
_LEVEL_LABELS: dict[ThinkingLevel, str] = {"low": "低", "medium": "中", "high": "高"}

_cli_level_override: ThinkingLevel | None = None

# 持久化思考档位的合法取值（配置页 / 配置文件）
_PERSIST_VALUES = frozenset({"on", "off", "low", "medium", "high"})

# 优先级：/thinking 会话覆盖 > 本基线 > 厂商默认
_config_enabled: bool | None = None
_config_level: ThinkingLevel | None = None


def load_persisted_thinking(raw: str) -> None:
    """装载持久化的思考档位（on/off/low/medium/high）；非法值静默忽略。"""
    global _config_enabled, _config_level
    value = str(raw or "").strip().lower()
    _config_enabled = None
    _config_level = None
    if value not in _PERSIST_VALUES:
        return
    if value == "on":
        _config_enabled = True
    elif value == "off":
        _config_enabled = False
    else:
        _config_enabled = True
        _config_level = _LEVEL_ALIASES.get(value)


def yaml_model_thinking(config_manager: Any, provider: str, model: str) -> str:
    """读 ``providers.*.models.available[]`` 里该模型的 ``thinking`` 字段；无则空串。"""
    if not provider or not model:
        return ""
    get_provider = getattr(config_manager, "get_provider", None)
    if not callable(get_provider):
        return ""
    try:
        cfg = get_provider(provider)
    except Exception:
        return ""
    models = getattr(cfg, "models", None) or {}
    available = models.get("available") if isinstance(models, dict) else None
    if not isinstance(available, list):
        return ""
    model_lower = str(model).strip().lower()
    for entry in available:
        if isinstance(entry, str):
            if entry.strip().lower() == model_lower:
                return ""
            continue
        if not isinstance(entry, dict):
            continue
        if str(entry.get("id") or "").strip().lower() != model_lower:
            continue
        raw = str(entry.get("thinking") or "").strip().lower()
        return raw if raw in _PERSIST_VALUES else ""
    return ""


def agent_main_thinking(config_manager: Any) -> str:
    """兼容旧路径：``llm_profiles.agent.main.thinking``。"""
    get_fn = getattr(config_manager, "get_llm_profile", None)
    if not callable(get_fn):
        return ""
    try:
        from src.llm.profiles import Profile

        profile = get_fn(Profile.AGENT_MAIN)
    except Exception:
        return ""
    raw = str(getattr(profile, "thinking", "") or "").strip().lower()
    return raw if raw in _PERSIST_VALUES else ""


def resolve_config_thinking(config_manager: Any, provider: str, model: str) -> str:
    """模型条目优先，其次 agent.main；都无则空（厂商默认）。"""
    from_model = yaml_model_thinking(config_manager, provider, model)
    if from_model:
        return from_model
    return agent_main_thinking(config_manager)


def apply_config_thinking(config_manager: Any, provider: str, model: str) -> str:
    """按当前 provider/model 装载思考基线；返回实际装载的字符串。"""
    value = resolve_config_thinking(config_manager, provider, model)
    load_persisted_thinking(value)
    return value


def persisted_thinking_value() -> str:
    """当前配置基线的字符串形式（on/off/low/medium/high），未配置返回空串。"""
    if _config_enabled is None:
        return ""
    if not _config_enabled:
        return "off"
    return _config_level or "on"


def set_cli_thinking_enabled(enabled: bool | None) -> None:
    """Set thinking mode from chat ``/thinking on|off`` (``None`` clears override)."""
    global _cli_override
    _cli_override = enabled


def set_cli_thinking_level(level: ThinkingLevel | None) -> None:
    """Set thinking effort level from chat ``/thinking low|medium|high`` (``None`` clears)."""
    global _cli_level_override
    _cli_level_override = level


def cli_level_override() -> ThinkingLevel | None:
    """Session ``/thinking`` level override, or ``None`` when using vendor default."""
    return _cli_level_override


def config_level() -> ThinkingLevel | None:
    """配置基线档位（low/medium/high）；``on``/``off``/未配置时为 None。"""
    return _config_level


def explicit_level() -> ThinkingLevel | None:
    """会话或配置里**显式**选定的档位；未指定时返回 None（交给厂商默认）。"""
    if _cli_level_override is not None:
        return _cli_level_override
    return _config_level


def current_level() -> ThinkingLevel:
    """Effective thinking effort level (session override > 配置基线 > 默认).

    注意：配置仅为 ``on``（开但不指定档位）时这里仍回落 ``medium``，供 UI/通用路径；
    厂商请求应优先用 :func:`explicit_level`，无显式档位时走各自 API 默认。
    """
    if _cli_level_override is not None:
        return _cli_level_override
    if _config_level is not None:
        return _config_level
    return DEFAULT_LEVEL


def level_label(level: ThinkingLevel | None = None) -> str:
    return _LEVEL_LABELS[level or current_level()]


def supports_levels(kind: ThinkingKind) -> bool:
    """Kimi K3 / DeepSeek V4 / 智谱 GLM expose effort levels; other kinds are on/off only."""
    return kind in ("kimi_k3", "deepseek_v4", "zhipu_glm")


def is_enabled(*, kind: ThinkingKind | None = None) -> bool:
    if _cli_override is not None:
        return _cli_override
    if _config_enabled is not None:
        return _config_enabled
    # Agnes: opt-in — enable_thinking often yields empty visible content on short turns.
    if kind == "agnes_thinking":
        return False
    return DEFAULT_ENABLED


def reset_for_tests() -> None:
    global _cli_override, _cli_level_override, _config_enabled, _config_level
    _cli_override = None
    _cli_level_override = None
    _config_enabled = None
    _config_level = None


def classify_thinking_support(
    model: str,
    *,
    base_url: str = "",
    driver: str = "",
) -> ThinkingKind:
    model_lower = (model or "").lower()
    if driver == "openai" and is_kimi_openai_endpoint(base_url) and model_lower.startswith("k3"):
        return "kimi_k3"
    if driver == "responses" and is_minimax_endpoint(base_url) and model_lower.startswith("minimax-m3"):
        return "minimax_m3"
    if driver == "openai" and is_mimo_openai_endpoint(base_url):
        return "mimo_tools"
    if driver == "openai" and is_agnes_openai_endpoint(base_url) and model_lower.startswith("agnes-2."):
        return "agnes_thinking"
    if driver == "responses" and is_deepseek_openai_endpoint(base_url) and is_deepseek_reasoning_model(model_lower):
        return "deepseek_v4"
    if driver in ("openai", "responses") and is_zhipu_openai_endpoint(base_url) and model_lower.startswith("glm-"):
        return "zhipu_glm"
    return "none"


def _thinking_source_label(*, kind: ThinkingKind) -> str:
    """用户可见的生效来源：本会话 > 配置基线 > 厂商默认。"""
    if _cli_override is not None or _cli_level_override is not None:
        return "本会话"
    if _config_enabled is not None:
        if not _config_enabled:
            return "配置关闭"
        if _config_level is not None:
            return f"配置·{_LEVEL_LABELS[_config_level]}"
        return "配置开启"
    if kind == "agnes_thinking":
        return "默认关闭"
    return "默认开启"


def describe_thinking_status(
    model: str,
    *,
    base_url: str = "",
    driver: str = "",
) -> tuple[bool, str, str]:
    """Return ``(effective_enabled, source_label, model_note)``."""
    kind = classify_thinking_support(model, base_url=base_url, driver=driver)
    enabled = is_enabled(kind=kind)
    source = _thinking_source_label(kind=kind)
    if kind == "none":
        note = "当前模型不支持思考模式切换"
    elif kind == "minimax_m3":
        note = "MiniMax M3：思考已开（自适应）" if enabled else "MiniMax M3：思考已关"
    elif kind == "agnes_thinking":
        note = "Agnes：思考已开" if enabled else "Agnes：思考已关"
    elif kind == "kimi_k3":
        note = (
            f"Kimi K3：思考已开（档位：{level_label()}）"
            if enabled
            else "Kimi K3：思考已关（本会话不传档位；Coding 端默认仍会开思考）"
        )
    elif kind == "deepseek_v4":
        if enabled:
            lvl = explicit_level()
            note = (
                f"DeepSeek Flash：思考已开（档位：{level_label(lvl)}）"
                if lvl is not None
                else "DeepSeek Flash：思考已开（厂商默认高档）"
            )
        else:
            note = "DeepSeek Flash：思考已关"
    elif kind == "zhipu_glm":
        from src.llm.vendor_options import glm_forces_thinking

        if enabled:
            lvl = level_label() if _cli_level_override is not None else "high（交互默认；/thinking high → max）"
            note = f"智谱 GLM：思考已开（档位：{lvl}）"
        elif glm_forces_thinking(model):
            note = "智谱 GLM：已降为低档（本模型不可关闭思考，不会传 disabled）"
        else:
            note = "智谱 GLM：思考已关"
    else:
        note = "MiMo：思考内容随响应返回并保留（开关不改变 MiMo 请求）"
    return enabled, source, note


@dataclass(frozen=True, slots=True)
class ThinkingCommandResult:
    """Plain-text lines for CLI or Matrix (/thinking)."""

    lines: tuple[str, ...]


_USAGE = "用法：/thinking ｜ on ｜ off ｜ toggle ｜ low ｜ medium ｜ high"


def run_thinking_command(
    raw: str,
    *,
    model: str,
    base_url: str = "",
    driver: str = "",
    provider_name: str = "",
) -> ThinkingCommandResult:
    """Parse ``/thinking`` and apply session toggle/level; return display lines."""
    parts = raw.strip().split()
    ctx = {"model": model, "base_url": base_url, "driver": driver}

    if len(parts) == 1:
        enabled, source, note = describe_thinking_status(**ctx)
        state = "开" if enabled else "关"
        lines = [f"思考模式：{state}（{source}）"]
        if provider_name:
            lines.append(f"当前模型：{provider_name} / {model}")
        lines.extend([note, _USAGE])
        return ThinkingCommandResult(tuple(lines))

    arg = parts[1].lower()
    level = _LEVEL_ALIASES.get(arg)
    if level is not None:
        set_cli_thinking_enabled(True)
        set_cli_thinking_level(level)
        kind = classify_thinking_support(model, base_url=base_url, driver=driver)
        _, _, note = describe_thinking_status(**ctx)
        line = f"思考模式：开（本会话）· 档位：{_LEVEL_LABELS[level]}"
        if not supports_levels(kind):
            line += "（当前模型不支持档位，仅开关生效）"
        return ThinkingCommandResult((line, note))
    if arg == "toggle":
        current, _, _ = describe_thinking_status(**ctx)
        set_cli_thinking_enabled(not current)
        enabled, _, note = describe_thinking_status(**ctx)
        kind = classify_thinking_support(model, base_url=base_url, driver=driver)
        if not enabled and kind == "zhipu_glm":
            from src.llm.vendor_options import glm_forces_thinking

            if glm_forces_thinking(model):
                return ThinkingCommandResult(("思考模式：降为低档（本会话）— 不可关闭思考", note))
        state = "开" if enabled else "关"
        return ThinkingCommandResult((f"思考模式：{state}（本会话）", note))
    if arg == "on":
        set_cli_thinking_enabled(True)
        _, _, note = describe_thinking_status(**ctx)
        return ThinkingCommandResult(("思考模式：开（本会话）", note))
    if arg == "off":
        set_cli_thinking_enabled(False)
        _, _, note = describe_thinking_status(**ctx)
        kind = classify_thinking_support(model, base_url=base_url, driver=driver)
        if kind == "zhipu_glm":
            from src.llm.vendor_options import glm_forces_thinking

            if glm_forces_thinking(model):
                return ThinkingCommandResult(("思考模式：降为低档（本会话）— 不可关闭思考", note))
        return ThinkingCommandResult(("思考模式：关（本会话）", note))

    return ThinkingCommandResult((_USAGE,))
