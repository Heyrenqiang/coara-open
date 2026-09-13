"""Session projection checkpoint — 录像带投影检查点（治本提速）。

问题：恢复 = 把最后一个 session 的事件流投影成 message_history，每次启动都
全量扫录像带（含全部 .gz 归档段），磁带越大越慢（随时间劣化）。

思路：录像带 append-only ⇒ 同一 session 的事件流确定 ⇒ 投影结果确定。会话
持久化时把投影结果顺带缓存成检查点；下次启动直接读检查点 + 只增量投影尾部
新增事件。录像带照旧追加、永久保存，一个字节不动。

唯一真正的坑是影子重写（``history/shadow``，压缩/截断时作废投影尾部）。
保守但绝对正确的策略：检查点记录投影到的 seq 水位；恢复时增量读水位之后的
事件，**一旦出现 ``history/shadow`` 即回退全量重建**——宁可压缩后第一次启动
慢一次，也绝不让恢复出错。正确性永远优先于速度。

恢复路径见 ``workspace_state.replay_session_projection``；本模块只管读写与拼接。
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from src.core.types import Message
from src.session_log.types import EVENT_HISTORY_SHADOW

_CHECKPOINT_VERSION = 2  # v2：去掉 source_segments（来源标签废弃，恢复不再按段重打）
_CHECKPOINT_NAME = "session_projection.checkpoint.json"


def checkpoint_path(session_dir: Path) -> Path:
    """检查点文件路径：与录像带同目录（按工作空间隔离）。"""
    return session_dir / _CHECKPOINT_NAME


def serialize_projection(
    *,
    session_id: str,
    last_seq: int,
    messages: list[Message],
    seqs: list[int],
    usage_snapshot: dict[str, Any] | None,
) -> dict[str, Any]:
    """把 SessionProjection 序列化为检查点 dict。"""
    return {
        "version": _CHECKPOINT_VERSION,
        "session_id": session_id,
        "last_seq": int(last_seq),
        "usage_snapshot": usage_snapshot,
        "messages": [m.model_dump(mode="json") for m in messages],
        "seqs": [int(s) for s in seqs],
    }


class ProjectionCheckpoint:
    """反序列化后的检查点：与 SessionProjection 同构的基线投影 + 水位。"""

    __slots__ = ("session_id", "last_seq", "messages", "seqs", "usage_snapshot")

    def __init__(
        self,
        *,
        session_id: str,
        last_seq: int,
        messages: list[Message],
        seqs: list[int],
        usage_snapshot: dict[str, Any] | None,
    ) -> None:
        self.session_id = session_id
        self.last_seq = last_seq
        self.messages = messages
        self.seqs = seqs
        self.usage_snapshot = usage_snapshot


def load_checkpoint(session_dir: Path, session_id: str) -> ProjectionCheckpoint | None:
    """读检查点。损坏/版本不符/session 不匹配 → None（回退全量）。"""
    path = checkpoint_path(session_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    if int(raw.get("version") or 0) != _CHECKPOINT_VERSION:
        return None
    if str(raw.get("session_id") or "") != str(session_id or ""):
        return None
    try:
        messages = [Message(**m) for m in raw.get("messages") or [] if isinstance(m, dict)]
        seqs = [int(s) for s in raw.get("seqs") or []]
    except (TypeError, ValueError):
        return None
    if len(messages) != len(seqs):
        return None
    usage = raw.get("usage_snapshot")
    return ProjectionCheckpoint(
        session_id=str(raw["session_id"]),
        last_seq=int(raw.get("last_seq") or 0),
        messages=messages,
        seqs=seqs,
        usage_snapshot=usage if isinstance(usage, dict) else None,
    )


def write_checkpoint(
    session_dir: Path,
    *,
    session_id: str,
    last_seq: int,
    messages: list[Message],
    seqs: list[int],
    usage_snapshot: dict[str, Any] | None,
) -> bool:
    """原子写检查点（先 .tmp 再 rename，防崩溃半文件）。失败只返回 False。"""
    payload = serialize_projection(
        session_id=session_id,
        last_seq=last_seq,
        messages=messages,
        seqs=seqs,
        usage_snapshot=usage_snapshot,
    )
    path = checkpoint_path(session_dir)
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=session_dir, prefix=".cp_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            os.replace(tmp_name, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
        return True
    except OSError:
        return False


def incremental_needs_full_rebuild(events: list[dict[str, Any]]) -> bool:
    """增量事件里出现 history/shadow（影子重写）即需全量重建——影子会作废
    检查点已缓存的投影尾部，无法安全增量拼接。"""
    return any(e.get("kind") == EVENT_HISTORY_SHADOW for e in events)


def splice_checkpoint_with_increment(
    checkpoint: ProjectionCheckpoint,
    increment_projection: Any,
) -> Any:
    """把检查点基线与增量投影（水位之后的纯追加事件）拼成完整投影。

    调用方须保证增量事件无 history/shadow（否则应先回退全量，不走这里）。
    """
    from src.session_log.project import SessionProjection

    merged = SessionProjection()
    merged.messages = [*checkpoint.messages, *increment_projection.messages]
    merged.seqs = [*checkpoint.seqs, *increment_projection.seqs]
    merged.usage_snapshot = increment_projection.usage_snapshot or checkpoint.usage_snapshot
    return merged
