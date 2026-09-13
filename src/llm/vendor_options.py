"""Vendor-specific LLM request extras (MiMo / MiniMax / Agnes / DeepSeek / 智谱 thinking, top_p, …).

Per-provider ``max_tokens`` / ``temperature`` / hard caps: ``src.llm.call_defaults``.
"""

from __future__ import annotations

from typing import Any

from src.llm.endpoints import is_deepseek_reasoning_model
from src.llm.thinking_mode import ThinkingLevel, cli_level_override, current_level, is_enabled

__all__ = [
    "agnes_chat_extra_body",
    "deepseek_responses_reasoning",
    "glm_forces_thinking",
    "kimi_messages_kwargs",
    "kimi_openai_chat_kwargs",
    "minimax_messages_kwargs",
    "minimax_prefers_streaming_complete",
    "zhipu_glm_chat_kwargs",
    "zhipu_glm_responses_reasoning",
]


def deepseek_responses_reasoning(
    model: str,
    *,
    thinking_enabled: bool | None = None,
    level: ThinkingLevel | None = None,
) -> dict[str, Any]:
    """Responses API：``deepseek-flash`` 的 ``reasoning.effort``。

    none 关闭思考；低/中/高 → low / high / max（API 默认 high）。
    """
    if not is_deepseek_reasoning_model(model):
        return {}
    active = thinking_enabled if thinking_enabled is not None else is_enabled(kind="deepseek_v4")
    if not active:
        return {"effort": "none"}
    return {"effort": _DEEPSEEK_EFFORT_MAP[level or current_level()]}


# DeepSeek effort mapping（官方：low/high/max；默认 thinking 开启、默认 high）。
# coara 低/中/高 → low / high / max。
_DEEPSEEK_EFFORT_MAP: dict[ThinkingLevel, str] = {"low": "low", "medium": "high", "high": "max"}


# 智谱 GLM：docs.bigmodel.cn — GLM-5.3 / 4.7 强制思考，disabled 会 400。
# coara 低/中/高 → low / high / max；无会话档位覆盖时默认 max（官网 Coding 推荐）。
# 注意（2026-08-16 核对）：
# - GLM-4.5 系列文档明确 thinking.type 支持 enabled/disabled（非强制思考），
#   故前缀表不含 glm-4.5；glm-4.5v 为视觉模型，按厂商约束列入，未单独核实。
# - reasoning_effort 取值（low/high/max）仅在 Responses/Codex 端点（默认配置）
#   有冒烟验证；OpenAI 兼容 chat 端点（paas/v4 + driver: openai 的可选配置）
#   只验证过 thinking.type，reasoning_effort 兼容性未实况确认，若 400 请按
#   官网迁移提示改用 Responses 端点。
_GLM_EFFORT_MAP: dict[ThinkingLevel, str] = {"low": "low", "medium": "high", "high": "max"}
_GLM_FORCE_THINKING_PREFIXES = ("glm-5.3", "glm-4.7", "glm-4.5v")


def _glm_model_id(model: str) -> str:
    """Strip Coding Plan suffixes like ``glm-5.3[1m]``."""
    return (model or "").lower().split("[", 1)[0].strip()


def glm_forces_thinking(model: str) -> bool:
    """Whether this GLM id rejects ``thinking.type: disabled``."""
    mid = _glm_model_id(model)
    return any(mid.startswith(prefix) for prefix in _GLM_FORCE_THINKING_PREFIXES)


def _glm_reasoning_effort(level: ThinkingLevel | None) -> str:
    if level is not None:
        return _GLM_EFFORT_MAP[level]
    override = cli_level_override()
    if override is not None:
        return _GLM_EFFORT_MAP[override]
    # CLI 交互默认 high（增强）：max 深度推理首包极慢，体感像卡死。
    # 需要官网 Coding 推荐的 max 时用 /thinking high。
    return "high"


def zhipu_glm_chat_kwargs(
    model: str,
    *,
    thinking_enabled: bool | None = None,
    level: ThinkingLevel | None = None,
) -> dict[str, Any]:
    """OpenAI-compatible 智谱 GLM extras — thinking + reasoning_effort.

    GLM-5.3 等强制思考模型：永不传 ``disabled``；``/thinking off`` 映射为
    ``enabled`` + ``reasoning_effort=low``（官网迁移提示）。
    """
    mid = _glm_model_id(model)
    if not mid.startswith("glm-"):
        return {}
    active = thinking_enabled if thinking_enabled is not None else is_enabled(kind="zhipu_glm")
    forced = glm_forces_thinking(model)
    if not active:
        if forced:
            return {
                "extra_body": {"thinking": {"type": "enabled"}},
                "reasoning_effort": "low",
            }
        return {"extra_body": {"thinking": {"type": "disabled"}}}
    return {
        "extra_body": {"thinking": {"type": "enabled"}},
        "reasoning_effort": _glm_reasoning_effort(level),
    }


