"""WebSocket connection registry for the embedded web server"""

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
from src.ui.ws_outbox import WsOutbox

if TYPE_CHECKING:
    from aiohttp import web


@dataclass
class WebClientConnection:
    """A single browser WebSocket connection."""

    ws: web.WebSocketResponse
    conn_id: str
    # Pending approval frames keyed by approval_id — for cleanup on disconnect.
    pending_prompts: set[str] = field(default_factory=set)
    # 只读观察者角色（如录像带拖出的独立窗口）：不占 active 坑、不被顶替、
    # 收广播帧但不接定向帧（state/approval/focus 等仍只走 active 主连接）。
    observer: bool = False


class WebSocketRegistry:
    """Registry of connected browser clients"""

    def __init__(self) -> None:
        # OrderedDict 保留注册序：active 连接注销后回切给最近注册的存量连接。
        self._connections: OrderedDict[str, WebClientConnection] = OrderedDict()
        self._active_conn_id: str | None = None
        self._lock = asyncio.Lock()
        # unregister happens outside the WS handler (e.g. send failure).
        self.on_disconnect: Callable[[str], None] | None = None
        # TurnStream 热路径：每连接有界 outbox，满则丢最旧 + need_topup。
        self._outbox = WsOutbox()

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

    async def register(self, ws: web.WebSocketResponse, conn_id: str, *, observer: bool = False) -> WebClientConnection:
        """Register a new connection. 主连接占 active 坑并顶替旧主连接；
        observer（录像带拖出窗等只读端）不占坑、不顶替、不被顶替。"""
        async with self._lock:
            old = None if observer else self.active_connection
            conn = WebClientConnection(ws=ws, conn_id=conn_id, observer=observer)
            self._connections[conn_id] = conn
            if not observer:
                self._active_conn_id = conn_id
                logger.debug(f"WebClient registered: {conn_id} (active)")
            else:
                logger.debug(f"WebClient registered: {conn_id} (observer)")
        # 不自动重连并提示「已在另一标签页打开」，杜绝两标签互顶竞态。
        if old is not None and old.conn_id != conn_id and not old.ws.closed:
            with contextlib.suppress(Exception):
                await old.ws.close(code=4000, message=b"superseded by another tab")
            logger.debug(f"WebClient superseded: {old.conn_id} (closed 4000)")
        return conn

    async def unregister(self, conn_id: str) -> None:
        """Remove a connection. If it was active, fall back to the most recent survivor."""
        self._outbox.clear(conn_id)
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
                # 回切给最近注册的存量主连接：observer（录像带拖出窗）永不上位——
                # 只读连接成 active 会让 send_to_active 的 state/审批/回放全打到拖出窗。
                for cid in reversed(self._connections):
                    if not self._connections[cid].observer:
                        self._active_conn_id = cid
                        break
                else:
                    self._active_conn_id = None
                if self._active_conn_id is not None:
                    logger.debug(f"WebClient active fallback: {self._active_conn_id}")
            logger.debug(f"WebClient unregistered: {conn_id}")

    async def _send_to_conn(self, conn_id: str, message: dict[str, Any]) -> bool:
        """Send JSON to a specific registered connection (active or observer)."""
        conn = self._connections.get(conn_id)
        if conn is None:
            return False
        if conn.ws.closed:
            await self.unregister(conn_id)
            return False
        try:
            await conn.ws.send_str(json.dumps(message, ensure_ascii=False))
            return True
        except (
            ConnectionResetError,
            ClientConnectionResetError,
            RuntimeError,
        ) as exc:
            logger.debug(f"WebClient send failed ({conn_id}): {exc}")
            await self.unregister(conn_id)
            return False

    async def send_to_active(self, message: dict[str, Any]) -> bool:
        """Send a JSON message to the active connection (awaits socket; no outbox)."""
        conn = self.active_connection
        if conn is None:
            return False
        return await self._send_to_conn(conn.conn_id, message)

    def _enqueue_nowait(self, conn_id: str, message: dict[str, Any]) -> None:
        """Enqueue one frame for ``conn_id`` on the bounded outbox."""

        async def _send(msg: dict[str, Any], *, _cid: str = conn_id) -> bool:
            return await self._send_to_conn(_cid, msg)

        self._outbox.enqueue(conn_id, message, _send)

    def send_to_active_nowait(self, message: dict[str, Any]) -> None:
        """有界 outbox：供同步上下文（TurnStream.emit）调用。"""
        conn = self.active_connection
        if conn is None or conn.ws.closed:
            return
        self._enqueue_nowait(conn.conn_id, message)

    def broadcast_observers_nowait(self, message: dict[str, Any]) -> None:
        """有界 outbox：逐 observer 入队，供同步上下文（TurnStream.emit）调用。"""
        for conn in list(self._connections.values()):
            if conn.observer and not conn.ws.closed:
                self._enqueue_nowait(conn.conn_id, message)

    async def broadcast_observers(self, message: dict[str, Any]) -> None:
        """发一帧给全部 observer 连接（与主连接的定向流并行，互不影响）。"""
        for conn in list(self._connections.values()):
            if not conn.observer:
                continue
            # 失败时 _send_to_conn 已 unregister；closed 连接显式清掉
            failed = conn.ws.closed or not await self._send_to_conn(conn.conn_id, message)
            if failed and conn.conn_id in self._connections:
                await self.unregister(conn.conn_id)
