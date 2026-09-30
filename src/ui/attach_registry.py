"""外挂 CLI（coara attach）连接注册表"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from aiohttp.client_exceptions import ClientConnectionResetError

from src.coara.output_router import EndRoute
from src.coara.workspace_occupancy import WorkspaceOccupancy
from src.core.logger import logger
from src.ui.ws_outbox import WsOutbox

if TYPE_CHECKING:
    from aiohttp import web


@dataclass
class AttachConnection:
    """一条外挂 CLI 的 WebSocket 连接。"""

    ws: web.WebSocketResponse
    conn_id: str
    workspace_id: str


class AttachRegistry:
    """外挂 CLI 连接注册表（连接生命周期）+ 经 WorkspaceOccupancy 登记挂载"""

    def __init__(self, occupancy: WorkspaceOccupancy | None = None) -> None:
        self._connections: dict[str, AttachConnection] = {}
        # 工作空间↔端占用唯一事实源（共享表；缺省私有，web_server 注入共享实例）
        self.occupancy = occupancy if occupancy is not None else WorkspaceOccupancy()
        self._lock = asyncio.Lock()
        # TurnStream 热路径：每连接有界 outbox，满则丢最旧 + need_topup。
        self._outbox = WsOutbox()

    @staticmethod
    def _route(workspace_id: str, conn_id: str) -> EndRoute:
        return EndRoute(source="cli-attached", channel_id=conn_id)

    async def register(self, ws: web.WebSocketResponse, conn_id: str, workspace_id: str) -> AttachConnection | None:
        """注册连接并挂载其工作空间。同空间已有其它 attach 时仍接受。

        返回 None 仅保留给未来扩展（当前路径在 occupancy.acquire 后总是成功）。
        """
        route = self._route(workspace_id, conn_id)
        if not await self.occupancy.acquire(workspace_id, route):
            return None
        async with self._lock:
            conn = AttachConnection(ws=ws, conn_id=conn_id, workspace_id=workspace_id)
            self._connections[conn_id] = conn
            logger.debug(f"AttachClient registered: {conn_id} (workspace={workspace_id})")
            return conn

    async def unregister(self, conn_id: str) -> None:
        """移除连接并释放其工作空间挂载。"""
        self._outbox.clear(conn_id)
        async with self._lock:
            conn = self._connections.pop(conn_id, None)
        if conn is None:
            return
        await self.occupancy.release(conn.workspace_id, self._route(conn.workspace_id, conn_id))
        logger.debug(f"AttachClient unregistered: {conn_id} (workspace={conn.workspace_id})")

    async def rebind_workspace(self, conn_id: str, new_workspace_id: str) -> bool:
        """把连接 pin 换到另一空间（释放旧挂载、登记新空间）"""
        async with self._lock:
            conn = self._connections.get(conn_id)
            if conn is None:
                return False
            old_id = conn.workspace_id
            if old_id == new_workspace_id:
                return True
        route = self._route(new_workspace_id, conn_id)
        old_route = self._route(old_id, conn_id)
        if not await self.occupancy.acquire(new_workspace_id, route):
            return False
        async with self._lock:
            conn = self._connections.get(conn_id)
            if conn is None or conn.workspace_id != old_id:
                commit = False
            else:
                conn.workspace_id = new_workspace_id
                commit = True
        if not commit:
            await self.occupancy.release(new_workspace_id, route)
            return False
        await self.occupancy.release(old_id, old_route)
        logger.debug(f"AttachClient rebound: {conn_id} {old_id} → {new_workspace_id}")
        return True

    async def resume_connection(self, conn_id: str, resume_id: str) -> bool:
        """重连续接：把已注册连接重键到上次连接的 conn_id"""
        if not resume_id or resume_id == conn_id:
            return resume_id == conn_id
        workspace_id: str | None = None
        async with self._lock:
            conn = self._connections.get(conn_id)
            if conn is not None and resume_id not in self._connections:
                workspace_id = conn.workspace_id
        if workspace_id is None:
            return False
        # 占用随 conn_id 走：换到 resume_id 名下（旧键释放），unregister 时才对得上账。
        if not await self.occupancy.acquire(workspace_id, self._route(workspace_id, resume_id)):
            return False
        async with self._lock:
            conn = self._connections.get(conn_id)
            if conn is None or resume_id in self._connections or conn.workspace_id != workspace_id:
                commit = False
            else:
                conn.conn_id = resume_id
                del self._connections[conn_id]
                self._connections[resume_id] = conn
                commit = True
        if not commit:
            await self.occupancy.release(workspace_id, self._route(workspace_id, resume_id))
            return False
        # resume 会回放 TurnStream buffer；旧键 outbox 里的帧会过期
        self._outbox.clear(conn_id)
        await self.occupancy.release(workspace_id, self._route(workspace_id, conn_id))
        logger.debug(f"AttachClient resumed: {conn_id} → {resume_id} (workspace={workspace_id})")
        return True

    def workspace_owner(self, workspace_id: str) -> str | None:
        """该空间任一 attach 连接 id；未挂载返回 None。多端并存时不保证哪一条。"""
        route = self.occupancy.owner(workspace_id)
        if route is None or route.source != "cli-attached":
            return None
        return route.channel_id

    def workspace_owners(self, workspace_id: str) -> list[str]:
        """挂在该空间上的全部 attach conn_id。"""
        return [
            r.channel_id for r in self.occupancy.owners(workspace_id) if r.source == "cli-attached" and r.channel_id
        ]

    def is_workspace_occupied(self, workspace_id: str) -> bool:
        return self.occupancy.is_occupied(workspace_id)

    def has_connections(self) -> bool:
        """是否存在活跃 attach 连接（事件流快路径判空用）。"""
        return bool(self._connections)

    def workspace_targets(self) -> list[tuple[str, str]]:
        """[(conn_id, workspace_id)] 快照——事件流按连接 pin 空间逐连接过滤。"""
        return [(c.conn_id, c.workspace_id) for c in self._connections.values()]

    async def send_to(self, conn_id: str, message: dict[str, Any]) -> bool:
        """发 JSON 给指定连接；失败自动注销（释放占用）。"""
        conn = self._connections.get(conn_id)
        if conn is None:
            return False
        if conn.ws.closed:
            await self.unregister(conn_id)
            return False
        try:
            await conn.ws.send_str(json.dumps(message, ensure_ascii=False))
            return True
        except (ConnectionResetError, ClientConnectionResetError, RuntimeError) as exc:
            logger.debug(f"AttachClient send failed ({conn_id}): {exc}")
            await self.unregister(conn_id)
            return False

    def send_to_nowait(self, conn_id: str, message: dict[str, Any]) -> None:
        """有界 outbox：供同步上下文（TurnStream.emit）调用。"""
        conn = self._connections.get(conn_id)
        if conn is None or conn.ws.closed:
            return

        async def _send(msg: dict[str, Any], *, _cid: str = conn_id) -> bool:
            return await self.send_to(_cid, msg)

        self._outbox.enqueue(conn_id, message, _send)

    async def close_all(self) -> None:
        """服务停止时清空所有连接（尽力而为，不抛）。"""
        for conn_id in list(self._connections):
            conn = self._connections.get(conn_id)
            if conn is not None:
                with contextlib.suppress(Exception):
                    await conn.ws.close()
            await self.unregister(conn_id)
