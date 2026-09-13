"""SessionLogRecorder — 挂在 CoaraBase 上的会话事件记录器（唯一事实源）。

写点纪律（防双写）：
- 消息事件（user/assistant/tool_result/system_note）**只**由 ``sync_history``
  在回合落盘边界写入——内存 message_history 与已记投影做指纹前缀对账，
  差异转事件（补记/影子/重写），任何历史写入路径都不会漏记；
  写失败（append_with_seq 返回 False）不推进投影游标，下次 sync 重对账
- 回合边界（turn/start、turn/end）与 ``session/meta``（usage 快照）为独立
  事件，不参与投影，无双写风险

独立事件允许尽力而为（写失败只丢边界标记，不丢消息）。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from src.core.types import Message
from src.session_log.store import _log_write_failure, append_with_seq, resolve_session_log_path
from src.session_log.types import (
    EVENT_ASSISTANT_DIFF,
    EVENT_ASSISTANT_FILES,
    EVENT_HISTORY_SHADOW,
    EVENT_SEGMENT_OPEN,
    EVENT_SESSION_META,
    EVENT_TOOL_EXEC,
    EVENT_TURN_END,
    EVENT_TURN_START,
    build_event,
    message_to_event,
)

# 落带 diff 的单 hunk 行数预算：超过则该 hunk 折叠为 summary（防大文件改动
# 撑爆录像带）。web 端 DiffBlocksView 对 summary hunk 渲染为「已省略 N 行」。
_DIFF_TAPE_MAX_HUNK_LINES = 200


def _budget_diff_for_tape(diff: dict[str, Any] | None, max_hunk_lines: int) -> dict[str, Any] | None:
    """对 CanonicalDiffLines 做落带体积预算：单 hunk 超 max_hunk_lines 折叠为
    summary 占位行（kind=ctx，code 为省略说明），保留 path/added/removed。
    无 hunks 返回 None（不落带）。"""
    if not diff or not isinstance(diff, dict):
        return None
    hunks = diff.get("hunks")
    if not isinstance(hunks, list) or not hunks:
        return None
    out_hunks: list[list[dict[str, Any]]] = []
    for hunk in hunks:
        if not isinstance(hunk, list):
            continue
        if len(hunk) <= max_hunk_lines:
            out_hunks.append(hunk)
            continue
        # 折叠：保留首/尾各 3 行 + 中间省略占位
        head = hunk[:3]
        tail = hunk[-3:]
        omitted = len(hunk) - 6
        placeholder = {
            "kind": "context",
            "oldNum": 0,
            "newNum": 0,
            "code": f"… 已省略 {omitted} 行 …",
        }
        out_hunks.append([*head, placeholder, *tail])
    if not out_hunks:
        return None
    return {
        "path": str(diff.get("path") or ""),
        "added": int(diff.get("added") or 0),
        "removed": int(diff.get("removed") or 0),
        "hunks": out_hunks,
    }


def message_key(msg: Message) -> str:
    """消息指纹（角色 + 内容 + 工具环）：用于前缀比对，不追求密码学强度。"""
    import hashlib
    import json

    try:
        role = str(getattr(msg.role, "value", msg.role) or "")
        # TOOL 与 TOOL_RESULT 角色归一：投影恒还原 TOOL_RESULT，若内存历史
        # 保留原 TOOL 而指纹来自投影，role 差异会误判分叉触发影子重写。
        if role in ("tool", "tool_result"):
            role = "tool_result"
        blob = json.dumps(
            {
                "role": role,
                "content": msg.content,
                "tool_calls": [
                    {"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in (msg.tool_calls or [])
                ],
                "tool_call_id": msg.tool_call_id or "",
                "name": msg.name or "",
                "reasoning_content": msg.reasoning_content or "",
                "provider_wire_blocks": msg.provider_wire_blocks or None,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
    except (TypeError, ValueError):
        blob = f"{getattr(msg.role, 'value', msg.role)}:{str(msg.content)[:256]}"
    return hashlib.sha1(blob.encode("utf-8", "replace")).hexdigest()


class SessionLogRecorder:
    """线程安全的会话事件记录器（单进程；seq 由 store 全局分配，单调递增）。

    投影状态 ``_synced_keys/_synced_seqs``：已确认落入事件日志的内存历史
    指纹与对应事件 seq（恢复会话时由 ``reset_projection`` 重建）。
    """

    def __init__(
        self,
        *,
        workspace_dir: str | Path,
        session_id: str,
        coara_id: str = "",
        coara_name: str = "",
        agent_kind: str = "",
        coara_home: Path | None = None,
        log_path: Path | None = None,
    ) -> None:
        self._workspace_dir = Path(workspace_dir)
        self._session_id = session_id
        self._coara_id = coara_id
        self._coara_name = coara_name
        self._agent_kind = agent_kind
        self._coara_home = coara_home
        # 显式录像带路径（工作流主体系统带）；缺省按工作空间解析
        self._path: Path | None = log_path
        self._lock = threading.Lock()
        self._synced_keys: list[str] = []
        self._synced_seqs: list[int] = []
        self._syncing = False
        # 对账排队：sync 进行中又收到新对账请求时，保留最新快照（合并语义——
        # 多个请求只留最新那版历史），完成后补一次对账，不静默丢弃请求。
        self._pending_sync: list[Message] | None = None

    def _ensure_path(self) -> Path:
        if self._path is None:
            self._path = resolve_session_log_path(self._workspace_dir, coara_home=self._coara_home)
        return self._path

    def _record(self, kind: str, payload: dict[str, Any], turn_id: str = "") -> None:
        path = self._ensure_path()
        try:
            append_with_seq(
                path,
                lambda seq: [
                    build_event(
                        kind=kind,
                        seq=seq,
                        session_id=self._session_id,
                        payload=payload,
                        turn_id=turn_id,
                        coara_id=self._coara_id,
                        coara_name=self._coara_name,
                        agent_kind=self._agent_kind,
                    )
                ],
            )
        except Exception:
            # 独立事件写失败此前完全静默：接同款可见化告警（按文件去抖）
            _log_write_failure(path, 1)

    # ------------------------------------------------------------------
    # 独立事件（非消息，不参与投影）
    # ------------------------------------------------------------------

    def record_turn_start(self, turn_id: str, source: str = "") -> None:
        # payload.source：turn_id→前端来源索引的事实源（conversation_projection
        # 据它给消息行解析 source，对齐旧平表的来源过滤语义）
        self._record(EVENT_TURN_START, {"source": source or ""}, turn_id)

    def record_files(self, *, files: list[dict[str, Any]], turn_id: str = "", source: str = "") -> None:
        """记录一条文件卡片事件（Web 投递附件的显示投影源，不进消息历史）。"""
        self._record(EVENT_ASSISTANT_FILES, {"files": list(files), "source": source or ""}, turn_id)

    def record_assistant_diff(
        self,
        *,
        diff: dict[str, Any],
        turn_id: str = "",
        source: str = "",
        max_hunk_lines: int = _DIFF_TAPE_MAX_HUNK_LINES,
    ) -> None:
        """记录一条代码 diff 事件（工具改文件的显示投影源，不进消息历史）。

        ``diff`` 是内核算好的 CanonicalDiffLines（含对称上下文，三端渲染单一
        事实源）。带体积预算：单 hunk 行数超 ``max_hunk_lines`` 时该 hunk 折叠
        为 summary 行（path + added/removed + 省略计数），防大文件改动撑爆录像带。
        """
        budgeted = _budget_diff_for_tape(diff, max_hunk_lines)
        if budgeted is None:
            return
        self._record(EVENT_ASSISTANT_DIFF, {"diff": budgeted, "source": source or ""}, turn_id)

    def record_turn_end(self, turn_id: str, reason: str) -> None:
        self._record(EVENT_TURN_END, {"reason": reason or ""}, turn_id)

    def record_tool_exec(
        self,
        *,
        turn_id: str,
        tool_call_id: str,
        tool_name: str,
        is_error: bool,
        duration_ms: float | None,
        lines: int,
        bytes_: int,
        spill_ref: str = "",
        cache_hit: bool = False,
    ) -> None:
        """记录一条工具执行事件（显示投影源 + 展开索引，独立事件，实时写）。

        payload 只存轻元数据（时长/行数/字节/spill ref），完整输出在
        tool_output_store 按 ``spill_ref`` 惰性读取；未 spill 的小输出
        完整内容已随历史消息快照（sync_history 的 tool/result）落盘。
        ``lines/bytes`` 是写时算好的折叠计数，显示路径零计算。
        """
        self._record(
            EVENT_TOOL_EXEC,
            {
                "tool_call_id": tool_call_id or "",
                "tool_name": tool_name or "",
                "is_error": bool(is_error),
                "duration_ms": round(float(duration_ms), 2) if duration_ms is not None else None,
                "lines": int(lines),
                "bytes": int(bytes_),
                "spill_ref": spill_ref or "",
                "cache_hit": bool(cache_hit),
            },
            turn_id,
        )

    def record_session_meta(self, payload: dict[str, Any]) -> None:
        """会话级元数据（usage 快照等）；投影时取最后一条。"""
        self._record(EVENT_SESSION_META, payload)

    def record_segment_open(
        self,
        *,
        seq: int,
        source: str,
        turn_id: str = "",
        mid_turn: bool = False,
    ) -> None:
        """记录一条注入段边界事件（独立事件，不进消息历史）。

        输出归属/渲染/恢复的统一分界：段内输出归 ``source`` 端。
        """
        self._record(
            EVENT_SEGMENT_OPEN,
            {"seq": int(seq), "source": source or "", "mid_turn": bool(mid_turn)},
            turn_id,
        )

    # ------------------------------------------------------------------
    # 落盘边界对账：内存 message_history ⟷ 事件日志已记投影
    # ------------------------------------------------------------------

    def sync_history(self, messages: list[Message]) -> None:
        """把内存历史与已记投影对账，差异转事件（线程安全，幂等，无差异零写入）。

        - 追加：公共前缀不变，尾部新增 → 逐条事件化补记
        - 前缀分叉（压缩/概况替换/工具剥离）：写 ``history/shadow``
          （``keep_until_seq`` = 公共前缀最后一条事件 seq），分叉后的新消息
          全部事件化重写；旧投影事件底稿保留、不再投影
        - 截断（content_policy 回滚等）：分叉的特殊情形，同影子
        """
        with self._lock:
            if self._syncing:
                # 不丢弃对账请求：记录最新快照，当前 sync 完成后补一次对账。
                # 若被丢的这次携带更新历史（如 ws 清尾后的影子），期间崩溃
                # 会导致该影子从未落盘、恢复后旧尾部「复活」。合并到最新快照
                # 即可——对账是幂等的，只认最新历史。
                self._pending_sync = list(messages)
                return
            self._syncing = True
        try:
            current: list[Message] = list(messages)
            while True:
                self._sync_history_locked(current)
                with self._lock:
                    if self._pending_sync is None:
                        self._syncing = False
                        return
                    current = self._pending_sync
                    self._pending_sync = None
        finally:
            with self._lock:
                self._syncing = False

    def _sync_history_locked(self, messages: list[Message]) -> None:
        path = self._ensure_path()
        keys = [message_key(m) for m in messages]

        common = 0
        for old_key, new_key in zip(self._synced_keys, keys, strict=False):
            if old_key != new_key:
                break
            common += 1

        if common == len(self._synced_keys) and common == len(keys):
            return  # 无差异

        keep_until_seq = self._synced_seqs[common - 1] if common > 0 else 0
        replaced_count = len(self._synced_keys) - common
        new_seqs_holder: list[int] = []

        def _build(start: int) -> list[dict[str, Any]]:
            events: list[dict[str, Any]] = []
            seq = start
            if replaced_count > 0:
                events.append(
                    build_event(
                        kind=EVENT_HISTORY_SHADOW,
                        seq=seq,
                        session_id=self._session_id,
                        payload={
                            "keep_until_seq": keep_until_seq,
                            "replaced_count": replaced_count,
                            "reason": "history_prefix_replaced",
                        },
                        coara_id=self._coara_id,
                        coara_name=self._coara_name,
                        agent_kind=self._agent_kind,
                    )
                )
                seq += 1
            for idx in range(common, len(messages)):
                event = message_to_event(
                    message=messages[idx],
                    seq=seq,
                    session_id=self._session_id,
                    coara_id=self._coara_id,
                    coara_name=self._coara_name,
                    agent_kind=self._agent_kind,
                )
                if event is not None:
                    events.append(event)
                    new_seqs_holder.append(seq)
                    seq += 1
            return events

        # 写失败（返回 False）不推进游标：下次 sync 重对账同一差异，避免静默丢增量
        if append_with_seq(path, _build):
            self._synced_keys = keys
            self._synced_seqs = self._synced_seqs[:common] + new_seqs_holder

    def reset_projection(self, *, keys: list[str], seqs: list[int]) -> None:
        """恢复会话后由投影器重建状态（只设游标，不写盘）。"""
        with self._lock:
            self._synced_keys = list(keys)
            self._synced_seqs = list(seqs)


def build_recorder(
    *,
    workspace_dir: str | Path,
    session_id: str,
    coara_id: str,
    coara_name: str,
    agent_kind: str = "",
    coara_home: Path | None = None,
    log_path: Path | None = None,
) -> SessionLogRecorder | None:
    """创建 recorder（事件日志为唯一事实源，始终启用）。"""
    try:
        return SessionLogRecorder(
            workspace_dir=workspace_dir,
            session_id=session_id,
            coara_id=coara_id,
            coara_name=coara_name,
            agent_kind=agent_kind,
            coara_home=coara_home,
            log_path=log_path,
        )
    except Exception:
        return None


__all__ = ["SessionLogRecorder", "build_recorder", "message_key"]
