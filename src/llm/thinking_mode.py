"""Session-global thinking / reasoning mode for LLM requests.

Models with optional thinking (MiniMax M3) default to **on** (``adaptive``).
MiMo returns ``reasoning_content`` in responses, which is preserved and echoed
back on tool-call turns; there is no request-side thinking parameter for MiMo.
Agnes 2.x Flash uses ``chat_template_kwargs.enable_thinking`` when enabled.
Kimi K3 supports effort levels (low / high / max); the generic 低/中/高 levels
map to low / high / max. Turning thinking off on K3 routes requests to K2.6.
DeepSeek ``deepseek-flash`` defaults to thinking on (effort ``high``) via
Responses ``reasoning.effort`` (none / low / high / max). Tool-call turns must
echo ``reasoning_content`` back or the API returns 400.
智谱 GLM（OpenAI 兼容）：显式传 ``thinking`` + ``reasoning_effort``；GLM-5.3 /
4.7 强制思考，``/thinking off`` 降为 ``low`` 而非 ``disabled``（否则 400）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from src.llm.endpoints import (
    is_agnes_openai_endpoint,
    is_deepseek_openai_endpoint,
    is_deepseek_reasoning_model,
    is_kimi_anthropic_endpoint,
    is_mimo_openai_endpoint,
    is_minimax_anthropic_endpoint,
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


def current_level() -> ThinkingLevel:
    """Effective thinking effort level (session override or default)."""
    return _cli_level_override or DEFAULT_LEVEL


def level_label(level: ThinkingLevel | None = None) -> str:
    return _LEVEL_LABELS[level or current_level()]


def supports_levels(kind: ThinkingKind) -> bool:
    """Kimi K3 / DeepSeek V4 / 智谱 GLM expose effort levels; other kinds are on/off only."""
    return kind in ("kimi_k3", "deepseek_v4", "zhipu_glm")


def is_enabled(*, kind: ThinkingKind | None = None) -> bool:
    if _cli_override is not None:
        return _cli_override
    # Agnes: opt-in — enable_thinking often yields empty visible content on short turns.
    if kind == "agnes_thinking":
        return False
    return DEFAULT_ENABLED


def reset_for_tests() -> None:
    global _cli_override, _cli_level_override
    _cli_override = None
    _cli_level_override = None


def classify_thinking_support(
    model: str,
    *,
    base_url: str = "",
    driver: str = "",
) -> ThinkingKind:
    model_lower = (model or "").lower()
    if is_kimi_anthropic_endpoint(base_url) and model_lower.startswith("k3"):
        return "kimi_k3"
    if (driver == "anthropic" or is_minimax_anthropic_endpoint(base_url)) and model_lower.startswith("minimax-m3"):
        return "minimax_m3"
    if driver == "openai" and is_mimo_openai_endpoint(base_url):
        return "mimo_tools"
    if driver == "openai" and is_agnes_openai_endpoint(base_url) and model_lower.startswith("agnes-2."):
        return "agnes_thinking"
    if (
        driver == "responses"
        and is_deepseek_openai_endpoint(base_url)
        and is_deepseek_reasoning_model(model_lower)
    ):
        return "deepseek_v4"
    if driver in ("openai", "responses") and is_zhipu_openai_endpoint(base_url) and model_lower.startswith("glm-"):
        return "zhipu_glm"
    return "none"


def describe_thinking_status(
    model: str,
    *,
    base_url: str = "",
    driver: str = "",
) -> tuple[bool, str, str]:
    """Return ``(effective_enabled, source_label, model_note)``."""
    kind = classify_thinking_support(model, base_url=base_url, driver=driver)
    enabled = is_enabled(kind=kind)
    source = "本会话" if _cli_override is not None else ("默认关闭" if kind == "agnes_thinking" else "默认开启")
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
            else "Kimi K3：思考已关（关闭后请求会被路由到 K2.6）"
        )
    elif kind == "deepseek_v4":
        note = f"DeepSeek Flash：思考已开（档位：{level_label()}，默认高档）" if enabled else "DeepSeek Flash：思考已关"
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
