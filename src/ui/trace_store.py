"""Persistent trace and conversation storage for the standalone dashboard."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import queue
import re
import threading
import uuid
from collections import OrderedDict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from src.core.coara_home import CoaraHomePaths, resolve_trace_data_dir
from src.core.events import TraceEvent
from src.core.json_store import write_json_atomic, write_text_atomic
from src.core.logger import logger
from src.core.text import preview_line
from src.core.time import parse_utc_datetime, utc_now_iso
from src.utils.text_utils import sanitize_json_payload


def _parse_jsonl_text(raw_text: str) -> list[dict[str, Any]]:
    """解析 JSONL 文本（纯计算，可在锁外执行）。"""
    rows: list[dict[str, Any]] = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _utc_now() -> str:
    return utc_now_iso(timespec="seconds")


@dataclass(slots=True)
class DashboardRuntimeState:
    workspace: str
    provider: str
    model: str
    running: bool
    pid: int
    updated_at: str


@dataclass(slots=True)
class _FileWriteOp:
    path: Path
    text: str
    append: bool = True


class _AsyncFileWriter:
    """Shared async file writer for a workspace.

    EventBus subscribers stay cheap while trace events, messages, and heavy
    detail payloads are persisted on a background thread.
    """

    _BATCH_LIMIT = 64
    _IDLE_BATCH_WAIT_SECONDS = 0.05

    def __init__(self) -> None:
        self._queue: queue.Queue[_FileWriteOp | None] = queue.Queue()
        self._pending = 0
        self._pending_lock = threading.Lock()
        self._drained = threading.Event()
        self._drained.set()
        self._closed = False
        self._io_lock = threading.Lock()
        self._thread = threading.Thread(target=self._worker_loop, name="coara-trace-writer", daemon=True)
        self._thread.start()

    @property
    def io_lock(self) -> threading.Lock:
        return self._io_lock

    def append_jsonl(self, path: Path, payload: dict[str, Any]) -> None:
        safe = sanitize_json_payload(payload)
        self.enqueue(_FileWriteOp(path=path, text=json.dumps(safe, ensure_ascii=False) + "\n", append=True))

    def write_json(self, path: Path, payload: dict[str, Any]) -> None:
        safe = sanitize_json_payload(payload)
        self.enqueue(_FileWriteOp(path=path, text=json.dumps(safe, ensure_ascii=False, indent=2), append=False))

    def enqueue(self, operation: _FileWriteOp) -> None:
        if self._closed:
            self._write_batch([operation])
            return
        with self._pending_lock:
            self._pending += 1
            self._drained.clear()
        self._queue.put(operation)

    def flush(self, timeout: float | None = None) -> bool:
        return self._drained.wait(timeout=timeout)

    def close(self) -> None:
        if self._closed:
            return
        # Wait for pending writes to flush (max 2s).
        self.flush(timeout=2.0)
        # Signal the worker thread to exit after draining remaining items.
        self._queue.put(None)
        # Wait for the thread to finish (it drains the queue before exiting).
        self._thread.join(timeout=2.0)
        # After the thread exits, all pending items are written — no need
        # for another flush. Just mark as closed.
        self._closed = True

    def _worker_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                self._drain_and_close()
                return
            batch = [item]
            while len(batch) < self._BATCH_LIMIT:
                try:
                    next_item = self._queue.get(timeout=self._IDLE_BATCH_WAIT_SECONDS)
                except queue.Empty:
                    break
                if next_item is None:
                    self._write_batch(batch)
                    self._drain_and_close()
                    return
                batch.append(next_item)
            self._write_batch(batch)

    def _drain_and_close(self) -> None:
        batch: list[_FileWriteOp] = []
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is not None:
                batch.append(item)
        if batch:
            self._write_batch(batch)

    def _write_batch(self, batch: list[_FileWriteOp]) -> None:
        try:
            with self._io_lock:
                append_groups: dict[Path, list[str]] = {}
                overwrite_ops: list[_FileWriteOp] = []
                for operation in batch:
                    if operation.append:
                        append_groups.setdefault(operation.path, []).append(operation.text)
                    else:
                        overwrite_ops.append(operation)

                for path, lines in append_groups.items():
                    with path.open("a", encoding="utf-8") as handle:
                        handle.writelines(lines)

                for operation in overwrite_ops:
                    operation.path.write_text(operation.text, encoding="utf-8")
        except Exception as exc:
            logger.warning(f"Failed to append trace batch: {exc}")
        finally:
            with self._pending_lock:
                self._pending = max(0, self._pending - len(batch))
                if self._pending == 0:
                    self._drained.set()


class TraceStore:
    """Append-only storage for trace events and runtime state."""

    _DETAIL_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
    _LEGACY_DETAIL_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
    _DETAIL_MESSAGE_PREVIEW_LIMIT = 180
    _DETAIL_MESSAGE_COUNT_LIMIT = 3
    # Cap detail files to prevent unbounded disk growth in long-running sessions.
    _MAX_DETAIL_FILES = 5000
    # Rotate JSONL files when they exceed 50 MB.
    _MAX_JSONL_SIZE_BYTES = 50 * 1024 * 1024
    # Cap the in-memory event dedupe cache (FIFO eviction beyond this).
    _MAX_WRITTEN_EVENT_KEYS = 10_000
    _shared_writers: dict[str, tuple[_AsyncFileWriter, int]] = {}
    _shared_writers_lock = threading.Lock()

    def __init__(self, workspace_dir: Path | str, *, coara_home: Path | str | None = None):
        self.workspace_dir = Path(workspace_dir)
        self.home_paths = CoaraHomePaths.for_workspace(self.workspace_dir, configured_home=coara_home)
        self.data_dir = resolve_trace_data_dir(self.workspace_dir, configured_home=coara_home)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.details_dir = self.data_dir / "trace_details"
        self.details_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.data_dir / "trace_events.jsonl"
        self.runtime_path = self.data_dir / "dashboard_runtime.json"
        self._runtime_cache: dict[str, Any] | None = None
        # 会话增量缓存实例锁：串行化 _build_sessions 与 reset_session_cache，
        # 避免 WS 线程与 REST 线程并发读改写造成会话列表重复/错乱
        self._session_lock = threading.Lock()
        writer_key = str(self.data_dir.resolve())
        self._writer_key = writer_key
        with self._shared_writers_lock:
            entry = self._shared_writers.get(writer_key)
            if entry is None:
                entry = (_AsyncFileWriter(), 0)
            writer, ref_count = entry
            self._shared_writers[writer_key] = (writer, ref_count + 1)
            self._writer = writer
        self._prune_counter = 0
        self._prune_interval = 20
        self._rotate_counter: dict[Path, int] = {}
        self._rotate_interval = 10
        self._prune_detail_files()

    def append_event(self, event: TraceEvent) -> bool:
        # chat_chunk 逐流式增量落盘没有任何消费方（恢复/hydrate//log 均不读），
        # 同一文本的最终全量已由 conversation_message 记录——纯写放大，不落盘。
        if event.event_type == "chat_chunk":
            return False
        payload_dict = self._prepare_event_payload(event)
        dedupe_key = self._event_dedupe_key(payload_dict)
        if not hasattr(self, "_written_event_keys"):
            # OrderedDict doubles as a set + FIFO eviction order: cap memory
            # so a long-lived session can't grow the dedupe cache unbounded.
            self._written_event_keys: OrderedDict[tuple[Any, ...], None] = OrderedDict()
        if dedupe_key in self._written_event_keys:
            return False
        self._written_event_keys[dedupe_key] = None
        if len(self._written_event_keys) > self._MAX_WRITTEN_EVENT_KEYS:
            self._written_event_keys.popitem(last=False)
        self._rotate_jsonl_if_needed(self.events_path)
        self._writer.append_jsonl(self.events_path, payload_dict)
        return True

    def sync_runtime_from_event(self, event: TraceEvent) -> None:
        """Update dashboard runtime heartbeat from high-signal trace events."""
        payload = event.payload or {}
        if event.event_type == "llm_turn_start":
            current = self.load_runtime_state()
            self.set_runtime_state(
                provider=str(payload.get("provider", "") or current.get("provider", "") or ""),
                model=str(payload.get("model", "") or current.get("model", "") or ""),
                running=True,
            )
        elif event.event_type == "turn_start":
            # New high-level turn event emitted by process_message — mark
            # runtime as running so the dashboard shows live status
            # even when llm_turn_start hasn't fired yet (e.g. before the LLM
            # call begins). Provider/model are not in this payload, so we
            # preserve whatever was previously set.
            current = self.load_runtime_state()
            self.set_runtime_state(
                provider=str(current.get("provider", "") or ""),
                model=str(current.get("model", "") or ""),
                running=True,
            )
        elif event.event_type in ("turn_end", "turn_interrupted", "turn_failed"):
            current = self.load_runtime_state()
            self.set_runtime_state(
                provider=str(current.get("provider", "") or ""),
                model=str(current.get("model", "") or ""),
                running=False,
            )

    def set_runtime_state(
        self,
        *,
        provider: str,
        model: str,
        running: bool,
    ) -> None:
        state = DashboardRuntimeState(
            workspace=str(self.workspace_dir),
            provider=provider,
            model=model,
            running=running,
            pid=os.getpid(),
            updated_at=_utc_now(),
        )
        payload_dict = asdict(state)
        self._runtime_cache = payload_dict
        payload = json.dumps(payload_dict, ensure_ascii=False, indent=2)
        # Runtime JSON is independent of the JSONL writer queue — do not flush
        # the event writer here (that blocked the EventBus publish path).
        with self._writer.io_lock:
            self._atomic_write_text(self.runtime_path, payload)

    async def heartbeat(self) -> None:
        self.flush()
        if not self.runtime_path.exists() and self._runtime_cache is None:
            return

        def _update() -> None:
            with self._writer.io_lock:
                if self._runtime_cache is not None:
                    state = dict(self._runtime_cache)
                else:
                    state = json.loads(self.runtime_path.read_text(encoding="utf-8"))
                state["updated_at"] = _utc_now()
                self._runtime_cache = state
                write_json_atomic(self.runtime_path, state)

        await asyncio.to_thread(_update)

    def load_runtime_state(self) -> dict[str, Any]:
        if self._runtime_cache is not None:
            return dict(self._runtime_cache)
        state = self._load_runtime_state()
        self._runtime_cache = state
        return dict(state)

    def load_events(self) -> tuple[list[dict[str, Any]], int]:
        """Load persisted trace events plus current file size.

        DashboardRestHandlers uses the size as an incremental-read cursor.
        """
        self.flush()
        return self._load_jsonl(self.events_path), self._jsonl_size(self.events_path)

    # 尾部倒读块大小：每次从文件末尾向前读一块，累积直到凑够目标条数。
    _TAIL_READ_CHUNK = 256 * 1024
    # 倒读总量上限：防极端情况（目标事件稀疏）下读穿整个大文件。
    _TAIL_READ_MAX_BYTES = 32 * 1024 * 1024

    def load_recent_events_tail(self, match: Any, limit: int) -> list[dict[str, Any]]:
        """从文件尾部倒读，返回最近 ``limit`` 条满足 ``match(row)`` 的事件（时间升序）。

        侧栏 hydrate 只需最近 N 条相关事件，全量 ``load_events`` 会把整个
        trace_events.jsonl 读进内存并解析所有行——大文件下每次请求数百 ms 到
        数秒。这里按块倒读、凑够即停，不碰文件前部，把延迟降到亚十毫秒级。

        ``match`` 是行过滤谓词（在解析后调用，返回 True 表示相关）。返回的事件
        按时间升序（与 ``load_events`` 的顺序一致），调用方无需再反转。
        """
        self.flush()
        path = self.events_path
        if not path.exists() or limit <= 0:
            return []
        with self._writer.io_lock:
            size = path.stat().st_size
            if size <= 0:
                return []
            collected: list[dict[str, Any]] = []  # 新→旧累积
            pos = size
            read_bytes = 0
            # buffer 保存「块首不完整行」待与下一块拼接
            tail_fragment = ""
            with path.open("rb") as handle:
                while pos > 0 and len(collected) < limit and read_bytes < self._TAIL_READ_MAX_BYTES:
                    chunk_size = min(self._TAIL_READ_CHUNK, pos)
                    pos -= chunk_size
                    handle.seek(pos)
                    raw = handle.read(chunk_size)
                    read_bytes += chunk_size
                    text = raw.decode("utf-8", errors="replace") + tail_fragment
                    lines = text.split("\n")
                    # 第一行在本块起点可能不完整（被切断），留作与下一块拼接；
                    # 已到文件头（pos==0）时它是完整首行，照常解析。
                    if pos > 0:
                        tail_fragment = lines[0]
                        lines = lines[1:]
                    else:
                        tail_fragment = ""
                    for line in reversed(lines):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            row = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if match(row):
                            collected.append(row)
                            if len(collected) >= limit:
                                break
        # collected 是新→旧，反转为时间升序
        collected.reverse()
        return collected

    def flush(self, timeout: float | None = 2.0) -> bool:
        return self._writer.flush(timeout=timeout)

    @property
    def io_lock(self) -> threading.Lock:
        return self._writer.io_lock

    def close(self) -> None:
        with self._shared_writers_lock:
            entry = self._shared_writers.get(self._writer_key)
            if entry is None:
                return
            writer, ref_count = entry
            if ref_count <= 1:
                self._shared_writers.pop(self._writer_key, None)
                writer.close()
            else:
                self._shared_writers[self._writer_key] = (writer, ref_count - 1)

    def _prepare_event_payload(self, event: TraceEvent) -> dict[str, Any]:
        event_payload = event.to_dict()
        payload = dict(event_payload.get("payload") or {})
        llm_input = payload.get("llm_input")
        if isinstance(llm_input, dict):
            detail_id = self._store_trace_detail(
                {
                    "detail_type": "llm_input",
                    "event_type": event.event_type,
                    "timestamp": event.timestamp,
                    "coara_id": event.coara_id,
                    "coara_name": event.coara_name,
                    "session_id": payload.get("session_id", ""),
                    "llm_input": llm_input,
                }
            )
            self._prune_counter += 1
            if self._prune_counter >= self._prune_interval:
                self._prune_counter = 0
                self._prune_detail_files()
            payload["llm_input_preview"] = self._build_llm_input_preview(llm_input)
            payload["llm_input_detail_id"] = detail_id
            payload.pop("llm_input", None)
        event_payload["payload"] = payload
        return event_payload

    def _store_trace_detail(self, payload: dict[str, Any]) -> str:
        detail_id = uuid.uuid4().hex
        detail_payload = {"detail_id": detail_id, **payload}
        self._writer.write_json(self._detail_path(detail_id), detail_payload)
        return detail_id

    def _detail_path(self, detail_id: str) -> Path:
        return self.details_dir / f"{detail_id}.json"

    def _is_safe_detail_id(self, detail_id: str) -> bool:
        return bool(self._DETAIL_ID_PATTERN.fullmatch(detail_id) or self._LEGACY_DETAIL_ID_PATTERN.fullmatch(detail_id))

    def _prune_detail_files(self) -> None:
        """Remove oldest detail files when count exceeds cap.

        Uses a soft-cap strategy (prune oldest 20%) to reduce the chance that
        recent JSONL entries still reference a detail file that has been deleted.
        """
        try:
            files = sorted(
                self.details_dir.glob("*.json"),
                key=lambda p: p.stat().st_mtime,
            )
            excess = len(files) - self._MAX_DETAIL_FILES
            if excess > 0:
                # Remove oldest 20% or the exact excess, whichever is larger,
                # to avoid thrashing near the boundary.
                to_remove = max(excess, len(files) // 5)
                for old_file in files[:to_remove]:
                    old_file.unlink(missing_ok=True)
        except OSError:
            pass

    def _rotate_jsonl_if_needed(self, path: Path) -> None:
        """Rotate JSONL file if it exceeds size cap; keep one backup (.1).

        Uses a counter to skip frequent disk stat() calls; the 50 MB cap is
        soft, so occasional overshoot between checks is harmless.
        """
        count = self._rotate_counter.get(path, 0) + 1
        if count < self._rotate_interval:
            self._rotate_counter[path] = count
            return
        self._rotate_counter[path] = 0
        try:
            if path.exists() and path.stat().st_size >= self._MAX_JSONL_SIZE_BYTES:
                backup = path.with_suffix(".jsonl.1")
                backup.unlink(missing_ok=True)
                path.rename(backup)
        except OSError:
            pass

    def _build_llm_input_preview(self, llm_input: dict[str, Any]) -> dict[str, Any]:
        preview: dict[str, Any] = {
            "system_prompt_preview": preview_line(
                llm_input.get("system_prompt", ""), self._DETAIL_MESSAGE_PREVIEW_LIMIT
            ),
            "last_messages": [],
        }
        raw_messages = llm_input.get("messages", [])
        if isinstance(raw_messages, list):
            preview["last_messages"] = [
                self._preview_message(message)
                for message in raw_messages[-self._DETAIL_MESSAGE_COUNT_LIMIT :]
                if isinstance(message, dict)
            ]
        return preview

    def _preview_message(self, message: dict[str, Any]) -> dict[str, Any]:
        from src.utils.message_content import message_content_to_text

        preview = {
            "role": message.get("role", ""),
            "content_preview": preview_line(
                message_content_to_text(message.get("content")), self._DETAIL_MESSAGE_PREVIEW_LIMIT
            ),
        }
        tool_call_id = message.get("tool_call_id")
        if tool_call_id:
            preview["tool_call_id"] = tool_call_id
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            preview["tool_calls"] = [
                {
                    "id": str(tool_call.get("id", "")),
                    "name": str(tool_call.get("name", "")),
                }
                for tool_call in tool_calls[: self._DETAIL_MESSAGE_COUNT_LIMIT]
                if isinstance(tool_call, dict)
            ]
        return preview

    @staticmethod
    def _atomic_write_text(path: Path, text: str) -> None:
        write_text_atomic(path, text)

    def _default_runtime_state(self) -> dict[str, Any]:
        return {
            "workspace": str(self.workspace_dir),
            "provider": "",
            "model": "",
            "running": False,
            "pid": 0,
            "updated_at": "",
            "alive": False,
        }

    def _load_runtime_state(self) -> dict[str, Any]:
        if not self.runtime_path.exists():
            return self._default_runtime_state()
        with self._writer.io_lock:
            try:
                state = json.loads(self.runtime_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                logger.warning(f"Dashboard runtime state unreadable ({self.runtime_path}): {exc}")
                return self._default_runtime_state()
        updated_at = state.get("updated_at") or ""
        alive = False
        if updated_at:
            parsed = parse_utc_datetime(updated_at)
            if parsed is not None:
                alive = parsed >= datetime.now(UTC) - timedelta(seconds=10)
        state["alive"] = bool(state.get("running")) and alive
        return state

    def _build_sessions(self, messages: list[dict[str, Any]], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """线程安全入口：并发调用经实例锁串行化，保护增量缓存的读-改-写。"""
        with self._session_lock:
            return self._build_sessions_inner(messages, events)

    def _build_sessions_inner(
        self, messages: list[dict[str, Any]], events: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """L1 会话磁带的投影：按事件类型把 messages/events 聚成会话列表。

        **唯一用途**是 ``DashboardRestHandlers._build_state`` 的 ``sessions`` 字段
        （遗留 ``/api/state``，三端均无消费方）。它**不是消息权威**：web 聊天区
        看的是视图带（逐帧落盘、带 view_seq，读端在 ``handlers/session.py``），
        两者是相互独立的来源——拿这里的会话当「用户看到过什么」会得出错误结论。
        """
        # --- Incremental session build ---
        # Persistent cache avoids rebuilding from scratch on every refresh.
        if not hasattr(self, "_session_by_key"):
            self._session_by_key: dict[tuple[str, str], dict[str, Any]] | None = None
            self._session_build_msg_idx = 0
            self._session_build_evt_idx = 0

        # Detect file truncation / reload -> invalidate cache
        if (
            self._session_by_key is None
            or self._session_build_msg_idx > len(messages)
            or self._session_build_evt_idx > len(events)
        ):
            self._session_by_key = {}
            self._session_build_msg_idx = 0
            self._session_build_evt_idx = 0
            self._session_event_keys: set[tuple[Any, ...]] = set()
            self._subagent_event_map: dict[str, list[dict[str, Any]]] = {}
        elif not hasattr(self, "_session_event_keys"):
            self._session_event_keys = set()
        if not hasattr(self, "_subagent_event_map"):
            self._subagent_event_map = {}

        had_new_data = self._session_build_msg_idx < len(messages) or self._session_build_evt_idx < len(events)

        # Process only newly appended messages
        for message in messages[self._session_build_msg_idx :]:
            key = (message.get("coara_id", ""), message.get("session_id", ""))
            session = self._session_by_key.setdefault(
                key,
                {
                    "coara_id": message.get("coara_id", ""),
                    "coara_name": message.get("coara_name", ""),
                    "session_id": message.get("session_id", ""),
                    "origin_scope": message.get("origin_scope", ""),
                    "parent_id": message.get("parent_id", ""),
                    "user_facing": bool(message.get("user_facing", False)),
                    "is_entrypoint": bool(message.get("is_entrypoint", False)),
                    "started_at": message.get("timestamp", ""),
                    "updated_at": message.get("timestamp", ""),
                    "messages": [],
                    "events": [],
                },
            )
            session["messages"].append(message)
            session["updated_at"] = max(session["updated_at"], message.get("timestamp", ""))

        # Process only newly appended events
        for event in events[self._session_build_evt_idx :]:
            dedupe_key = self._event_dedupe_key(event)
            if dedupe_key in self._session_event_keys:
                continue
            self._session_event_keys.add(dedupe_key)
            payload = event.get("payload") or {}
            session_id = payload.get("session_id")
            if not session_id:
                continue
            key = (event.get("coara_id", ""), session_id)
            session = self._session_by_key.setdefault(
                key,
                {
                    "coara_id": event.get("coara_id", ""),
                    "coara_name": event.get("coara_name", ""),
                    "session_id": session_id,
                    "origin_scope": payload.get("origin_scope", ""),
                    "parent_id": payload.get("parent_id", ""),
                    "user_facing": bool(payload.get("user_facing", False)),
                    "is_entrypoint": bool(payload.get("is_entrypoint", False)),
                    "started_at": event.get("timestamp", ""),
                    "updated_at": event.get("timestamp", ""),
                    "messages": [],
                    "events": [],
                },
            )
            session["events"].append(event)
            session["updated_at"] = max(session["updated_at"], event.get("timestamp", ""))
            if payload.get("origin_scope") == "subagent_loop":
                sid = str(payload.get("session_id", ""))
                if sid:
                    self._subagent_event_map.setdefault(sid, []).append(event)
            if not session.get("origin_scope"):
                session["origin_scope"] = payload.get("origin_scope", "")
            if not session.get("parent_id"):
                session["parent_id"] = payload.get("parent_id", "")
            session["user_facing"] = bool(session.get("user_facing")) or bool(payload.get("user_facing", False))
            session["is_entrypoint"] = bool(session.get("is_entrypoint")) or bool(payload.get("is_entrypoint", False))

        self._session_build_msg_idx = len(messages)
        self._session_build_evt_idx = len(events)

        if had_new_data or not getattr(self, "_subagent_sessions_attached", False):
            self._attach_subagent_sessions()
            self._subagent_sessions_attached = True

        sessions = list(self._session_by_key.values())
        # Subagent loops are embedded in parent turns via subagent_sessions; keep them out of the main list.
        sessions = [s for s in sessions if s.get("origin_scope") != "subagent_loop"]
        sessions.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
        return sessions

    @staticmethod
    def _event_dedupe_key(event: dict[str, Any]) -> tuple[Any, ...]:
        payload = event.get("payload") or {}
        content = payload.get("content") or payload.get("content_preview") or event.get("message") or ""
        # content 换 hash：去重语义不变，但 _written_event_keys 不再驻留完整
        # 正文字符串（长会话下 key 体积从内容大小降为定长 64 字符）
        content_hash = hashlib.sha256(str(content).encode("utf-8")).hexdigest()
        event_type = event.get("event_type", "")
        base = (
            event_type,
            event.get("coara_id", ""),
            payload.get("session_id", ""),
        )
        if event_type == "conversation_message":
            return base + (payload.get("role", ""), content_hash)
        return base + (
            event.get("timestamp", ""),
            payload.get("tool_call_id", ""),
            payload.get("task_id", ""),
            payload.get("subagent_id", ""),
            payload.get("iteration", ""),
            payload.get("role", ""),
            content_hash,
        )

    def _attach_subagent_sessions(self) -> None:
        subagent_event_map = self._subagent_event_map
        for session in self._session_by_key.values():
            session["subagent_sessions"] = []
            for event in session.get("events", []):
                payload = event.get("payload") or {}
                child_session_id = payload.get("child_session_id")
                if child_session_id and child_session_id in subagent_event_map:
                    raw_type = str(payload.get("subagent_type", "") or "")
                    session["subagent_sessions"].append(
                        {
                            "subagent_type": raw_type,
                            "subagent_id": payload.get("subagent_id", ""),
                            "description": payload.get("description", ""),
                            "child_session_id": child_session_id,
                            "child_coara_id": payload.get("child_coara_id", ""),
                            "events": list(subagent_event_map.get(child_session_id, [])),
                        }
                    )

    def reset_session_cache(self) -> None:
        """Drop incremental session builder state (e.g. after JSONL rotation)."""
        with self._session_lock:
            self._session_by_key = None
            self._subagent_sessions_attached = False
            if hasattr(self, "_session_event_keys"):
                del self._session_event_keys
            if hasattr(self, "_subagent_event_map"):
                del self._subagent_event_map

    def _load_jsonl(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        # 锁内只读原始文本，JSON 解析挪到锁外（大文件全量读不再长时间阻塞写线程）
        with self._writer.io_lock, path.open("r", encoding="utf-8", errors="replace") as handle:
            raw_text = handle.read()
        return _parse_jsonl_text(raw_text)

    @staticmethod
    def _jsonl_size(path: Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return 0
