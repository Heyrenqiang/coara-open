"""Turn / frontend source helpers.

``process_message(..., source=…)`` 和端注入共用这些标签：

- ``cli-attached`` — 外挂 CLI（coara attach）经 /ws/attach 的对话。内核化后
  CLI 全为 attach，是 CLI 端唯一来源；``cli`` 只是历史兼容别名（旧事件/防御），
  新输出一律 ``cli-attached``
- ``web`` — web 端
- ``matrix`` — 手机端（Matrix 房间）
- ``event`` — 外部事件源 auto_run（非用户键盘）
- ``background`` — 后台任务完成自动唤醒的回合（非用户键盘，CLI 无标签镜像）

端显示独立（默认）：正文 / 工具树 / spinner 按注入段分派，各端零镜像。
同空间例外：会话上的生效模型共看该空间的各端 chrome 统一同步（见
``docs/架构契约.md``）。
"""

from __future__ import annotations

from typing import Any

# Values accepted by process_message / remote_sync / matrix delivery.
# ``cli`` 保留兼容识别（历史值），实际 CLI 端来源为 ``cli-attached``。
TURN_SOURCES = frozenset({"cli", "web", "matrix", "event", "background", "cli-attached"})

# Interactive frontends that can launch background shell/agent work.
LAUNCH_SOURCES = frozenset({"cli", "web", "matrix", "cli-attached"})


def normalize_turn_source(raw: str | None) -> str:
    """Normalize a stored or runtime source label."""
    source = str(raw or "").strip().lower()
    if source in TURN_SOURCES:
        return source
    return "cli-attached"


def normalize_launch_source(raw: str | None) -> str:
    """Origin for a newly started background task (never ``event``)."""
    source = normalize_turn_source(raw)
    if source in LAUNCH_SOURCES:
        return source
    return "cli-attached"


def current_turn_source(coara: Any) -> str:
    """Read ``_active_turn_source`` from a running Coara (default ``cli-attached``)."""
    return normalize_launch_source(getattr(coara, "_active_turn_source", None))


def should_push_matrix(source: str) -> bool:
    """True when assistant text must be delivered to the Matrix room."""
    return normalize_turn_source(source) in {"matrix", "event"}


def cli_shows_foreground_spinner(source: str | None) -> bool:
    """Whether the CLI end's Thinking / 前台活动树 should follow this turn.

    三端独立零镜像：端只关心本端发起的回合。仅 cli/cli-attached 回合驱动
    attach 端 spinner/活动树；web/matrix/background/event 回合 attach 不显示。
    """
    s = str(source or "").strip().lower()
    return s in ("cli", "cli-attached")


def cli_shows_source(source: str | None, *, unknown: bool = True) -> bool:
    """CLI 端对来源 source 的输出可见性判定（单一事实源，替代各处硬编码集合）。

    各端显示独立契约：CLI 只显示本端（cli/cli-attached）发起的输出；
    web/matrix/event 的输出回投各自端，CLI 不镜像；background 唤醒回合的
    输出同样不镜像。``unknown=True``（缺省）时空来源按「宁多勿丢」显示
    （兼容无 source 的旧事件/防御）；``unknown=False`` 时空来源不显示
    （严格路由——真本端回合必带 source）。

    纪律：所有 trace 广播事件 payload 必带 ``source``（或 ``origin_source``），
    新输出路径一律查本函数判定 CLI 可见性，端侧不再硬编码来源集合。
    """
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
