"""Vendor-specific LLM request extras (MiMo / MiniMax / Agnes / DeepSeek / 智谱 thinking, top_p, …)."""

from __future__ import annotations

from typing import Any

from src.llm.endpoints import is_deepseek_reasoning_model
from src.llm.thinking_mode import ThinkingLevel, cli_level_override, current_level, explicit_level, is_enabled

__all__ = [
    "agnes_chat_extra_body",
    "deepseek_responses_reasoning",
    "glm_forces_thinking",
    "kimi_openai_chat_kwargs",
    "minimax_responses_reasoning",
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
    # 无显式档位时用 API 默认 high（配置仅为 on / 未配档位时勿强行 medium→high 映射）
    resolved = level if level is not None else explicit_level()
    if resolved is None:
        return {"effort": "high"}
    return {"effort": _DEEPSEEK_EFFORT_MAP[resolved]}


# DeepSeek effort mapping（官方：low/high/max；默认 thinking 开启、默认 high）。
# coara 低/中/高 → low / high / max。
_DEEPSEEK_EFFORT_MAP: dict[ThinkingLevel, str] = {"low": "low", "medium": "high", "high": "max"}


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


# Kimi K3 effort：coara 低/中/高 → low / high / max（「高」对应服务端 max）。
# /thinking off 只是不传 reasoning_effort；Coding 端默认仍开思考，无法彻底关闭。
_KIMI_EFFORT_MAP: dict[ThinkingLevel, str] = {"low": "low", "medium": "high", "high": "max"}


def kimi_openai_chat_kwargs(model: str, *, level: ThinkingLevel | None = None) -> dict[str, Any]:
    """Kimi / Moonshot OpenAI 兼容端请求修正。

    - ``k3`` / ``kimi-for-coding``：temperature 固定 1.0（Coding 端拒其它值）
    - 其它模型（如开放平台 moonshot-v1）：不强制 temperature
    - K3 思考开：经 extra_body 注入 reasoning_effort（low/high/max）；关则不传档位
    """
    extras: dict[str, Any] = {}
    model_lower = (model or "").lower()
    if model_lower.startswith("k3") or model_lower.startswith("kimi-for-coding"):
        extras["temperature"] = 1.0
    if not model_lower.startswith("k3"):
        return extras
    if is_enabled(kind="kimi_k3"):
        extras["extra_body"] = {"reasoning_effort": _KIMI_EFFORT_MAP[level or current_level()]}
    return extras


def minimax_responses_reasoning(model: str, *, thinking_enabled: bool | None = None) -> dict[str, Any] | None:
    """MiniMax Responses 推理控制（官方 responses-create 文档）。

    M3：省略 reasoning 默认关；effort=none 显式关；minimal/low/medium/high
    均开启推理但不调节深度——开启时按官方兼容取值统一传 low。
    M2.x 推理不可关（effort=none 也被忽略），非 M3 一律不传交给服务端默认。
    """
    if not (model or "").lower().startswith("minimax-m3"):
        return None
    active = thinking_enabled if thinking_enabled is not None else is_enabled(kind="minimax_m3")
    return {"effort": "low" if active else "none"}
