"""会话事件溯源：append-only 会话事件日志为唯一事实源，历史/用量/trace 均为派生视图。

模块结构：
- ``types.py`` — 事件模型与消息转换
- ``store.py`` — append-only JSONL 读写
- ``project.py`` — 事件流 → Message/状态投影（恢复用）
- ``recorder.py`` — 回合记录器（persist 边界对账同步）
- ``archive.py`` — 压缩归档 + compaction 标记

架构：docs/SESSION_EVENT_SOURCING.md
"""

from src.session_log.archive import archive_compressed_history_for, archive_compressed_messages
from src.session_log.project import SessionProjection, derive_messages, project_session
from src.session_log.recorder import SessionLogRecorder, build_recorder, message_key
from src.session_log.store import (
    append_event,
    append_events,
    count_events,
    iter_events,
    last_seq,
    read_events,
    resolve_session_log_path,
)

__all__ = [
    "SessionLogRecorder",
    "SessionProjection",
    "append_event",
    "append_events",
    "archive_compressed_history_for",
    "archive_compressed_messages",
    "build_recorder",
    "count_events",
    "derive_messages",
    "iter_events",
    "last_seq",
    "message_key",
    "project_session",
    "read_events",
    "resolve_session_log_path",
]
