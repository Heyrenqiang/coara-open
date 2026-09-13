"""deliver tool — flow 节点交付结果与路由（工作流节点专用，非主会话沟通）。

flow 节点（routes=one）用 deliver(message=结果, next=后继) 交付：
写入 _final_deliver_message 并立即结束本回合迭代（turn_orchestrator 在工具执行后
检查标志终止循环），结果作为节点输出；next 声明后继 node_id。

子智能体与主会话的中间沟通走 interact 工具，不用本工具。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.core.tool_base import BaseTool, ToolInvocation, ToolKind, ToolResult

if TYPE_CHECKING:
    from src.coara.base import CoaraBase


class DeliverTool(BaseTool):
    name = "deliver"
    display_name = "Deliver"
    description = """flow 节点交付结果与后继路由，工作流节点专用

用法 deliver(message=节点结果, next=后继 node_id)——routes_mode=one 时 next 必填，
从可选后继中选一个。调用即交付，本节点回合立即结束，不要再调用任何工具。
子智能体与主会话的中间沟通走 interact 工具，不用本工具。"""
    kind = ToolKind.OTHER
    category = "communication"
    owner_only = False
    parameters_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "节点结果，交付内容"},
            "next": {
                "type": "string",
                "description": "routes_mode=one 时必填，声明本节点的后继 node_id，从 routes_to 中选一个",
            },
        },
        "required": ["message"],
    }

    def __init__(self, *, parent_coara: CoaraBase | None = None) -> None:
        self._parent_coara = parent_coara

    def create_invocation(self, params: dict[str, Any]) -> ToolInvocation:
        return _DeliverInvocation(params, self._parent_coara)


class _DeliverInvocation(ToolInvocation):
    def __init__(self, params: dict[str, Any], parent_coara: CoaraBase | None) -> None:
        super().__init__(params)
        self._parent_coara = parent_coara
        self.message = str(params.get("message") or "").strip()
        self.next_node = str(params.get("next") or "").strip() or None

    def get_description(self) -> str:
        return f"Deliver: {self.message[:60]}"

    async def execute(self, signal=None) -> ToolResult:
        if not self.message:
            return ToolResult.error("deliver 需要非空 message")
        if self._parent_coara is None:
            return ToolResult.error("deliver 仅在 flow 节点上下文可用")
        # 交付：写入结果并终止本回合。turn_orchestrator 在本批工具执行完成后
        # 检查该标志，正常结束迭代循环——deliver 之后不可能再有其它工具调用
        # （物理约束，非事后判断）
        self._parent_coara._final_deliver_message = self.message
        if self.next_node:
            self._parent_coara._next_node = self.next_node
        subagent_id = getattr(self._parent_coara.identity, "name", "") or ""
        return ToolResult.success(
            content=f"结果已交付（{len(self.message)} 字），本回合结束",
            metadata={"task_id": subagent_id, "mode": "deliver", "final": True},
        )
