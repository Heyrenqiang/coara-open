"""Browser tab presence — 跨内核重启记住「刚才有 Web 标签开着」"""

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
# 历史兼容：曾用于「抬到 coara 窗后等重连」的满额等待；托盘开页已统一走短探，
# 避免标题残留导致空等约 1s。保留常量供旧测试/外部引用。
RECONNECT_GRACE_SECONDS = 1.0
# 无活 WS 时的重连短探：有标签通常几百毫秒内到位；探不到立刻开新标签。
RECONNECT_PROBE_SECONDS = 0.15

# 决策结果（单一真源，WebServer 与测试共用）
ACTION_FOCUS_ACTIVE = "focus-active"
ACTION_FOCUS_RECENT = "focus-recent"
ACTION_OPEN = "open"


@dataclass(slots=True)
class TabPresence:
    """落盘的标签存在性信号。零值表示「从未见过标签」。"""

    last_seen_at: float = 0.0
    last_bye_at: float = 0.0


def decide_open_action(*, has_active: bool, fresh: bool) -> str:
    """本次「打开 Web UI」该做什么：**只有两种结局**——唤起已有标签 / 开新标签"""
    if has_active:
        return ACTION_FOCUS_ACTIVE
    if fresh:
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
    "RECONNECT_PROBE_SECONDS",
    "TabPresence",
    "decide_open_action",
    "is_tab_fresh",
    "mark_tab_left",
    "mark_tab_seen",
    "presence_file",
    "read_presence",
]
