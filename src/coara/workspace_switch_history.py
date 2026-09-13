"""Message-history cleanup for mid-turn workspace switches."""

from __future__ import annotations

from collections import Counter

from src.coara.injections.context_modules import is_context_module_seed
from src.core.message_tags import CONTINUATION_OPEN
from src.core.types import Message, MessageRole, ToolCall
from src.utils.message_content import message_content_to_text


def _is_conversation_user_message(msg: Message) -> bool:
    """True for real user turns, not context-module seeds or system injections."""
    if msg.role != MessageRole.USER:
        return False
    text = message_content_to_text(msg.content).strip()
    if not text:
        return False
    if is_context_module_seed(text):
        return False
    if text.startswith("<系统消息>"):
        return False
    if text.startswith("<系统提醒>"):
        return False
    return CONTINUATION_OPEN not in text


def _has_ws_switch(msg: Message) -> bool:
    if msg.role != MessageRole.ASSISTANT or not msg.tool_calls:
        return False
    for tc in msg.tool_calls:
        if tc.name != "ws":
            continue
        action = str((tc.arguments or {}).get("action", "")).strip()
        if action == "switch":
            return True
    return False


def _has_ws_tool_call(msg: Message) -> bool:
    if msg.role != MessageRole.ASSISTANT or not msg.tool_calls:
        return False
    return any(tc.name == "ws" for tc in msg.tool_calls)


def sanitize_dangling_tool_tail(message_history: list[Message]) -> None:
    """Drop orphan tool_results and trailing assistant messages with dangling tool_calls."""
    tool_use_ids: set[str] = set()
    for msg in message_history:
        if msg.role == MessageRole.ASSISTANT and msg.tool_calls:
            for tc in msg.tool_calls:
                tool_use_ids.add(tc.id)

    i = 0
    while i < len(message_history):
        msg = message_history[i]
        if msg.role == MessageRole.TOOL_RESULT and msg.tool_call_id not in tool_use_ids:
            message_history.pop(i)
            continue
        i += 1

    while message_history:
        last = message_history[-1]
        if last.role == MessageRole.ASSISTANT and last.tool_calls:
            message_history.pop()
            continue
        break


def close_unmatched_tool_calls(
    message_history: list[Message],
    *,
    content: str = "[已取消] 工具执行被用户打断。",
) -> tuple[int, int]:
    """Insert synthetic tool_results for any tool_call that still lacks a response.

    Providers (Anthropic / Kimi compat) reject histories where an assistant
    ``tool_calls`` row is followed by a non-tool message (e.g. a truncation
    reminder) without matching tool_results. Insert results immediately after
    each assistant's existing tool-result run so adjacency stays valid.
    """
    tool_use_ids: set[str] = set()
    for msg in message_history:
        if msg.role == MessageRole.ASSISTANT and msg.tool_calls:
            for tc in msg.tool_calls:
                tool_use_ids.add(tc.id)

    i = 0
    while i < len(message_history):
        msg = message_history[i]
        if msg.role == MessageRole.TOOL_RESULT and msg.tool_call_id not in tool_use_ids:
            message_history.pop(i)
            continue
        i += 1

    # 配对按「每 id 多集差额 + 调用点紧邻结果段」而非全局集合：同一 tool_call id
    # 出现在多条 assistant 消息上（如事件日志重复写盘产生的副本）时，每个调用点
    # 都需要自己的结果——否则 provider 会以 "No tool output found for tool call ..."
    # 拒掉整个请求，而全局集合会被另一副本上的结果掩盖、永远修不好
    result_totals: Counter[str] = Counter()
    call_totals: Counter[str] = Counter()
    for msg in message_history:
        if msg.role == MessageRole.TOOL_RESULT and msg.tool_call_id:
            result_totals[msg.tool_call_id] += 1
        elif msg.role == MessageRole.ASSISTANT and msg.tool_calls:
            for tc in msg.tool_calls:
                call_totals[tc.id] += 1

    # 每个 id 还缺几份结果（调用点数 − 结果数，仅计缺口）
    deficit: dict[str, int] = {}
    for cid, count in call_totals.items():
        gap = count - result_totals.get(cid, 0)
        if gap > 0:
            deficit[cid] = gap
    if not deficit:
        return 0, 0

    pending: list[tuple[int, list[ToolCall]]] = []
    for idx, msg in enumerate(message_history):
        if msg.role != MessageRole.ASSISTANT or not msg.tool_calls:
            continue
        # 本调用点紧邻的 tool_result 段（多集）：紧邻结果优先满足本点，
        # 差额只补给没有紧邻结果的调用点
        adjacent: Counter[str] = Counter()
        j = idx + 1
        while j < len(message_history) and message_history[j].role == MessageRole.TOOL_RESULT:
            if message_history[j].tool_call_id:
                adjacent[message_history[j].tool_call_id] += 1
            j += 1
        missing: list[ToolCall] = []
        for tc in msg.tool_calls:
            if adjacent.get(tc.id, 0) > 0:
                adjacent[tc.id] -= 1
            elif deficit.get(tc.id, 0) > 0:
                deficit[tc.id] -= 1
                missing.append(tc)
        if missing:
            pending.append((idx, missing))

    closed = 0
    closed_background_delegate = 0
    for idx, missing in reversed(pending):
        insert_at = idx + 1
        while insert_at < len(message_history) and message_history[insert_at].role == MessageRole.TOOL_RESULT:
            insert_at += 1
        for tc in missing:
            message_history.insert(
                insert_at,
                Message(
                    role=MessageRole.TOOL_RESULT,
                    tool_call_id=tc.id,
                    name=tc.name,
                    content=content,
                ),
            )
            insert_at += 1
            # delegate 后台模式：ToolResult 占位符在 BackgroundAgentManager.start()
            # 返回后才写入历史，start 进行中恰好碰上 prepare 会被检测为孤儿——
            # 这是设计内瞬态，闭合但不算异常。
            if tc.name == "delegate" and bool((tc.arguments or {}).get("background")):
                closed_background_delegate += 1
            else:
                closed += 1
    return closed, closed_background_delegate


