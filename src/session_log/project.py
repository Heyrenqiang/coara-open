"""Session event log projection — 事件流 → Message 列表（派生视图）。

投影规则（按 seq 顺序单遍扫描）：
- 消息事件（user/assistant/tool_result/system_note）按序产出 Message
- ``history/shadow``：``keep_until_seq`` 之后已投影的消息被替代（压缩/
  概况替换/工具剥离/回滚的兜底标记）→ 截掉 seq 大于该值的已投影尾部
- ``compaction/summary``（旧压缩归档线）：``shadow_start_seq..shadow_end_seq``
  区间不投影（保留兼容已积累的事件文件）
- ``session/meta``：不产出消息；最后一次事件的 payload.usage_snapshot
  作为恢复用量快照
- ``turn/*``、``compaction/start|end`` 非消息事件跳过
"""

from __future__ import annotations

from typing import Any

from src.core.types import Message, MessageRole, ToolCall
from src.session_log.types import (
    EVENT_ASSISTANT_MESSAGE,
    EVENT_COMPACTION_SUMMARY,
    EVENT_HISTORY_SHADOW,
    EVENT_SEGMENT_OPEN,
    EVENT_SESSION_META,
    EVENT_SYSTEM_NOTE,
    EVENT_TOOL_RESULT,
    EVENT_USER_MESSAGE,
)
from src.session_log.types import (
    safe_seq as _safe_seq,
)


def _tool_calls_from_payload(payload: dict[str, Any]) -> list[ToolCall] | None:
    raws = payload.get("tool_calls")
    if not isinstance(raws, list) or not raws:
        return None
    calls: list[ToolCall] = []
    for raw in raws:
        if not isinstance(raw, dict):
            continue
        try:
            calls.append(
                ToolCall(
                    id=str(raw.get("id") or ""),
                    name=str(raw.get("name") or ""),
                    arguments=raw.get("arguments"),
                )
            )
        except (TypeError, ValueError):
            continue
    return calls or None


def _message_from_event(kind: str, payload: dict[str, Any]) -> Message | None:
    if kind == EVENT_USER_MESSAGE:
        return Message(role=MessageRole.USER, content=payload.get("content", ""))
    if kind == EVENT_ASSISTANT_MESSAGE:
        wire_blocks = payload.get("provider_wire_blocks")
        return Message(
            role=MessageRole.ASSISTANT,
            content=payload.get("content", ""),
            reasoning_content=payload.get("reasoning_content"),
            tool_calls=_tool_calls_from_payload(payload),
            provider_wire_blocks=wire_blocks if isinstance(wire_blocks, list) else None,
        )
    if kind == EVENT_TOOL_RESULT:
        return Message(
            role=MessageRole.TOOL_RESULT,
            content=payload.get("content", ""),
            tool_call_id=payload.get("tool_call_id") or "",
            name=payload.get("name") or "",
        )
    if kind == EVENT_SYSTEM_NOTE:
        return Message(role=MessageRole.SYSTEM, content=payload.get("content", ""))
    return None


class SessionProjection:
    """单遍扫描的投影状态（消息 + 对齐的事件 seq + usage 快照）。"""

    __slots__ = ("messages", "seqs", "usage_snapshot")

    def __init__(self) -> None:
        self.messages: list[Message] = []
        self.seqs: list[int] = []
        self.usage_snapshot: dict[str, Any] | None = None


def project_session(events: list[dict[str, Any]]) -> SessionProjection:
    """把一个会话的事件流投影为完整状态（恢复用）。

    events 需为同一 session 的事件、按 seq 升序（``store.read_events`` 保证）。
    """
    # 旧压缩归档线的影子区间（兼容已积累文件；新写入走 history/shadow）
    compaction_shadowed: set[int] = set()
    for event in events:
        if event.get("kind") != EVENT_COMPACTION_SUMMARY:
            continue
        payload = event.get("payload") or {}
        start, end = payload.get("shadow_start_seq"), payload.get("shadow_end_seq")
        if start is None or end is None:
            continue
        try:
            compaction_shadowed.update(range(int(start), int(end) + 1))
        except (TypeError, ValueError):
            continue

    state = SessionProjection()
    # 段事件在磁带中的位置即注入点：其后的消息事件归属该段 source，直到下一
    # 个段边界。segment/open 仅推进「当前段」语义（来源归属由投影 source 字段承载），
    # 恢复不再按段重打内容标签（标签已废弃），故此处只需跳过段事件。
    for event in events:
        seq = _safe_seq(event)
        kind = event.get("kind")
        payload = event.get("payload") or {}
        if kind == EVENT_SESSION_META:
            snapshot = payload.get("usage_snapshot")
            if isinstance(snapshot, dict):
                state.usage_snapshot = snapshot
            continue
        if kind == EVENT_HISTORY_SHADOW:
            keep_until = payload.get("keep_until_seq")
            try:
                keep_until = int(keep_until or 0)
            except (TypeError, ValueError):
                keep_until = 0
            # 截掉公共前缀之后被替代的投影尾部
            while state.seqs and state.seqs[-1] > keep_until:
                state.seqs.pop()
                state.messages.pop()
            continue
        if kind == EVENT_SEGMENT_OPEN:
            continue
        if seq in compaction_shadowed:
            continue
        message = _message_from_event(str(kind), payload)
        if message is not None:
            state.messages.append(message)
            state.seqs.append(seq)
    return state


def derive_messages(events: list[dict[str, Any]], *, skip_shadowed: bool = True) -> list[Message]:
    """把事件流投影成 Message 列表（seq 升序）——``project_session`` 的消息视图。

    ``skip_shadowed=False``：完整重建（含被替代的原始事件），用于审计。
    """
    if skip_shadowed:
        return project_session(events).messages
    # 审计视图：忽略一切影子标记，直接按消息事件重建
    messages: list[Message] = []
    for event in events:
        message = _message_from_event(str(event.get("kind")), event.get("payload") or {})
        if message is not None:
            messages.append(message)
    return messages


__all__ = ["SessionProjection", "derive_messages", "project_session"]
