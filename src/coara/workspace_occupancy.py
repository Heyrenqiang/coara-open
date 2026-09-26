"""工作空间 ↔ 端 占用表（方案 D5/D6 占用维度）"""

from __future__ import annotations

import asyncio

from src.coara.output_router import EndRoute


class WorkspaceOccupancy:
    """workspace_id → 占用端集合（EndRoute）的登记表"""

    def __init__(self) -> None:
        self._owners: dict[str, set[EndRoute]] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, workspace_id: str, route: EndRoute) -> bool:
        """登记占用；始终成功（同路由幂等）。返回 True 便于调用方统一处理。"""
        async with self._lock:
            owners = self._owners.setdefault(workspace_id, set())
            owners.add(route)
            return True

    async def release(self, workspace_id: str, route: EndRoute) -> None:
        """释放占用；仅移除该 route（不影响同空间其它端）。"""
        async with self._lock:
            owners = self._owners.get(workspace_id)
            if not owners:
                return
            owners.discard(route)
            if not owners:
                del self._owners[workspace_id]

    def owner(self, workspace_id: str) -> EndRoute | None:
        """任一占用端；未占用返回 None。多端并存时不保证哪一条。"""
        owners = self._owners.get(workspace_id)
        if not owners:
            return None
        return next(iter(owners))

    def owners(self, workspace_id: str) -> list[EndRoute]:
        """该空间全部占用端（无序快照）。"""
        return list(self._owners.get(workspace_id, ()))

    def is_occupied(self, workspace_id: str) -> bool:
        return bool(self._owners.get(workspace_id))
