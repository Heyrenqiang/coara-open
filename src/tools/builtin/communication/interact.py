"""interact tool — 子智能体与主会话的中间沟通（仅前台 coaras）。

子智能体主动调用 interact 推一条消息到主会话接续队列，下一边界进入主会话
上下文。普通文字输出不传给主会话；最终交付不走工具——任务结束时的最后一
条文本即最终结果（主会话取它兜底）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.core.tool_base import BaseTool, ToolInvocation, ToolKind, ToolResult

if TYPE_CHECKING:
    from src.coara.base import CoaraBase


class InteractTool(BaseTool):
    name = "interact"
    display_name = "Interact"
    description = """向主会话发送消息

何时发，里程碑、卡点、需要主会话拍板，或回复主会话的 <途中消息>
怎么写，惜字如金，精炼有实质内容"""
    kind = ToolKind.OTHER
    category = "communication"
    owner_only = False
    parameters_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "要发给主会话的消息内容"},
        },
        "required": ["message"],
    }

    def __init__(self, *, parent_coara: CoaraBase | None = None, parent_session: CoaraBase | None = None) -> None:
        self._parent_coara = parent_coara  # 子智能体自身（取 id 用）
        self._parent_session = parent_session  # 主会话（消息送达方），注册时直接绑定

    def create_invocation(self, params: dict[str, Any]) -> ToolInvocation:
        return _InteractInvocation(params, self._parent_coara, self._parent_session)


class _InteractInvocation(ToolInvocation):
    def __init__(
        self, params: dict[str, Any], parent_coara: CoaraBase | None, parent_session: CoaraBase | None
    ) -> None:
        super().__init__(params)
        self._parent_coara = parent_coara
        self._parent_session = parent_session
        self.message = str(params.get("message") or "").strip()

    def get_description(self) -> str:
        return f"Interact: {self.message[:60]}"

    async def execute(self, signal=None) -> ToolResult:
        if not self.message:
            return ToolResult.error("interact 需要非空 message")
        if self._parent_coara is None or self._parent_session is None:
            return ToolResult.error("interact 仅在前台委派通道可用（无活跃主会话通道）")
        subagent_id = getattr(self._parent_coara.identity, "name", "") or ""
        from src.core.message_tags import subagent_message

        self._parent_session.submit_continuation_input(
            subagent_message(self.message, task_id=subagent_id, description="")
        )
        return ToolResult.success(
            content=f"已向主会话发送（{len(self.message)} 字）",
            metadata={"task_id": subagent_id, "mode": "interact"},
        )
