"""外挂 CLI（coara attach）连接注册表。

与 webui 的 :class:`WebSocketRegistry` 完全独立：webui 是单活跃顶替模型，
attach 是多连接并存——每个外挂 CLI 进程一条连接，绑定一个工作空间，回合
输出按 发起连接 路由回该连接，互不串扰，也绝不顶替浏览器 webui。

同空间允许多条 attach（各有独立 conn_id / EndRoute.channel_id）：共享会话，
谁发消息，回合输出就定向回谁——与多 Web/手机端一致。

占用登记（哪些端挂在某空间）与连接表放在一起：占用以连接存活为准，
连接断开即释放，不留残留。
"""

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

if TYPE_CHECKING:
    from aiohttp import web


@dataclass
class AttachConnection:
    """一条外挂 CLI 的 WebSocket 连接。"""

    ws: web.WebSocketResponse
    conn_id: str
    workspace_id: str


class AttachRegistry:
    """外挂 CLI 连接注册表（连接生命周期）+ 经 WorkspaceOccupancy 登记挂载。

    单事件循环线程安全（asyncio，无显式锁）。占用规则：
    同一工作空间可被多条 attach 同时挂载；互斥已取消（端独立、按 conn 回投）。
    占用事实源为 :class:`WorkspaceOccupancy`，本类只管连接。
    """

    def __init__(self, occupancy: WorkspaceOccupancy | None = None) -> None:
        self._connections: dict[str, AttachConnection] = {}
        # 工作空间↔端占用唯一事实源（共享表；缺省私有，web_server 注入共享实例）
        self.occupancy = occupancy if occupancy is not None else WorkspaceOccupancy()
        self._lock = asyncio.Lock()

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
        async with self._lock:
            conn = self._connections.pop(conn_id, None)
        if conn is None:
            return
        await self.occupancy.release(conn.workspace_id, self._route(conn.workspace_id, conn_id))
        logger.debug(f"AttachClient unregistered: {conn_id} (workspace={conn.workspace_id})")

    async def rebind_workspace(self, conn_id: str, new_workspace_id: str) -> bool:
        """把连接 pin 换到另一空间（释放旧挂载、登记新空间）。失败则保持原 pin。

        占用 acquire/release 不在 ``_lock`` 内（与 :meth:`register` 同序：先 occupancy
        再连接表），避免与 register 交叉死锁。acquire 成功后必须再校验连接仍存活且
        pin 未变，否则吐回新占用——防止 /ws switch 中途断连留下孤儿占用。
        """
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
        """重连续接：把已注册连接重键到上次连接的 conn_id。

        在飞回合的 EndRegistry sender 与 TurnStream.route 均绑定旧 conn_id；
        重键后定向路由（send_to/send_to_nowait 按字典键查）无缝续跑。
        resume_id 已被占用或不存在时拒绝（防顶替它端）。
        """
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
            r.channel_id
            for r in self.occupancy.owners(workspace_id)
            if r.source == "cli-attached" and r.channel_id
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
        """fire-and-forget 版 send_to：供同步上下文（TurnStream.emit）调用。"""
        conn = self._connections.get(conn_id)
        if conn is None or conn.ws.closed:
            return
        asyncio.ensure_future(self.send_to(conn_id, message))

    async def close_all(self) -> None:
        """服务停止时清空所有连接（尽力而为，不抛）。"""
        for conn_id in list(self._connections):
            conn = self._connections.get(conn_id)
            if conn is not None:
                with contextlib.suppress(Exception):
                    await conn.ws.close()
            await self.unregister(conn_id)
