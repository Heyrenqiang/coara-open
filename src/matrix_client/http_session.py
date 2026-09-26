"""Matrix 链路共享的 aiohttp 会话（惰性单例）"""

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
