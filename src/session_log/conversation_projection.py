"""Conversation projection — L1 事件带 → UI 对话行流（平表 conversation_messages 的替代）。

从 append-only 的 session_events 带投影出与旧平表行等价的 dict 流：
``{seq, timestamp, coara_id, coara_name, session_id, role, content, turn_id,
source, agent_kind, files?}``。

投影规则（按 seq 单遍扫描，按 session_id 分桶处理影子）：
- ``user/message``→role=user；``assistant/message``→role=assistant；
  ``assistant/files``→role=assistant + files（content 为空，纯显示投影）
- source：``turn/start`` 建 turn_id→source 索引；消息事件本身不带 turn_id，
  按会话归属到该会话最近一个 turn/start；assistant 行解析不到 source 时
  继承同会话前一 user 行的 source（对齐旧 filter 继承语义）
- content 以 ``<系统消息>``/``<系统提醒>``/``<情境>``/``<子智能体消息>``/``<途中消息>``/
  ``<任务指令>``/``<后台结果>`` 开头的行跳过（注入落点不面向用户）；``<接续输入>`` 包装剥壳
  （对齐旧平表只存显示正文的语义；来源标签已废弃，不再剥壳）
- 影子截断：``history/shadow`` 截掉同会话 seq 大于 keep_until_seq 的已投影尾部；
  旧压缩归档线 ``compaction/summary`` 区间不投影（与 project.py 对齐）
- ``after_seq``：只产出 seq 大于它的事件行（dashboard 增量游标）
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.core.message_tags import strip_continuation_input
from src.session_log.store import iter_events
from src.session_log.types import (
    EVENT_ASSISTANT_DIFF,
    EVENT_ASSISTANT_FILES,
    EVENT_ASSISTANT_MESSAGE,
    EVENT_COMPACTION_SUMMARY,
    EVENT_HISTORY_SHADOW,
    EVENT_TURN_START,
    EVENT_USER_MESSAGE,
)
from src.session_log.types import (
    safe_seq as _safe_seq,
)

# 子智能体事件默认不进对话行（消费方可用 include/exclude 调整）
DEFAULT_EXCLUDE_AGENT_KINDS = frozenset({"subagent"})
# 会引发已投影行回溯失效的事件 kind：增量消费方见到它们必须全量重投影
SHADOW_KINDS = frozenset({EVENT_HISTORY_SHADOW, EVENT_COMPACTION_SUMMARY})

# 注入落点/代理间信封：不面向用户显示，投影跳过（对齐旧平表只存显示正文的语义）
_SYSTEM_PREFIXES = ("<系统消息>", "<系统提醒>", "<情境>", "<子智能体消息>", "<途中消息>", "<任务指令>", "<后台结果>")


def _iso(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(float(ts), UTC).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return ""


def _text(content: Any) -> str:
    """content 取 str；list 结构（图文块）拼 text 块。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(p for p in parts if p)
    return str(content or "")


def project_events(
    events: list[dict[str, Any]],
    *,
    include_agent_kinds: frozenset[str] | set[str] | None = None,
    exclude_agent_kinds: frozenset[str] | set[str] | None = DEFAULT_EXCLUDE_AGENT_KINDS,
    _projector: ConversationProjector | None = None,
) -> list[dict[str, Any]]:
    """把一段事件流（同一磁带、seq 升序）投影为对话行列表（seq 升序）。

    ``include_agent_kinds`` 给定时只保留这些 agent_kind 的行；否则丢弃
    ``exclude_agent_kinds`` 中的 kind（默认排除 subagent）。
    增量投影（只喂新事件）时 source 索引不完整，解析不到的行 source 为空
    （跨批累积索引用 ConversationProjector，见下）。
    """
    projector = _projector or ConversationProjector(
        include_agent_kinds=include_agent_kinds,
        exclude_agent_kinds=exclude_agent_kinds,
    )
    if _projector is None:
        projector.learn_compaction_shadows(events)
    return projector.feed(events)


