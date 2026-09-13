"""WebSocket connection registry for the embedded web server.

Tracks connected browser clients and provides broadcast helpers. The
:class:`WebRemoteInteractionChannel` uses this to push approval
prompts to the active browser tab, and the web server uses it to stream
chat chunks and trace events.

Design:
- Single active connection model. coara is a personal assistant — only one
  browser tab drives Root at a time. New connections supersede old ones.
- Thread-safe via asyncio (single event loop, no locks needed).
- Connection lifecycle is managed by the WS handler in ``web_server.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from aiohttp.client_exceptions import ClientConnectionResetError

from src.core.logger import logger

if TYPE_CHECKING:
    from aiohttp import web


@dataclass
class WebClientConnection:
    """A single browser WebSocket connection."""

    ws: web.WebSocketResponse
    conn_id: str
    # Pending approval frames keyed by approval_id — for cleanup on disconnect.
    pending_prompts: set[str] = field(default_factory=set)


class WebSocketRegistry:
    """Registry of connected browser clients.

    coara is single-user: at most one browser tab actively drives Root.
    When a new tab connects, the previous one is marked stale (its pending
    prompts are cancelled). This prevents two tabs from racing on the same
    Root instance.
    """

    def __init__(self) -> None:
        # OrderedDict 保留注册序：active 连接注销后回切给最近注册的存量连接。
        self._connections: OrderedDict[str, WebClientConnection] = OrderedDict()
        self._active_conn_id: str | None = None
        self._lock = asyncio.Lock()
        # Hook invoked with conn_id when a connection is unregistered, so the
        # interaction channel can cancel its pending prompts even when the
        # unregister happens outside the WS handler (e.g. send failure).
        self.on_disconnect: Callable[[str], None] | None = None

    @property
    def active_connection(self) -> WebClientConnection | None:
        if self._active_conn_id is None:
            return None
        return self._connections.get(self._active_conn_id)

    def get_connection(self, conn_id: str) -> WebClientConnection | None:
        """Return the connection for ``conn_id`` (active or stale), if registered."""
        return self._connections.get(conn_id)

    def has_active(self) -> bool:
        return self.active_connection is not None

    async def register(self, ws: web.WebSocketResponse, conn_id: str) -> WebClientConnection:
        """Register a new connection and make it the active one.

        If a previous active connection exists it is explicitly closed with
        code 4000 (superseded): the displaced tab shows a notice and must not
        silently auto-reconnect, otherwise the reconnect race between the two
        tabs would keep evicting whichever tab the user is actually using
        (09-09 审计 P1-2). The displaced connection stays registered until its
        WS handler finally-block unregisters it.
        """
        async with self._lock:
            old = self.active_connection
            conn = WebClientConnection(ws=ws, conn_id=conn_id)
            self._connections[conn_id] = conn
            self._active_conn_id = conn_id
            logger.debug(f"WebClient registered: {conn_id} (active)")
        # 顶掉旧活跃连接：先明确 close(code=4000) 再收新连接——前端凭 4000
        # 不自动重连并提示「已在另一标签页打开」，杜绝两标签互顶竞态。
        # 不 unregister：由旧连接 WS handler 的 finally 走正常注销。
        if old is not None and old.conn_id != conn_id and not old.ws.closed:
            with contextlib.suppress(Exception):
                await old.ws.close(code=4000, message=b"superseded by another tab")
            logger.debug(f"WebClient superseded: {old.conn_id} (closed 4000)")
        return conn

    async def unregister(self, conn_id: str) -> None:
        """Remove a connection. If it was active, fall back to the most recent survivor."""
        async with self._lock:
            conn = self._connections.pop(conn_id, None)
            if conn is None:
                return
            for approval_id in conn.pending_prompts:
                logger.debug(f"WebClient disconnect: held approval {approval_id} (re-deliver on reconnect)")
            if self.on_disconnect is not None:
                with contextlib.suppress(Exception):
                    self.on_disconnect(conn_id)
            if self._active_conn_id == conn_id:
                # 回切给最近注册的存量连接：顶替竞态下「新标签先注册即失格、
                # 旧标签仲裁后自杀」不应让 active 永久空缺（09-09 审计 P1-2）。
                self._active_conn_id = next(reversed(self._connections), None)
                if self._active_conn_id is not None:
                    logger.debug(f"WebClient active fallback: {self._active_conn_id}")
            logger.debug(f"WebClient unregistered: {conn_id}")

    async def send_to_active(self, message: dict[str, Any]) -> bool:
        """Send a JSON message to the active connection.

        Returns True if sent, False if no active connection or send failed.
        On send failure, the connection is automatically unregistered.
        """
        conn = self.active_connection
        if conn is None:
            return False
        # 连接已关闭但 WS handler 的 finally 尚未 unregister：发送前顺手清引用
        if conn.ws.closed:
            await self.unregister(conn.conn_id)
            return False
        try:
            await conn.ws.send_str(json.dumps(message, ensure_ascii=False))
            return True
        except (
            ConnectionResetError,
            ClientConnectionResetError,
            RuntimeError,
        ) as exc:
            logger.debug(f"WebClient send failed ({conn.conn_id}): {exc}")
            await self.unregister(conn.conn_id)
            return False

    def send_to_active_nowait(self, message: dict[str, Any]) -> None:
        """Fire-and-forget 版 send_to_active：供同步上下文（TurnStream.emit）调用。

        在事件循环内调度发送协程；发送失败由 send_to_active 自身注销连接。
        无活跃连接或连接已关闭时静默跳过（事件已入 TurnStream.buffer，重连可回放）。
        """
        conn = self.active_connection
        if conn is None or conn.ws.closed:
            return
        asyncio.ensure_future(self.send_to_active(message))
