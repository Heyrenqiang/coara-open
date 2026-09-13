"""Control-plane workspace switch: strip switch tail from source, target untouched.

Mid-turn ``ws(switch)`` design:

1. ``apply_tool_results_to_history`` treats a ``control_only`` result as a
   control-plane signal — it is NOT appended as a ``tool_result`` and the
   matching assistant ``tool_call`` is stripped (idle / non-mid-turn path).
2. ``strip_ws_switch_tail`` removes the nearest user input plus all ws
   tool records in the switch attempt from the *source* workspace's history.
3. Target workspace history is never modified on switch.
"""

from __future__ import annotations

from src.agent.executor import ToolExecution
from src.coara.turn_loop.tool_results import apply_tool_results_to_history
from src.coara.workspace_switch_history import (
    close_unmatched_tool_calls,
    finalize_interrupted_turn_history,
    sanitize_dangling_tool_tail,
    strip_ws_switch_tail,
)
from src.core.tool_base import ToolResult
from src.core.types import Message, MessageRole, ToolCall
from src.utils.message_content import message_content_to_text


class _FakeCoara:
    """Minimal stand-in exposing only what apply_tool_results_to_history touches."""

    def __init__(self, message_history: list[Message]) -> None:
        self.message_history = message_history
        self._workflow_draft_pending = False

    def _wrap_tool_result(self, tool_name: str, result: ToolResult):
        return [{"type": "text", "text": message_content_to_text(result.content)}]


def _make_ws_execution(call_id: str) -> ToolExecution:
    return ToolExecution(
        index=0,
        tool_call=ToolCall(id=call_id, name="ws", arguments={"action": "switch", "name": "B"}),
        result=ToolResult.success(
            "已切换到工作空间 B\npath: /ws/B",
            metadata={"control_only": True},
        ),
    )


def test_control_only_result_not_appended_and_tool_call_stripped():
    call_id = "toolu_proj_1"
    assistant = Message(
        role=MessageRole.ASSISTANT,
        content="切过去看看",
        tool_calls=[ToolCall(id=call_id, name="ws", arguments={"action": "switch", "name": "B"})],
    )
    history = [Message(role=MessageRole.USER, content="切换到工作空间 B"), assistant]
    coara = _FakeCoara(history)

    apply_tool_results_to_history(
        coara,
        executions=[_make_ws_execution(call_id)],
        assistant_message_index=1,
        assistant_message=assistant,
        recent_progress_signatures=set(),
    )

    assert not any(m.role == MessageRole.TOOL_RESULT for m in coara.message_history)
    assert assistant.tool_calls is None
    assert len(coara.message_history) == 2


def test_control_only_error_result_falls_back_to_normal_append():
    """A failed control-plane call is a real error the LLM must see — keep it."""
    call_id = "toolu_proj_err"
    assistant = Message(
        role=MessageRole.ASSISTANT,
        content="",
        tool_calls=[ToolCall(id=call_id, name="ws", arguments={"action": "switch", "name": "X"})],
    )
    history = [assistant]
    coara = _FakeCoara(history)

    execution = ToolExecution(
        index=0,
        tool_call=ToolCall(id=call_id, name="ws", arguments={"action": "switch", "name": "X"}),
        result=ToolResult(
            "Unknown workspace: X",
            is_error=True,
            metadata={"control_only": True},
        ),
    )

    apply_tool_results_to_history(
        coara,
        executions=[execution],
        assistant_message_index=0,
        assistant_message=assistant,
        recent_progress_signatures=set(),
    )

    results = [m for m in coara.message_history if m.role == MessageRole.TOOL_RESULT]
    assert len(results) == 1
    assert results[0].tool_call_id == call_id


def test_strip_ws_switch_tail_removes_user_and_ws_chain():
    """ws(list) then ws(switch) in one switch attempt — all removed."""
    history = [
        Message(role=MessageRole.USER, content="你不调用下ws看一下"),
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="t1", name="ws", arguments={"action": "list"})],
        ),
        Message(role=MessageRole.TOOL_RESULT, content="workspaces", tool_call_id="t1", name="ws"),
        Message(role=MessageRole.ASSISTANT, content="要切到哪个？"),
        Message(role=MessageRole.USER, content="切换到pora工作空间"),
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="t2", name="ws", arguments={"action": "switch", "name": "pora"})],
        ),
    ]
    removed = strip_ws_switch_tail(history)

    assert removed == 2
    assert len(history) == 4
    assert history[-1].content == "要切到哪个？"
    assert not any(m.role == MessageRole.USER and "切换到pora" in message_content_to_text(m.content) for m in history)
    assert not any(
        tc.arguments.get("action") == "switch"
        for m in history
        if m.role == MessageRole.ASSISTANT and m.tool_calls
        for tc in m.tool_calls
        if tc.name == "ws"
    )