def zhipu_glm_responses_reasoning(
    model: str,
    *,
    thinking_enabled: bool | None = None,
    level: ThinkingLevel | None = None,
) -> dict[str, Any]:
    """Responses API 智谱 GLM 思考控制（Codex / wire_api=responses）。

    官网：Codex 使用 ``reasoning.effort``（low / high / max）；
    关闭思考配置转换为 ``low``，不会传 none/disabled（GLM-5.3 强制思考）。
    交互默认 ``high``（避免 max 深度推理把 CLI 首包拖到体感卡死）；
    ``/thinking high`` → max。见 docs.bigmodel.cn/cn/coding-plan/latest-model 。
    """
    mid = _glm_model_id(model)
    if not mid.startswith("glm-"):
        return {}
    active = thinking_enabled if thinking_enabled is not None else is_enabled(kind="zhipu_glm")
    if not active:
        # 官网：关闭 → low（强制思考模型）；可选关闭的旧模型也统一 low，
        # 避免 Responses 路径误传 none 触发 400。
        return {"effort": "low"}
    return {"effort": _glm_reasoning_effort(level)}


def agnes_chat_extra_body(model: str, *, thinking_enabled: bool | None = None) -> dict[str, Any] | None:
    """Agnes Flash thinking via OpenAI-compatible chat_template_kwargs."""
    active = thinking_enabled if thinking_enabled is not None else is_enabled(kind="agnes_thinking")
    if not active:
        return None
    model_lower = (model or "").lower()
    if not model_lower.startswith("agnes-2."):
        return None
    return {"chat_template_kwargs": {"enable_thinking": True}}


# Kimi K3 effort mapping (docs: medium/high→high, ultra/max/xhigh→max; off → thinking disabled → K2.6).
# coara 低/中/高 → low / high / max（「高」对应服务端 max）.
_KIMI_EFFORT_MAP: dict[ThinkingLevel, str] = {"low": "low", "medium": "high", "high": "max"}


def kimi_messages_kwargs(
    model: str,
    *,
    thinking_enabled: bool | None = None,
    level: ThinkingLevel | None = None,
) -> dict[str, Any]:
    """Anthropic-compatible Kimi Code extras — K3 reasoning_effort / thinking toggle."""
    if not (model or "").lower().startswith("k3"):
        return {}
    active = thinking_enabled if thinking_enabled is not None else is_enabled(kind="kimi_k3")
    if not active:
        return {"thinking": {"type": "disabled"}}
    effort = _KIMI_EFFORT_MAP[level or current_level()]
    return {"extra_body": {"reasoning_effort": effort}}


def kimi_openai_chat_kwargs(model: str, *, level: ThinkingLevel | None = None) -> dict[str, Any]:
    """Kimi Code OpenAI 兼容端（/coding/v1）K3 请求修正。

    - temperature 固定 1.0（端点拒绝其它值：HTTP 400 only 1 is allowed）
    - K3 thinking 常开，reasoning_effort 顶层字段经 extra_body 注入（low/high/max）；
      关闭思考仅 Anthropic 端支持，OpenAI 端不传即默认 max
    """
    extras: dict[str, Any] = {"temperature": 1.0}
    if not (model or "").lower().startswith("k3"):
        return extras
    if is_enabled(kind="kimi_k3"):
        extras["extra_body"] = {"reasoning_effort": _KIMI_EFFORT_MAP[level or current_level()]}
    return extras


def minimax_messages_kwargs(model: str, *, thinking_enabled: bool | None = None) -> dict[str, Any]:
    """Anthropic-compatible MiniMax defaults from platform docs."""
    active = thinking_enabled if thinking_enabled is not None else is_enabled()
    extras: dict[str, Any] = {}
    model_lower = (model or "").lower()
    if model_lower.startswith("minimax-m3"):
        extras["top_p"] = 0.95
        extras["thinking"] = {"type": "adaptive" if active else "disabled"}
    return extras


def minimax_prefers_streaming_complete(model: str, max_tokens: int) -> bool:
    """Use stream aggregation for long generations on compat endpoints (timeout safety)."""
    return max_tokens > 8192 and "highspeed" not in (model or "").lower()
