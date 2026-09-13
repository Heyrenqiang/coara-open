"""Apply tool execution results to message_history (history mutation only)."""

from __future__ import annotations

from dataclasses import dataclass, field

from src.agent.executor import ToolExecution
from src.core.types import Message, MessageRole


def _append_pending_tool_declaration(coara, tool_name: str) -> None:
    """K3 dynamic tool loading：在 tool_result 落史后再追加声明，保持 tool_calls 配对。"""
    manager = getattr(coara, "_tool_manager", None)
    if manager is None:
        return
    tool = manager.tools.get(tool_name)
    if tool is None:
        return
    from src.tools.builtin.integration.tool import append_tool_declaration

    append_tool_declaration(coara, tool)


@dataclass(slots=True)
class ToolBatchEffects:
    """Side effects from processing a batch of tool executions."""

    made_progress: bool = False
    background_delegate_ids: set[str] = field(default_factory=set)


def apply_tool_results_to_history(
    coara,
    *,
    executions: list[ToolExecution],
    assistant_message_index: int,
    assistant_message: Message,
    recent_progress_signatures: set[str],
) -> tuple[ToolBatchEffects, set[str]]:
    """Append tool results and mutate assistant tool_calls; returns updated progress signatures."""
    from src.coara.stagnation import execution_made_progress

    effects = ToolBatchEffects()
    signatures = set(recent_progress_signatures)

    for execution in executions:
        progressed, signatures = execution_made_progress(execution, signatures)
        if progressed:
            effects.made_progress = True

        result_metadata = execution.result.metadata or {}
        is_background_delegate = (
            execution.tool_call.name == "delegate"
            and result_metadata.get("mode") == "background"
            and not execution.result.is_error
        )
        # 控制面信号（如空闲时 /ws switch）：跳过 append，并剥离对应 assistant tool_call，
        # 避免当前 session 留下悬空 tool_result。mid-turn ws(switch) 走 CoaraRunCancelledError。
        is_control_only = bool(result_metadata.get("control_only")) and not execution.result.is_error
        wrapped_content = coara._wrap_tool_result(execution.tool_call.name, execution.result)
        history_tool_content = wrapped_content

        if is_control_only:
            if assistant_message.tool_calls:
                assistant_message.tool_calls = [
                    tc for tc in assistant_message.tool_calls if tc.id != execution.tool_call.id
                ] or None
        elif not is_background_delegate:
            coara.message_history.append(
                Message(
                    role=MessageRole.TOOL_RESULT,
                    content=history_tool_content,
                    tool_call_id=execution.tool_call.id,
                    name=execution.tool_call.name,
                )
            )
            pending_decl = result_metadata.get("pending_tool_declaration")
            if pending_decl and not execution.result.is_error:
                _append_pending_tool_declaration(coara, str(pending_decl))
            # 会话事件溯源：消息事件统一走 persist 边界 sync_history（防双写）
        elif assistant_message.tool_calls:
            assistant_message.tool_calls = [
                tc for tc in assistant_message.tool_calls if tc.id != execution.tool_call.id
            ] or None
            effects.background_delegate_ids.add(execution.tool_call.id)

    return effects, signatures