def test_strip_ws_switch_tail_removes_list_then_use():
    """Switch back: ws(list) + ws(switch) dangling — full tail gone."""
    history = [
        Message(role=MessageRole.USER, content="old nx context"),
        Message(role=MessageRole.ASSISTANT, content="earlier reply"),
        Message(role=MessageRole.USER, content="切换到nx工作空间"),
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="t_list", name="ws", arguments={"action": "list"})],
        ),
        Message(role=MessageRole.TOOL_RESULT, content="list output", tool_call_id="t_list", name="ws"),
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="t_use", name="ws", arguments={"action": "switch", "name": "nx"})],
        ),
    ]
    removed = strip_ws_switch_tail(history)

    assert removed == 4
    assert len(history) == 2
    assert history[0].content == "old nx context"
    assert history[1].content == "earlier reply"


def test_strip_ws_switch_tail_crosses_non_ws_assistant_prose():
    """Intervening text-only assistant must not stop the walk before the user turn."""
    history = [
        Message(role=MessageRole.USER, content="之前的话题"),
        Message(role=MessageRole.ASSISTANT, content="之前的回复"),
        Message(role=MessageRole.USER, content="切换到 v8"),
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="t_list", name="ws", arguments={"action": "list"})],
        ),
        Message(role=MessageRole.TOOL_RESULT, content="catalog", tool_call_id="t_list", name="ws"),
        Message(role=MessageRole.ASSISTANT, content="当前在 nx，切到 v8"),
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="t_use", name="ws", arguments={"action": "switch", "name": "v8"})],
        ),
    ]
    removed = strip_ws_switch_tail(history)

    assert removed == 5
    assert len(history) == 2
    assert history[0].content == "之前的话题"
    assert history[1].content == "之前的回复"
    assert not any("切换到 v8" in message_content_to_text(m.content) for m in history)


def test_strip_ws_switch_tail_crosses_other_tools_in_same_turn():
    """Non-ws tools between the switch user turn and ws(switch) are also stripped."""
    history = [
        Message(role=MessageRole.USER, content="keep me"),
        Message(role=MessageRole.ASSISTANT, content="kept reply"),
        Message(role=MessageRole.USER, content="切换到 nx"),
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="t_read", name="read", arguments={"path": "D:\\x"})],
        ),
        Message(role=MessageRole.TOOL_RESULT, content="ok", tool_call_id="t_read", name="read"),
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="t_use", name="ws", arguments={"action": "switch", "name": "nx"})],
        ),
    ]
    removed = strip_ws_switch_tail(history)

    assert removed == 4
    assert [message_content_to_text(m.content) for m in history] == ["keep me", "kept reply"]


def test_sanitize_dangling_tool_tail_fixes_orphan_tool_call():
    history = [
        Message(role=MessageRole.USER, content="hello"),
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="orphan", name="ws", arguments={"action": "switch", "name": "nx"})],
        ),
    ]
    sanitize_dangling_tool_tail(history)

    assert len(history) == 1
    assert history[0].content == "hello"


def test_finalize_interrupted_turn_keeps_user_and_closes_tools():
    history = [
        Message(role=MessageRole.USER, content="开始"),
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="c1", name="echo", arguments={"text": "a"})],
        ),
    ]
    closed = finalize_interrupted_turn_history(history)
    assert closed == 1
    assert history[0].content == "开始"
    assert history[1].tool_calls and history[1].tool_calls[0].id == "c1"
    assert history[2].role == MessageRole.TOOL_RESULT
    assert history[2].tool_call_id == "c1"
    assert "未执行" in str(history[2].content)


def test_finalize_inserts_cancelled_results_adjacent_to_assistant():
    """Cancelled tool_results must sit right after their assistant message's
    existing tool-result run — not appended at the tail — so providers that
    require tool_result adjacency (Anthropic) accept the history even when a
    later injection followed the assistant row."""
    history = [
        Message(role=MessageRole.USER, content="开始"),
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[
                ToolCall(id="c1", name="echo", arguments={"text": "a"}),
                ToolCall(id="c2", name="echo", arguments={"text": "b"}),
            ],
        ),
        Message(role=MessageRole.TOOL_RESULT, tool_call_id="c1", name="echo", content="done-a"),
        # A later injection (e.g. delegate-completion note) separated the
        # assistant row from the tail.
        Message(role=MessageRole.USER, content="<系统消息>后台任务完成"),
    ]
    closed = finalize_interrupted_turn_history(history)
    assert closed == 1
    # c2's cancelled result is inserted at index 3, adjacent to c1's result —
    # before the injection message, not appended after it.
    assert history[3].role == MessageRole.TOOL_RESULT
    assert history[3].tool_call_id == "c2"
    assert "未执行" in str(history[3].content)
    assert history[4].content == "<系统消息>后台任务完成"


