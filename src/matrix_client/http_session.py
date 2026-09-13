"""Matrix 链路共享的 aiohttp 会话（惰性单例）。

ClientSession 构造要初始化 SSL 上下文与 connector（Windows 上还同步加载
系统 CA 库，实测可卡事件循环数百毫秒），每请求新建既是性能雷也是卡顿源。
一次性调用处统一从这里取共享会话。

会话必须在运行中的事件循环里首次创建（不要在 import 时建）；跨事件循环
时自动换新。约束：不支持两个活循环并发共用——换新会关闭旧循环上的
会话（含在途请求）；组件如需另起线程循环，请自建会话不要走单例。
"""

from __future__ import annotations

import asyncio
import contextlib

import aiohttp

_session: aiohttp.ClientSession | None = None
_session_loop: asyncio.AbstractEventLoop | None = None


def shared_matrix_http_session() -> aiohttp.ClientSession:
    """取共享 ClientSession；必须在运行中的事件循环里调用。"""
    global _session, _session_loop
    loop = asyncio.get_running_loop()
    if _session is not None and not _session.closed and _session_loop is loop:
        return _session
    old, old_loop = _session, _session_loop
    _session = aiohttp.ClientSession()
    _session_loop = loop
    # 旧循环还活着就把旧会话送回去关闭；循环已死则只能随 GC 回收
    if old is not None and not old.closed and old_loop is not None and not old_loop.is_closed():
        with contextlib.suppress(Exception):
            old_loop.call_soon_threadsafe(old_loop.create_task, old.close())
    return _session