class ConversationProjector:
    """有状态投影器：turn_id→source 索引与每会话状态跨批累积。

    增量消费方（投影缓存）持有一个实例逐批 feed，增量批次里 turn/start
    在旧段的消息也能凭累积索引解析 source。影子事件必须由调用方判
    （contains_shadow_events）并改用全量重放重建——投影器不做回溯。
    """

    def __init__(
        self,
        *,
        include_agent_kinds: frozenset[str] | set[str] | None = None,
        exclude_agent_kinds: frozenset[str] | set[str] | None = DEFAULT_EXCLUDE_AGENT_KINDS,
    ) -> None:
        self._include = include_agent_kinds
        self._exclude = exclude_agent_kinds
        self._compaction_shadowed: dict[str, set[int]] = {}
        self._turn_sources: dict[str, str] = {}
        self._sessions: dict[str, dict[str, Any]] = {}

    def _kind_allowed(self, kind: str) -> bool:
        if self._include is not None:
            return kind in self._include
        return not (self._exclude and kind in self._exclude)

    def learn_compaction_shadows(self, events: list[dict[str, Any]]) -> None:
        """旧压缩归档线的影子区间（兼容已积累文件；新写入走 history/shadow）。

        区间可引用早于本批的 seq，全量重放前必须先整批学一遍。
        """
        for event in events:
            if event.get("kind") != EVENT_COMPACTION_SUMMARY:
                continue
            payload = event.get("payload") or {}
            start, end = payload.get("shadow_start_seq"), payload.get("shadow_end_seq")
            if start is None or end is None:
                continue
            try:
                interval = set(range(int(start), int(end) + 1))
            except (TypeError, ValueError):
                continue
            session_id = str(event.get("session_id") or "")
            self._compaction_shadowed.setdefault(session_id, set()).update(interval)

    def _session(self, session_id: str) -> dict[str, Any]:
        return self._sessions.setdefault(session_id, {"turn": "", "last_user_source": "", "rows": []})

    def feed(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """投影一批 seq 升序事件 返回新增对话行（投影状态累积保留）。"""
        produced: list[dict[str, Any]] = []
        for event in events:
            kind = str(event.get("kind") or "")
            session_id = str(event.get("session_id") or "")
            payload = event.get("payload") or {}
            seq = _safe_seq(event)

            if kind == EVENT_TURN_START:
                turn_id = str(event.get("turn_id") or "")
                source = str(payload.get("source") or "").strip()
                if turn_id:
                    self._turn_sources[turn_id] = source
                    self._session(session_id)["turn"] = turn_id
                continue
            if kind == EVENT_HISTORY_SHADOW:
                try:
                    keep_until = int(payload.get("keep_until_seq") or 0)
                except (TypeError, ValueError):
                    keep_until = 0
                state = self._session(session_id)
                rows = state["rows"]
                while rows and rows[-1]["seq"] > keep_until:
                    rows.pop()
                state["last_user_source"] = next((r["source"] for r in reversed(rows) if r["role"] == "user"), "")
                # 本批已产出行同样按影子截掉（单批一次性投影的原始语义）
                produced[:] = [r for r in produced if not (r["session_id"] == session_id and r["seq"] > keep_until)]
                continue
            if kind not in (
                EVENT_USER_MESSAGE,
                EVENT_ASSISTANT_MESSAGE,
                EVENT_ASSISTANT_FILES,
                EVENT_ASSISTANT_DIFF,
            ):
                continue
            if seq in self._compaction_shadowed.get(session_id, set()):
                continue

            agent_kind = str(event.get("agent_kind") or "")
            if not self._kind_allowed(agent_kind):
                continue

            state = self._session(session_id)
            turn_id = str(event.get("turn_id") or "") or str(state["turn"] or "")
            source = self._turn_sources.get(turn_id, "") if turn_id else ""
            role = "user" if kind == EVENT_USER_MESSAGE else "assistant"

            files: list[dict[str, Any]] | None = None
            diff: dict[str, Any] | None = None
            if kind == EVENT_ASSISTANT_FILES:
                raw_files = payload.get("files")
                files = [f for f in raw_files if isinstance(f, dict)] if isinstance(raw_files, list) else []
                content = ""
                source = str(payload.get("source") or "").strip() or source
            elif kind == EVENT_ASSISTANT_DIFF:
                raw_diff = payload.get("diff")
                diff = raw_diff if isinstance(raw_diff, dict) else None
                if diff is None:
                    continue
                content = ""
                source = str(payload.get("source") or "").strip() or source
            else:
                content = _text(payload.get("content"))
                if content.strip().startswith(_SYSTEM_PREFIXES):
                    continue
                if role == "user":
                    # 剥 <接续输入> 外层包裹（显示正文）；来源标签已废弃，无需再剥。
                    content = strip_continuation_input(content)

            if role == "assistant" and not source:
                source = str(state["last_user_source"] or "")
            row: dict[str, Any] = {
                "seq": seq,
                "timestamp": _iso(event.get("ts")),
                "coara_id": str(event.get("coara_id") or ""),
                "coara_name": str(event.get("coara_name") or ""),
                "session_id": session_id,
                "role": role,
                "content": content,
                "turn_id": turn_id,
                "source": source,
                "agent_kind": agent_kind,
            }
            if files:
                row["files"] = files
            if diff is not None:
                row["diff"] = diff
            state["rows"].append(row)
            produced.append(row)
            if role == "user":
                state["last_user_source"] = source

        return produced

    def rows(self) -> list[dict[str, Any]]:
        merged = [row for state in self._sessions.values() for row in state["rows"]]
        merged.sort(key=lambda r: r["seq"])
        return merged


def iter_conversation_rows(
    tape_path: str | Path,
    *,
    after_seq: int = 0,
    include_agent_kinds: frozenset[str] | set[str] | None = None,
    exclude_agent_kinds: frozenset[str] | set[str] | None = DEFAULT_EXCLUDE_AGENT_KINDS,
) -> Iterator[dict[str, Any]]:
    """从磁带文件全量重放投影对话行；``after_seq`` 只产出 seq 大于它的行。"""
    events = list(iter_events(Path(tape_path)))
    events.sort(key=_safe_seq)
    for row in project_events(
        events,
        include_agent_kinds=include_agent_kinds,
        exclude_agent_kinds=exclude_agent_kinds,
    ):
        if row["seq"] > after_seq:
            yield row


def filter_rows_for_frontend(rows: list[dict[str, Any]], frontend: str) -> list[dict[str, Any]]:
    """前端过滤：已知属于其它前端的行丢弃；未解析出 source 的行保留。

    （静默丢行比多显示更糟；source 继承已在投影层完成。）
    """
    frontend = str(frontend or "").strip()
    if not frontend:
        return list(rows)
    return [row for row in rows if not row.get("source") or row["source"] == frontend]


def contains_shadow_events(events: list[dict[str, Any]]) -> bool:
    """增量消费方判据：新事件里含影子标记时必须全量重投影。"""
    return any(str(e.get("kind") or "") in SHADOW_KINDS for e in events)


# 增量读取的 active 段尾部窗口：新事件几乎总在窗口内 不足再整读 active
_TAIL_WINDOW_BYTES = 256 * 1024


def _read_new_events(tape_path: Path, after_seq: int) -> list[dict[str, Any]]:
    """只读 seq > after_seq 的新事件。

    归档段不可变 新事件只会出现在 active 段（append-only + seq 全程单调）：
    先读 active 尾部窗口 窗口内存在 seq ≤ after_seq 的行说明边界覆盖完整；
    否则整读 active 兜底。
    """

    def _parse(raw: bytes) -> tuple[list[dict[str, Any]], bool]:
        events: list[dict[str, Any]] = []
        covers_boundary = False
        for line in raw.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if _safe_seq(event) > after_seq:
                events.append(event)
            else:
                covers_boundary = True
        return events, covers_boundary

    active = tape_path
    try:
        size = active.stat().st_size
    except OSError:
        return []
    if size == 0:
        return []
    with active.open("rb") as fh:
        if size <= _TAIL_WINDOW_BYTES:
            events, _ = _parse(fh.read())
            return events
        fh.seek(-_TAIL_WINDOW_BYTES, 2)
        raw = fh.read()
    # 窗口首行可能是半行 丢弃到第一个换行
    nl = raw.find(b"\n")
    events, covers_boundary = _parse(raw[nl + 1 :] if nl >= 0 else b"")
    if covers_boundary:
        return events
    with active.open("rb") as fh:
        events, _ = _parse(fh.read())
        return events


class ConversationProjectionCache:
    """进程内对话投影缓存（hydrate 等高频消费方）：避免每次全带重放。

    三条路径：
    - 命中：store.last_seq 快路径判定无新事件 → 直接返回缓存
    - 增量：只读 active 段尾部新事件 无影子 → 有状态投影器累积追加
      （turn 索引跨批保留 根治增量 source 解析不全）
    - 重建：新事件含 shadow/compaction → 全量重放重置
    """

    def __init__(
        self,
        tape_path: str | Path,
        *,
        include_agent_kinds: frozenset[str] | set[str] | None = None,
        exclude_agent_kinds: frozenset[str] | set[str] | None = DEFAULT_EXCLUDE_AGENT_KINDS,
        max_rows: int = 2000,
    ) -> None:
        import threading

        self._tape_path = Path(tape_path)
        self._include = include_agent_kinds
        self._exclude = exclude_agent_kinds
        self._max_rows = max(100, max_rows)
        self._lock = threading.Lock()
        self._projector: ConversationProjector | None = None
        self._rows: list[dict[str, Any]] = []
        self._last_seq = 0

    def rows(self) -> list[dict[str, Any]]:
        with self._lock:
            self._refresh_locked()
            return list(self._rows)

    def _new_projector(self) -> ConversationProjector:
        return ConversationProjector(
            include_agent_kinds=self._include,
            exclude_agent_kinds=self._exclude,
        )

    def _full_rebuild_locked(self) -> None:
        events = list(iter_events(self._tape_path))
        events.sort(key=_safe_seq)
        self._projector = self._new_projector()
        self._projector.learn_compaction_shadows(events)
        self._projector.feed(events)
        self._rows = self._projector.rows()[-self._max_rows :]

    def _refresh_locked(self) -> None:
        from src.session_log.store import last_seq

        current = last_seq(self._tape_path)
        if self._projector is not None and current == self._last_seq:
            return
        if self._projector is None:
            self._full_rebuild_locked()
            self._last_seq = current
            return
        new_events = _read_new_events(self._tape_path, self._last_seq)
        new_events.sort(key=_safe_seq)
        if contains_shadow_events(new_events):
            self._full_rebuild_locked()
        elif new_events and self._projector is not None:
            self._projector.feed(new_events)
            self._rows = self._projector.rows()[-self._max_rows :]
        self._last_seq = current


__all__ = [
    "DEFAULT_EXCLUDE_AGENT_KINDS",
    "SHADOW_KINDS",
    "ConversationProjectionCache",
    "ConversationProjector",
    "contains_shadow_events",
    "filter_rows_for_frontend",
    "iter_conversation_rows",
    "project_events",
]
