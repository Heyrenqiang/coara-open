"""Tools package."""

from src.core.tool_base import BaseTool, ToolResult
from src.tools.builtin.manifest import PROCESS_STATELESS_TOOL_TYPES
from src.tools.cache import ToolCache, tool_cache
from src.tools.registry import tool_registry

__all__ = [
    "BaseTool",
    "ToolResult",
    "tool_registry",
    "ToolCache",
    "tool_cache",
    "register_builtin_tools",
]

_builtin_tool_names = {tool_type.name for tool_type in PROCESS_STATELESS_TOOL_TYPES}


def _create_process_builtin_tools() -> list[BaseTool]:
    return [tool_type() for tool_type in PROCESS_STATELESS_TOOL_TYPES]


def register_builtin_tools() -> None:
    """Register stateless built-in tools exactly once per process."""
    existing = {tool.name for tool in tool_registry.list_all()}
    if _builtin_tool_names.issubset(existing):
        return
    tool_registry.register_multiple(_create_process_builtin_tools())
