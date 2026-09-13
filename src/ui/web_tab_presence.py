"""Browser tab presence — 跨内核重启记住「刚才有 Web 标签开着」。

为什么需要它：打开/唤起 Web UI 的判定本来只看进程内的活跃 WebSocket 连接
（``WebSocketRegistry.has_active``）。内核重启丢掉内存态、后台标签的 WS 又被浏览器
节流，于是「已有标签」被误判成「没有标签」→ webbrowser.open 新开标签，而新标签
立刻被 SingleTabGuard 判定为后来者自我关闭 —— 用户看到「开一下又关掉」。

这里把「最近一次有标签」落盘（工作空间级），并记下「最近一次打开尝试」：

- 信号来源：WS 连接建立、前端 HTTP 心跳、标签关闭时的 bye（``navigator.sendBeacon``）
- 判定：``last_seen_at`` 晚于 ``last_bye_at`` 且在 TTL 内 → 认为标签还在
- 二次点击：同一次打开窗口内再点一下，直接开新窗（真的关了浏览器时的兜底）

文件位置：``<workspace_home>/runtime/web_tab_presence.json``
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from src.core.coara_home import CoaraHomePaths

# 标签信号有效期：前端可见时 30s 一次心跳，允许丢两拍。
FRESH_TTL_SECONDS = 90.0
# 「再点一下」窗口：这段时间内的第二次打开请求直接开新窗（标签确实不在时的兜底）。
SECOND_CLICK_WINDOW_SECONDS = 15.0
# 判定「标签还在」后，等它重连/被唤醒的时长。
RECONNECT_GRACE_SECONDS = 1.5

# 决策结果（单一真源，WebServer 与测试共用）
ACTION_FOCUS_ACTIVE = "focus-active"
ACTION_FOCUS_RECENT = "focus-recent"
ACTION_OPEN = "open"


@dataclass(slots=True)
class TabPresence:
    """落盘的标签存在性信号。零值表示「从未见过标签」。"""

    last_seen_at: float = 0.0
    last_bye_at: float = 0.0
    last_open_attempt_at: float = 0.0


def decide_open_action(*, has_active: bool, fresh: bool, second_click: bool) -> str:
    """本次「打开 Web UI」该做什么：活跃连接→唤起；刚有标签→唤起；否则开新窗。"""
    if has_active:
        return ACTION_FOCUS_ACTIVE
    if fresh and not second_click:
        return ACTION_FOCUS_RECENT
    return ACTION_OPEN


def presence_file(workspace_dir: str | Path, *, coara_home: str | Path | None = None) -> Path:
    """presence 落盘位置：跟随工作空间的 runtime 目录。"""
    paths = CoaraHomePaths.for_workspace(workspace_dir, configured_home=coara_home, migrate=False)
    return paths.workspace_home / "runtime" / "web_tab_presence.json"


def read_presence(workspace_dir: str | Path, *, coara_home: str | Path | None = None) -> TabPresence:
    """读信号；文件缺失或损坏时返回零值（不抛）。"""
    path = presence_file(workspace_dir, coara_home=coara_home)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return TabPresence()
    if not isinstance(raw, dict):
        return TabPresence()
    return TabPresence(
        last_seen_at=_as_float(raw.get("last_seen_at")),
        last_bye_at=_as_float(raw.get("last_bye_at")),
        last_open_attempt_at=_as_float(raw.get("last_open_attempt_at")),
    )


def mark_tab_seen(
    workspace_dir: str | Path,
    *,
    coara_home: str | Path | None = None,
    now: float | None = None,
) -> TabPresence:
    """标签报到（WS 连接 / HTTP 心跳）。"""
    presence = read_presence(workspace_dir, coara_home=coara_home)
    presence.last_seen_at = time.time() if now is None else now
    _write_presence(workspace_dir, presence, coara_home=coara_home)
    return presence


def mark_tab_left(
    workspace_dir: str | Path,
    *,
    coara_home: str | Path | None = None,
    now: float | None = None,
) -> TabPresence:
    """标签关闭（bye）。只记 bye 时间，不抹掉 last_seen：多标签时另一个标签的
    心跳会让 ``last_seen_at > last_bye_at``，仍判定为「标签还在」。"""
    presence = read_presence(workspace_dir, coara_home=coara_home)
    presence.last_bye_at = time.time() if now is None else now
    _write_presence(workspace_dir, presence, coara_home=coara_home)
    return presence


def note_open_attempt(
    workspace_dir: str | Path,
    *,
    coara_home: str | Path | None = None,
    now: float | None = None,
) -> tuple[bool, TabPresence]:
    """记录一次「打开/唤起」尝试。返回 (是否窗口内的二次点击, 更新后的信号)。"""
    moment = time.time() if now is None else now
    presence = read_presence(workspace_dir, coara_home=coara_home)
    second_click = presence.last_open_attempt_at > 0.0 and (
        moment - presence.last_open_attempt_at <= SECOND_CLICK_WINDOW_SECONDS
    )
    presence.last_open_attempt_at = moment
    _write_presence(workspace_dir, presence, coara_home=coara_home)
    return second_click, presence


def is_tab_fresh(presence: TabPresence, *, now: float | None = None) -> bool:
    """标签是否算「还在」：最近报过到、且最后一次 bye 之后又报过到、且未过期。"""
    moment = time.time() if now is None else now
    if presence.last_seen_at <= 0.0:
        return False
    if presence.last_seen_at <= presence.last_bye_at:
        return False
    return (moment - presence.last_seen_at) <= FRESH_TTL_SECONDS


def _write_presence(
    workspace_dir: str | Path,
    presence: TabPresence,
    *,
    coara_home: str | Path | None = None,
) -> None:
    """原子写：先落临时文件再 replace，避免读到半截 JSON。"""
    path = presence_file(workspace_dir, coara_home=coara_home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(asdict(presence)), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        return


def _as_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


__all__ = [
    "ACTION_FOCUS_ACTIVE",
    "ACTION_FOCUS_RECENT",
    "ACTION_OPEN",
    "FRESH_TTL_SECONDS",
    "RECONNECT_GRACE_SECONDS",
    "SECOND_CLICK_WINDOW_SECONDS",
    "TabPresence",
    "decide_open_action",
    "is_tab_fresh",
    "mark_tab_left",
    "mark_tab_seen",
    "note_open_attempt",
    "presence_file",
    "read_presence",
]
