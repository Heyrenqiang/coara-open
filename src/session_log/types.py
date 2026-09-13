"""Session event log — append-only 会话事件日志（事件溯源改造的事实源）。

事件公共字段：seq（单调）、ts、kind、session_id、turn_id、coara_id、coara_name、agent_kind。
事件按 append 顺序落盘，seq 用于影子标记与投影游标。

写点纪律：消息事件只在回合落盘边界的 sync_history 写入（指纹前缀对账）；
turn 边界与 session/meta 为独立事件；压缩归档（compaction/*）由压缩路径写。
"""

from __future__ import annotations

import time
from typing import Any

from src.core.types import Message, MessageRole

# 事件 kind（第一期）
EVENT_TURN_START = "turn/start"
EVENT_TURN_END = "turn/end"
EVENT_USER_MESSAGE = "user/message"
EVENT_ASSISTANT_MESSAGE = "assistant/message"
EVENT_TOOL_RESULT = "tool/result"
# 工具执行记录（显示投影源 + 展开索引）：executor 实时写，独立事件。
# 只存轻元数据（时长/行数/字节/spill ref），完整内容在 tool_output_store；
# 与 sync_history 边界由 message_to_event 转出的 tool/result（消息内容快照）
# 语义不同——tool/exec 是"执行记录"，tool/result 是"消息投影"，不双写。
EVENT_TOOL_EXEC = "tool/exec"
# 文件卡片（Web 投递附件）：纯显示投影事件，不进消息历史（project_session 跳过），
# 由 conversation_projection 投影为 role=assistant + files 的 UI 行
EVENT_ASSISTANT_FILES = "assistant/files"
# 代码 diff 块（工具改文件产生）：纯显示投影事件，不进消息历史（project_session
# 跳过），由 conversation_projection 投影为 role=assistant + diff 的 UI 行。
# payload 是内核算好的 CanonicalDiffLines（含对称上下文，三端渲染单一事实源），
# 带体积预算（超预算只落 summary 行）——与 assistant/files 同级的「显示内容」事件。
EVENT_ASSISTANT_DIFF = "assistant/diff"
EVENT_SYSTEM_NOTE = "system/note"
EVENT_SESSION_META = "session/meta"
EVENT_COMPACTION_START = "compaction/start"
EVENT_COMPACTION_SUMMARY = "compaction/summary"
EVENT_COMPACTION_END = "compaction/end"
# 落盘同步（persist 边界对账）发现历史前缀被替代/截断时写的影子标记：
# 分叉点之后旧投影不再出现在当前视图，但底稿保留可审计
EVENT_HISTORY_SHADOW = "history/shadow"
# 注入段边界（独立事件，不进消息历史）：每次输入注入上下文（首次输入 /
# mid-turn continuation 注入）开新段。payload 带 seq/source/turn_id/mid_turn，
# 回放/恢复/hydrate 据此重建「哪段输出归哪端」（输出跟随最新注入端）。
EVENT_SEGMENT_OPEN = "segment/open"

_KINDS = frozenset(
    {
        EVENT_TURN_START,
        EVENT_TURN_END,
        EVENT_USER_MESSAGE,
        EVENT_ASSISTANT_MESSAGE,
        EVENT_TOOL_RESULT,
        EVENT_TOOL_EXEC,
        EVENT_ASSISTANT_FILES,
        EVENT_ASSISTANT_DIFF,
        EVENT_SYSTEM_NOTE,
        EVENT_SESSION_META,
        EVENT_COMPACTION_START,
        EVENT_COMPACTION_SUMMARY,
        EVENT_COMPACTION_END,
        EVENT_HISTORY_SHADOW,
        EVENT_SEGMENT_OPEN,
    }
)


def valid_kind(kind: str) -> bool:
    return kind in _KINDS


def safe_seq(event: dict[str, Any]) -> int:
    """事件的单调 seq；缺失/非法按 0 计。投影排序与增量游标共用此口径。"""
    try:
        return int(event.get("seq") or 0)
    except (TypeError, ValueError):
        return 0


def build_event(
    *,
    kind: str,
    seq: int,
    session_id: str,
    payload: dict[str, Any],
    turn_id: str = "",
    coara_id: str = "",
    coara_name: str = "",
    agent_kind: str = "",
    ts: float | None = None,
) -> dict[str, Any]:
    """构造一条会话事件。``seq`` 由调用方从日志当前末尾 +1 提供。"""
    return {
        "seq": int(seq),
        "ts": time.time() if ts is None else float(ts),
        "kind": kind,
        "session_id": session_id,
        "turn_id": turn_id or "",
        "coara_id": coara_id or "",
        "coara_name": coara_name or "",
        "agent_kind": agent_kind or "",
        "payload": payload,
    }


def message_to_event(
    *,
    message: Message,
    seq: int,
    session_id: str,
    turn_id: str = "",
    coara_id: str = "",
    coara_name: str = "",
    agent_kind: str = "",
    ts: float | None = None,
) -> dict[str, Any] | None:
    """把历史中的 Message 转成对应事件（Phase 1 压缩归档用）。无法映射的返回 None。"""
    role = message.role
    common = {
        "seq": int(seq),
        "ts": time.time() if ts is None else float(ts),
        "session_id": session_id,
        "turn_id": turn_id or "",
        "coara_id": coara_id or "",
        "coara_name": coara_name or "",
        "agent_kind": agent_kind or "",
    }
    if role == MessageRole.USER:
        # 用户消息进历史即裸文本（来源标签已废弃），落带直接存裸文本；
        # 历史归属按机制（turn/start 的 source 字段）分类，不存内容标签。
        return {**common, "kind": EVENT_USER_MESSAGE, "payload": {"content": message.content}}
    if role == MessageRole.ASSISTANT:
        payload: dict[str, Any] = {"content": message.content}
        if message.reasoning_content:
            payload["reasoning_content"] = message.reasoning_content
        if message.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": tc.id,
                    "name": tc.name,
                    "arguments": tc.arguments,
                }
                for tc in message.tool_calls
            ]
        if message.provider_wire_blocks:
            # anthropic thinking wire 块：恢复后继续思考链所需，保真落盘
            payload["provider_wire_blocks"] = message.provider_wire_blocks
        return {**common, "kind": EVENT_ASSISTANT_MESSAGE, "payload": payload}
    if role in {MessageRole.TOOL_RESULT, MessageRole.TOOL}:
        return {
            **common,
            "kind": EVENT_TOOL_RESULT,
            "payload": {
                "tool_call_id": message.tool_call_id or "",
                "name": message.name or "",
                "content": message.content,
            },
        }
    # SYSTEM / 其它角色 → 系统注记（保底，不丢信息）
    return {**common, "kind": EVENT_SYSTEM_NOTE, "payload": {"content": str(message.content)}}


__all__ = [
    "EVENT_ASSISTANT_FILES",
    "EVENT_ASSISTANT_MESSAGE",
    "EVENT_COMPACTION_END",
    "EVENT_COMPACTION_START",
    "EVENT_COMPACTION_SUMMARY",
    "EVENT_HISTORY_SHADOW",
    "EVENT_SESSION_META",
    "EVENT_SYSTEM_NOTE",
    "EVENT_TOOL_EXEC",
    "EVENT_TOOL_RESULT",
    "EVENT_TURN_END",
    "EVENT_TURN_START",
    "EVENT_USER_MESSAGE",
    "build_event",
    "message_to_event",
    "safe_seq",
    "valid_kind",
]
