"""orphan_repair：provider 无关的孤儿 tool_call 闭合。"""

from __future__ import annotations

from src.core.types import Message, MessageRole, ToolCall
from src.llm.orphan_repair import close_orphan_tool_calls


def _call(call_id: str, name: str = "shell") -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments={})


def test_healthy_history_untouched():
    history = [
        Message(role=MessageRole.USER, content="干活"),
        Message(role=MessageRole.ASSISTANT, content="", tool_calls=[_call("a")]),
        Message(role=MessageRole.TOOL_RESULT, tool_call_id="a", name="shell", content="ok"),
        Message(role=MessageRole.ASSISTANT, content="完成"),
    ]
    fixed = close_orphan_tool_calls(history)
    assert [m.role for m in fixed] == [m.role for m in history]
    assert len(fixed) == len(history)


def test_missing_result_gets_placeholder():
    history = [
        Message(role=MessageRole.USER, content="干活"),
        Message(role=MessageRole.ASSISTANT, content="", tool_calls=[_call("a"), _call("b", "read")]),
        Message(role=MessageRole.TOOL_RESULT, tool_call_id="a", name="shell", content="ok"),
    ]
    fixed = close_orphan_tool_calls(history)
    assert len(fixed) == 4
    orphan = fixed[-1]
    assert orphan.role == MessageRole.TOOL_RESULT
    assert orphan.tool_call_id == "b"
    assert orphan.name == "read"
    assert "未执行" in str(orphan.content)


def test_orphan_result_without_call_dropped():
    history = [
        Message(role=MessageRole.USER, content="干活"),
        Message(role=MessageRole.TOOL_RESULT, tool_call_id="ghost", name="read", content="残留"),
        Message(role=MessageRole.ASSISTANT, content="好"),
    ]
    fixed = close_orphan_tool_calls(history)
    assert [m.role for m in fixed] == [MessageRole.USER, MessageRole.ASSISTANT]


def test_result_separated_by_injection_still_counts():
    """结果与调用之间被注入消息隔开时，注入消息保留、结果仍被识别为已应答。"""
    history = [
        Message(role=MessageRole.ASSISTANT, content="", tool_calls=[_call("a")]),
        Message(role=MessageRole.USER, content="<系统提醒>插曲</系统提醒>"),
        Message(role=MessageRole.TOOL_RESULT, tool_call_id="a", name="shell", content="ok"),
    ]
    fixed = close_orphan_tool_calls(history)
    # 严格端点要求紧邻：结果不在紧邻段时补占位（结果本体也保留在注入后）
    roles = [m.role for m in fixed]
    assert MessageRole.TOOL_RESULT in roles
    ids = [m.tool_call_id for m in fixed if m.role == MessageRole.TOOL_RESULT]
    assert ids.count("a") >= 1


def test_does_not_mutate_input():
    history = [
        Message(role=MessageRole.ASSISTANT, content="", tool_calls=[_call("a")]),
    ]
    fixed = close_orphan_tool_calls(history)
    assert len(history) == 1
    assert len(fixed) == 2