def test_finalize_partial_tool_batch_closes_only_missing():
    history = [
        Message(role=MessageRole.USER, content="开始"),
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[
                ToolCall(id="c1", name="echo", arguments={"text": "a"}),
                ToolCall(id="c2", name="echo", arguments={"text": "b"}),
                ToolCall(id="c3", name="echo", arguments={"text": "c"}),
            ],
        ),
        Message(role=MessageRole.TOOL_RESULT, tool_call_id="c1", name="echo", content="done-a"),
        Message(role=MessageRole.TOOL_RESULT, tool_call_id="c2", name="echo", content="done-b"),
    ]
    closed = finalize_interrupted_turn_history(history)
    assert closed == 1
    assert history[4].tool_call_id == "c3"
    assert "未执行" in str(history[4].content)
    # Idempotent second pass.
    assert finalize_interrupted_turn_history(history) == 0


def test_strip_is_idempotent():
    history = [
        Message(role=MessageRole.USER, content="切换到 B"),
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="u1", name="ws", arguments={"action": "switch", "name": "B"})],
        ),
    ]
    assert strip_ws_switch_tail(history) == 2
    assert strip_ws_switch_tail(history) == 0
    assert history == []


def _dup_shell_call() -> ToolCall:
    return ToolCall(id="x", name="shell", arguments={"command": "echo hi"})


def test_close_unmatched_covers_duplicate_call_id_copies():
    """事件日志重复写盘会把同一 assistant 消息（含 tool_call id）复制成两份，
    而 tool_result 只有一份挂在副本 2 后面。全局集合配对会把 id 判为「已回答」、
    让副本 1 永远悬空，provider 以 "No tool output found for tool call x" 400 拒绝。
    多集差额配对必须为缺结果的副本 1 补一条合成结果。"""
    history = [
        Message(role=MessageRole.USER, content="干活"),
        Message(role=MessageRole.ASSISTANT, content="A1", tool_calls=[_dup_shell_call()]),
        Message(role=MessageRole.ASSISTANT, content="A1", tool_calls=[_dup_shell_call()]),
        Message(role=MessageRole.TOOL_RESULT, tool_call_id="x", name="shell", content="ok"),
        Message(role=MessageRole.USER, content="继续"),
    ]
    closed, _ = close_unmatched_tool_calls(history)

    assert closed == 1
    # 副本 1 紧邻闭合（插在副本 2 之前）
    assert history[2].role == MessageRole.TOOL_RESULT
    assert history[2].tool_call_id == "x"
    # 每份调用都有结果：id=x 的 tool_result 共两条
    assert sum(1 for m in history if m.role == MessageRole.TOOL_RESULT and m.tool_call_id == "x") == 2


def test_close_unmatched_closes_every_copy_when_no_result_exists():
    """两份副本都没有结果时，每份都补一条（closed == 2）。"""
    history = [
        Message(role=MessageRole.USER, content="干活"),
        Message(role=MessageRole.ASSISTANT, content="", tool_calls=[_dup_shell_call()]),
        Message(role=MessageRole.ASSISTANT, content="", tool_calls=[_dup_shell_call()]),
    ]
    closed, _ = close_unmatched_tool_calls(history)

    assert closed == 2
    assert history[1].role == MessageRole.ASSISTANT and history[2].role == MessageRole.TOOL_RESULT
    assert history[3].role == MessageRole.ASSISTANT and history[4].role == MessageRole.TOOL_RESULT


def test_close_unmatched_leaves_fully_answered_duplicates_alone():
    """两份副本各有一份紧邻结果：本就健康，不动历史。"""
    history = [
        Message(role=MessageRole.ASSISTANT, content="", tool_calls=[_dup_shell_call()]),
        Message(role=MessageRole.TOOL_RESULT, tool_call_id="x", name="shell", content="ok1"),
        Message(role=MessageRole.ASSISTANT, content="", tool_calls=[_dup_shell_call()]),
        Message(role=MessageRole.TOOL_RESULT, tool_call_id="x", name="shell", content="ok2"),
    ]
    closed, _ = close_unmatched_tool_calls(history)

    assert closed == 0
    assert len(history) == 4
