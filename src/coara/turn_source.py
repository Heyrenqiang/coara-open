"""Turn / frontend source helpers."""

from __future__ import annotations

from typing import Any

# Values accepted by process_message / remote_sync / matrix delivery.
# 纯三端归属 + cli 历史兼容别名；event/background 曾是「非用户键盘」来源标签，
TURN_SOURCES = frozenset({"cli", "web", "matrix", "cli-attached"})

# Interactive frontends that can launch background shell/agent work.
LAUNCH_SOURCES = frozenset({"cli", "web", "matrix", "cli-attached"})


def normalize_turn_source(raw: str | None) -> str:
    """Normalize a stored or runtime source label."""
    source = str(raw or "").strip().lower()
    if source in TURN_SOURCES:
        return source
    return "cli-attached"


def normalize_launch_source(raw: str | None) -> str:
    """Origin for a newly started background task（恒为三端之一）。"""
    source = normalize_turn_source(raw)
    if source in LAUNCH_SOURCES:
        return source
    return "cli-attached"


def current_turn_source(coara: Any) -> str:
    """Read ``_active_turn_source`` from a running Coara (default ``cli-attached``)."""
    return normalize_launch_source(getattr(coara, "_active_turn_source", None))


# 原 should_push_matrix 判据已删：子智能体结果不再由注入路径直推房间；
# 显示面统一走 delegate 的帧路由（命中投递）与按端兜底（未命中落地）。


def cli_shows_foreground_spinner(source: str | None) -> bool:
    """Whether the CLI end's Thinking / 前台活动树 should follow this turn. 三端独立零镜像：端只关心本端发起的回合"""
    s = str(source or "").strip().lower()
    return s in ("cli", "cli-attached")


def cli_shows_source(source: str | None, *, unknown: bool = True) -> bool:
    """CLI 端对来源 source 的输出可见性判定（单一事实源，替代各处硬编码集合）"""
    s = str(source or "").strip().lower()
    if not s:
        return unknown
    return s in ("cli", "cli-attached") or s.startswith("cli-")


def web_shows_source(source: str | None, *, unknown: bool = True) -> bool:
    """浏览器端可见性：只收 web 段；严格路由时用 ``unknown=False``。"""
    s = str(source or "").strip().lower()
    if not s:
        return unknown
    return s == "web" or s.startswith("web-")


def resolve_trace_end_source(payload: dict[str, Any] | None) -> str:
    """从 trace payload 取端归属标签（发送端过滤用）。

    优先级：``source`` → ``turn_source`` → ``origin_source`` → ``subagent_origin``。
    """
    data = payload or {}
    for key in ("source", "turn_source", "origin_source", "subagent_origin"):
        value = str(data.get(key) or "").strip()
        if value:
            return value
    return ""


# cli- 前缀）是有意的分派行为，见该文件注释。


def turn_source_family(source: str | None) -> str:
    """来源标签的端族：cli / web / matrix；无法归类返回 ""。"""
    s = str(source or "").strip().lower()
    if s in ("cli", "cli-attached") or s.startswith("cli-"):
        return "cli"
    if s == "web" or s.startswith("web-"):
        return "web"
    if s == "matrix":
        return "matrix"
    return ""


def same_turn_family(a: str | None, b: str | None) -> bool:
    """两个来源标签是否同一端族；任一方无族判 False。"""
    fa = turn_source_family(a)
    return bool(fa) and fa == turn_source_family(b)
