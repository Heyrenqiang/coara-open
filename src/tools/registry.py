"""
Coara v8 - Tool 注册表

管理所有已注册的工具，支持按类别、权限过滤。
"""

from __future__ import annotations

from threading import RLock

from src.core.errors import ToolNotFoundError
from src.core.logger import logger
from src.core.tool_base import BaseTool


class ToolRegistry:
    """
    Tool 注册表。

    全局工具管理，支持注册、查询、过滤。
    """

    def __init__(self):
        self._tools: dict[str, BaseTool] = {}
        # Built-ins are normally registered during process startup, but UI and
        # test lifecycles can read the registry concurrently with that setup.
        # Keep compound operations atomic without imposing an async API on
        # callers that also run outside an event loop.
        self._lock = RLock()

    def register(self, tool: BaseTool) -> None:
        """注册一个工具"""
        with self._lock:
            self._tools[tool.name] = tool
        logger.debug(f"Registered tool: {tool.name}")

    def register_multiple(self, tools: list[BaseTool]) -> None:
        """批量注册工具"""
        for tool in tools:
            self.register(tool)

    def get(self, name: str) -> BaseTool:
        """获取工具"""
        with self._lock:
            if name not in self._tools:
                raise ToolNotFoundError(name)
            return self._tools[name]

    def has(self, name: str) -> bool:
        """检查工具是否存在"""
        with self._lock:
            return name in self._tools

    def list_all(self) -> list[BaseTool]:
        """列出所有工具"""
        with self._lock:
            return list(self._tools.values())

    def clear(self) -> None:
        """清空注册表（主要用于测试）"""
        with self._lock:
            self._tools.clear()


# 全局注册表实例
tool_registry = ToolRegistry()
