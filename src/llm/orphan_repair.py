"""Provider-agnostic orphan tool_call repair for outgoing request payloads.

Operates on the message list about to be sent (a copy / request payload), never
on the live ``message_history``. Strict endpoints (Kimi K3, DeepSeek, Anthropic
compat, Responses API) reject any history where an assistant ``tool_calls`` row
lacks its tool response, or where a tool response has no matching call — both
arise from compression folds, rollbacks and session-restore replays.

This is the shared counterpart of the per-turn history repair in
``src/coara/workspace_switch_history.py``; keeping it provider-agnostic removes
the asymmetry where only the OpenAI driver self-healed.
"""

from __future__ import annotations

from src.core.types import Message, MessageRole


def close_orphan_tool_calls(messages: list[Message]) -> list[Message]:
    """Return a copy of ``messages`` with bidirectional orphan repair applied.

    - tool result with no matching assistant tool_call -> dropped
    - assistant tool_call with no following tool result -> placeholder appended
    """
    declared: set[str] = set()
    for msg in messages:
        if msg.role == MessageRole.ASSISTANT and msg.tool_calls:
            declared.update(str(tc.id) for tc in msg.tool_calls if tc.id)

    fixed: list[Message] = []
    i = 0
    n = len(messages)
    while i < n:
        msg = messages[i]
        i += 1
        if msg.role in (MessageRole.TOOL_RESULT, MessageRole.TOOL):
            tcid = str(getattr(msg, "tool_call_id", "") or "")
            if not tcid or tcid not in declared:
                continue  # orphan tool result: drop
        fixed.append(msg)
        if msg.role != MessageRole.ASSISTANT or not msg.tool_calls:
            continue
        answered: set[str] = set()
        j = i
        while j < n and messages[j].role in (MessageRole.TOOL_RESULT, MessageRole.TOOL):
            tcid = str(getattr(messages[j], "tool_call_id", "") or "")
            if tcid and tcid in declared:
                answered.add(tcid)
                fixed.append(messages[j])
            j += 1
        i = j
        for tc in msg.tool_calls:
            tcid = str(getattr(tc, "id", "") or "")
            if tcid and tcid not in answered:
                fixed.append(
                    Message(
                        role=MessageRole.TOOL_RESULT,
                        tool_call_id=tcid,
                        name=str(getattr(tc, "name", "") or ""),
                        content="[未执行] 该工具调用的结果消息在历史中缺失，已按未执行闭合。",
                    )
                )
    return fixed
