"""工具注册与定义 mixin（K 组）。

宿主为 CoaraBase（src/coara/base.py），属性（_tool_manager /
_tool_definitions_override）在宿主 __init__ 初始化（mixin 不设 __init__，只做方法容器）。
"""

from __future__ import annotations

from typing import Any

from src.coara.display import format_tool_call_label
from src.coara.injections import wrap_tool_result
from src.core.tool_base import BaseTool, ToolResult


class ToolsRegistryMixin:
    """工具注册与定义：register / LLM 定义快照 / CLI 标签 / 结果包装"""

    def register_tool(self, tool: BaseTool, *, replace: bool = False) -> None:
        self._tool_manager.register_tool(tool, replace=replace)
        self._invalidate_prompt_cache()

    def register_tools(self, tools: list[BaseTool]) -> None:
        self._tool_manager.register_tools(tools)

    def _get_visible_tool_definitions(self) -> list[dict[str, Any]]:
        return self._tool_manager.get_visible_tool_definitions(self.identity.is_owner_context)

    def _get_tool_definitions_for_llm(self) -> list[dict[str, Any]]:
        if self._tool_definitions_override is not None:
            return self._tool_definitions_override
        return self._tool_manager.get_tool_definitions_for_llm(self.identity.is_owner_context)

    def _format_tool_summary(self, tool_name: str, arguments: Any) -> str:
        """Full single-line tool call label for CLI scrollback (``✓ tool(...)``)."""
        return format_tool_call_label(tool_name, arguments, max_len=None)

    def _wrap_tool_result(self, tool_name: str, result: ToolResult) -> list[dict[str, Any]]:
        """Wrap tool result for message history.

        Delegates to the lightweight injections module. <结果> tag has been
        removed in favor of <系统消息> (informational) and direct content.
        """
        return wrap_tool_result(tool_name, result)
