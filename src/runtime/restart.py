"""内核重启意图：/restart → 优雅收尾 → exit(EXIT_RESTART)，由 supervisor respawn。

supervisor 模式（src/cli/supervisor.py）下，内核不再自己 spawn 替代者——
「进程自己替换自己」的交棒协议已整体废弃（intent/ready/rollback 三文件、
yield/resume 端口让位、健康轮询，全部删除）。内核只剩一件事：收到重启意图后
走与信号退出完全相同的优雅收尾路径，然后以 EXIT_RESTART 退出码结束。
sync 游标、用量、trace、实例锁、端口都由既有 shutdown 链自然收尾——
没有需要单独记得 flush 的链（旧模型连环三次事故的根源正是这一面）。

本模块只承载重启意图的传递：
- bind_restart_stop_event：启动时把停止事件与事件循环注册进来
- request_restart：/restart 处理器调用，记意图并唤醒停止事件
- consume_restart_flag：优雅收尾完成后由入口读取，决定是否以 EXIT_RESTART 退出
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from pathlib import Path
from typing import Any

from src.core.json_store import write_json_atomic
from src.core.logger import logger

# 进程内重启意图标志与停止事件：/restart 处理器与 _run_frontends 同进程。
_restart_requested = False
_stop_event: asyncio.Event | None = None
_stop_loop: asyncio.AbstractEventLoop | None = None


def bind_restart_stop_event(stop_event: asyncio.Event, loop: asyncio.AbstractEventLoop) -> None:
    """内核启动时注册停止事件；/restart 意图将经它走既有优雅收尾路径。"""
    global _stop_event, _stop_loop, _restart_requested
    _stop_event = stop_event
    _stop_loop = loop
    _restart_requested = False


def request_restart(root: Any, *, reason: str, requested_by: str, home: Path | None = None) -> None:
    """记录重启意图并唤醒优雅收尾。幂等：重复调用只保留首个意图。"""
    global _restart_requested
    _write_restart_record(home, reason=reason, requested_by=requested_by, root=root)
    _restart_requested = True
    if _stop_event is not None and _stop_loop is not None:
        with contextlib.suppress(Exception):
            _stop_loop.call_soon_threadsafe(_stop_event.set)


def consume_restart_flag() -> bool:
    """优雅收尾完成后读取并清除意图标志；True = 本次退出应以 EXIT_RESTART 结束。"""
    global _restart_requested
    flag = _restart_requested
    _restart_requested = False
    return flag


def wake_stop_event() -> bool:
    """唤醒已绑定的停止事件（supervisor 托盘退出的内核侧通道）。未绑定返回 False。"""
    if _stop_event is None or _stop_loop is None:
        return False
    with contextlib.suppress(Exception):
        _stop_loop.call_soon_threadsafe(_stop_event.set)
        return True
    return False


def _write_restart_record(home: Path | None, *, reason: str, requested_by: str, root: Any) -> None:
    """重启交接记录：谁在什么时候因为什么重启了内核 + 发起时的空间/会话。

    诊断之外兼作「重启成功分割线」的交接载体——旧进程退场时记下视图归属，
    新内核就绪后经 :func:`consume_restart_divider` 推手机事件 + 落 web divider 帧。
    """
    if home is None:
        return
    try:
        workspace_dir = ""
        session_id = ""
        try:
            view = root.resolve_web_view_coara()
            workspace_dir = str(getattr(view, "workspace_dir", "") or "")
            session_id = str(getattr(view, "session_id", "") or "")
        except Exception:
            fg = getattr(root, "foreground_coara", None)
            workspace_dir = str(getattr(fg, "workspace_dir", "") or "")
            session_id = str(getattr(fg, "session_id", "") or "")
        record = {
            "requested_at": time.time(),
            "reason": reason,
            "requested_by": requested_by,
            "pid": __import__("os").getpid(),
            "workspace_dir": workspace_dir,
            "session_id": session_id,
        }
        path = home / "restart_last.json"
        write_json_atomic(path, record)
    except Exception:
        logger.debug("restart record write skipped", exc_info=True)


def consume_restart_divider(home: Path | None) -> dict[str, str] | None:
    """启动期消费重启交接：有未消费的 /restart 记录则返回其归属并标记已消费。

    幂等——记录打上 ``divider_done`` 后不再重复画线（防 supervisor respawn 循环
    或反复启动时刷线）。非 /restart 退出（信号/托盘/崩溃）无记录，自然不会画。
    """
    if home is None:
        return None
    path = home / "restart_last.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, dict) or raw.get("divider_done"):
        return None
    # 时效闸：/restart 后新内核本该数秒内就绪；超窗才消费说明那次重启实际失败
    # （respawn 崩退/被叫停），本次是冷启动，不该画「已重启」线。
    try:
        requested_at = float(raw.get("requested_at") or 0)
    except (TypeError, ValueError):
        requested_at = 0.0
    expired = requested_at <= 0 or (time.time() - requested_at) > 300
    try:
        raw["divider_done"] = True
        write_json_atomic(path, raw)
    except Exception:
        return None
    if expired:
        return None
    return {
        "workspace_dir": str(raw.get("workspace_dir") or ""),
        "session_id": str(raw.get("session_id") or ""),
    }