def finalize_interrupted_turn_history(message_history: list[Message]) -> int:
    """Keep the interrupted turn in history; close unmatched tool_calls for the next LLM call.

    Unlike ``sanitize_dangling_tool_tail`` (used for workspace switch), this does **not**
    drop the user message or assistant tool-intent rows.

    中断时编排器已把执行器带出的真实结果（已完成/已取消）先入史，走到这里的
    悬空 tool_call 只会是「未执行」的调用，合成闭合文案如实标注
    """
    closed, _ = close_unmatched_tool_calls(
        message_history,
        content="[未执行] 回合被中断，该工具调用未执行。",
    )
    return closed


def strip_ws_switch_tail(message_history: list[Message]) -> int:
    """Remove the switch attempt from the nearest user turn through ``ws(switch)``.

    From the assistant message that called ``ws(switch)``, walk upward to the
    first real user input; delete that user message and everything after it
    (assistant prose, other tools, ``ws(list)`` / results, and the ``switch``
    call). System injections (env seed, reminders) are skipped when walking
    and are not treated as the strip boundary.

    Called on the *source* workspace when ``ws(switch)`` fires mid-turn. The
    target workspace history is never touched.
    """
    if not message_history:
        return 0

    anchor_idx: int | None = None
    for i in range(len(message_history) - 1, -1, -1):
        if _has_ws_switch(message_history[i]):
            anchor_idx = i
            break

    if anchor_idx is None:
        for i in range(len(message_history) - 1, -1, -1):
            if _has_ws_tool_call(message_history[i]):
                anchor_idx = i
                break

    if anchor_idx is None:
        sanitize_dangling_tool_tail(message_history)
        return 0

    start_idx = anchor_idx
    for i in range(anchor_idx - 1, -1, -1):
        msg = message_history[i]
        if _is_conversation_user_message(msg):
            start_idx = i
            break
        if msg.role == MessageRole.USER:
            # Env seed / system-tagged user injections — walk past them; do not
            # treat them as the switch-triggering user turn.
            continue
        # Extend through any same-turn traffic (assistant text, other tools,
        # ws list/results) until the triggering user input.
        start_idx = i

    removed = len(message_history) - start_idx
    if removed > 0:
        del message_history[start_idx:]

    sanitize_dangling_tool_tail(message_history)
    return removed
