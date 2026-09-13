"""Unified background task store — persists all background tasks (bash + agent).

Background task runtime state:
- Single source of truth for background task runtime state
- Unified status enum across bash and agent tasks
- Stored as JSON under .coara/tasks/{task_id}.json
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from src.core.json_store import write_json_atomic
from src.core.logger import logger
from src.core.time import now_iso


class TaskKind(StrEnum):
    """Background task kind."""

    BASH = "bash"
    AGENT = "agent"


class TaskStatus(StrEnum):
    """Unified lifecycle status for all background tasks."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    KILLED = "killed"
    TIMED_OUT = "timed_out"


@dataclass
class TaskRecord:
    """Serialized state of a background task."""

    task_id: str
    kind: str  # TaskKind value
    description: str
    status: str  # TaskStatus value
    created_at: str
    updated_at: str
    completed_at: str | None = None
    # bash-specific
    command: str | None = None
    output_path: str | None = None
    exit_code: int | None = None
    pid: int | None = None
    # agent-specific
    subagent_type: str | None = None
    agent_id: str | None = None
    # common
    error: str | None = None
    result_preview: str = ""
    # agent-specific full result（后台子智能体完整最终结果；bash 完整在 output.log）
    result_full: str = ""
    interrupted: bool = False
    timed_out: bool = False
    # Frontend that launched this task ("matrix" / "web" / "cli")
    origin_source: str = ""
    # Set by startup recovery when the recorded pid was still alive (the
    # runner died but the detached subprocess kept running)
    orphan_process_alive: bool = False


def _orphan_pid_alive(pid: int, created_at: str) -> bool:
    """True when the recorded pid still refers to a live process.

    pid reuse guard: the process must have started no earlier than (roughly)
    the task record creation — an older process means the pid was recycled
    and killing it would hit an innocent bystander.
    """
    try:
        import psutil

        if not psutil.pid_exists(pid):
            return False
        proc = psutil.Process(pid)
        try:
            proc_started = proc.create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False
        try:
            from datetime import datetime

            created_ts = datetime.fromisoformat(created_at).timestamp()
        except (TypeError, ValueError):
            created_ts = 0.0
        if created_ts and proc_started < created_ts - 60.0:
            logger.warning(
                f"Orphan probe: pid {pid} predates task record (started {proc_started:.0f} < {created_ts:.0f}); "
                "assuming pid reuse, skipping kill"
            )
            return False
        return True
    except Exception as exc:
        logger.warning(f"Orphan probe failed for pid {pid}: {exc}")
        return False


def _kill_process_tree(pid: int) -> None:
    """Force-kill a process and its children (Windows taskkill /T equivalent)."""
    import contextlib

    import psutil

    proc = psutil.Process(pid)
    children = proc.children(recursive=True)
    for child in children:
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            child.kill()
    proc.kill()
    psutil.wait_procs([proc, *children], timeout=5)


def _reap_orphan_process(store: TaskStore, record: TaskRecord) -> None:
    """Best-effort kill of a leftover bash subprocess after a runner restart.

    Never raises: a failed kill only logs — startup recovery must not break
    because of an unkillable process.
    """
    if record.kind != TaskKind.BASH.value or record.pid is None:
        return
    pid = record.pid
    try:
        if not _orphan_pid_alive(pid, record.created_at):
            return
        store.update(record.task_id, orphan_process_alive=True)
        logger.warning(
            f"Orphan bash process still alive after restart: task={record.task_id} pid={pid}; attempting to terminate"
        )
        try:
            _kill_process_tree(pid)
            logger.info(f"Terminated orphan bash process tree: task={record.task_id} pid={pid}")
        except Exception as exc:
            logger.warning(f"Failed to terminate orphan bash process pid={pid}: {exc}")
    except Exception as exc:
        logger.warning(f"Orphan process reaping failed for {record.task_id}: {exc}")


class TaskStore:
    """Persist and query background task instances.

    Storage layout: .coara/tasks/{task_id}.json
    Uses a per-instance in-memory cache to avoid repeated disk reads.
    """

    _MAX_RECORDS = 500

    def __init__(self, base_dir: Path | None = None) -> None:
        if base_dir is None:
            base_dir = Path.cwd() / ".coara"
        base_path = Path(base_dir).expanduser().resolve()
        # Accept either the `.coara` root or a workspace directory.
        self._storage_dir = base_path / "tasks" if base_path.name == ".coara" else base_path / ".coara" / "tasks"
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, TaskRecord] = {}
        # RLock: update() holds the lock across load-modify-save while save()
        # and _prune_records() re-acquire it internally.
        self._cache_lock = threading.RLock()
        self._cache_initialized = False
        self._ensure_cache_loaded()

    @classmethod
    def recover_stale_running(cls, *coara_roots: Path | None, coara_home: Path | None = None) -> int:
        """Mark orphaned ``running`` tasks as failed after a process restart.

        被回收的任务按工作空间写入重启清算通知（restart_notice.json），
        下次该空间会话恢复时注入历史，让模型知道这些任务不会再有结果。
        """
        from src.core.time import now_iso

        recovered = 0
        seen: set[str] = set()
        seen_dirs: set[str] = set()
        notice_items: dict[str, tuple[Path, list[str]]] = {}
        for root in coara_roots:
            if root is None:
                continue
            coara_dir = Path(root).expanduser().resolve()
            if coara_dir.name != ".coara":
                coara_dir = coara_dir / ".coara"
            if not (coara_dir / "tasks").exists():
                continue
            dir_key = str(coara_dir).lower()
            if dir_key in seen_dirs:
                continue
            seen_dirs.add(dir_key)
            store = cls(coara_dir)
            for record in store.list_all():
                if record.task_id in seen:
                    continue
                seen.add(record.task_id)
                if record.status != TaskStatus.RUNNING.value:
                    continue
                store.update(
                    record.task_id,
                    status=TaskStatus.FAILED.value,
                    error="Process restarted while task was running",
                    completed_at=now_iso(),
                )
                recovered += 1
                logger.info(f"Recovered stale background task: {record.task_id}")
                _reap_orphan_process(store, record)
                ws_dir = coara_dir.parent
                desc = (record.description or record.command or "").strip()
                if len(desc) > 60:
                    desc = desc[:60] + "…"
                notice_items.setdefault(dir_key, (ws_dir, []))[1].append(
                    f"后台任务 {record.task_id}（{desc}）" if desc else f"后台任务 {record.task_id}"
                )
        for _key, (ws_dir, items) in notice_items.items():
            try:
                # 装配点豁免：重启清算通知是「后台任务恢复 ↔ 会话恢复协议」的交接，
                # restart_notice 文件归 workspace_state 拥有（含 coara 注入逻辑，无法下沉），
                # background 只在此一处写通知、不读其内部——属分层中允许的恢复协议边界。
                from src.coara.workspace_state import append_restart_notice

                append_restart_notice(ws_dir, items, coara_home=coara_home)
            except Exception as exc:
                logger.warning(f"Failed to write restart notice for {ws_dir}: {exc}")
        return recovered

    def _ensure_cache_loaded(self) -> None:
        """Lazy-load all records from disk into this instance's cache once."""
        if self._cache_initialized:
            return
        with self._cache_lock:
            if self._cache_initialized:
                return
            for path in self._storage_dir.glob("*.json"):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    record = TaskRecord(**data)
                    self._cache[record.task_id] = record
                except Exception as exc:
                    logger.warning(f"TaskStore skipped corrupt file {path.name}: {exc}")
            self._cache_initialized = True

    def _persist_record(self, record: TaskRecord) -> None:
        """Write a single record to disk."""
        path = self._storage_dir / f"{record.task_id}.json"
        try:
            write_json_atomic(path, asdict(record))
        except Exception as exc:
            logger.warning(f"TaskStore failed to persist {record.task_id}: {exc}")

    def _path_for(self, task_id: str) -> Path:
        return self._storage_dir / f"{task_id}.json"

    def save(self, record: TaskRecord) -> None:
        """Persist a task record to disk (upsert)."""
        with self._cache_lock:
            self._cache[record.task_id] = record
        self._persist_record(record)
        logger.debug(f"TaskStore saved {record.task_id} ({record.status})")
        self._prune_records()

    def load(self, task_id: str) -> TaskRecord | None:
        """Load a task record from cache (fallback to disk on miss)."""
        with self._cache_lock:
            cached = self._cache.get(task_id)
        if cached is not None:
            return cached
        # Cache miss: try disk (record may have been written by another process)
        path = self._path_for(task_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            record = TaskRecord(**data)
            with self._cache_lock:
                self._cache[task_id] = record
            return record
        except Exception as exc:
            logger.warning(f"TaskStore failed to load {task_id}: {exc}")
            return None

    def update(self, task_id: str, **fields: Any) -> bool:
        """Update specific fields of an existing task record.

        Returns True if the record existed and was updated.
        """
        # Hold the lock across the whole load-modify-save so concurrent
        # updates cannot clobber each other's fields (RLock: save/load/
        # _prune_records re-acquire it internally; persist IO takes no locks).
        with self._cache_lock:
            record = self._cache.get(task_id)
            if record is None:
                record = self.load(task_id)
                if record is None:
                    return False
            for key, value in fields.items():
                if hasattr(record, key):
                    setattr(record, key, value)
            record.updated_at = now_iso()
            self.save(record)
            return True

    def delete(self, task_id: str) -> bool:
        """Remove a persisted task record."""
        with self._cache_lock:
            self._cache.pop(task_id, None)
        path = self._path_for(task_id)
        if not path.exists():
            return False
        try:
            path.unlink()
            logger.debug(f"TaskStore deleted {task_id}")
            return True
        except Exception as exc:
            logger.warning(f"TaskStore failed to delete {task_id}: {exc}")
            return False

    def list_all(self) -> list[TaskRecord]:
        """List all persisted task records, newest first."""
        with self._cache_lock:
            records = list(self._cache.values())
        records.sort(key=lambda r: r.updated_at or r.created_at, reverse=True)
        return records

    def list_active(self) -> list[TaskRecord]:
        """List tasks with RUNNING status, newest first."""
        with self._cache_lock:
            return sorted(
                [r for r in self._cache.values() if r.status == TaskStatus.RUNNING.value],
                key=lambda r: r.updated_at or r.created_at,
                reverse=True,
            )

    def _prune_records(self) -> None:
        """Limit the number of persisted records to prevent storage explosion."""
        with self._cache_lock:
            total = len(self._cache)
            if total <= self._MAX_RECORDS:
                return
            # Sort by updated_at, evict oldest idle/completed records first
            sorted_records = sorted(
                self._cache.values(),
                key=lambda r: (r.updated_at or r.created_at, r.status != TaskStatus.RUNNING.value),
            )
            to_remove = sorted_records[: total - self._MAX_RECORDS]
            for record in to_remove:
                if record.status == TaskStatus.RUNNING.value:
                    continue
                task_id = record.task_id
                self._cache.pop(task_id, None)
                path = self._path_for(task_id)
                try:
                    if path.exists():
                        path.unlink()
                        logger.debug(f"TaskStore pruned {task_id}")
                except Exception as exc:
                    logger.warning(f"TaskStore failed to prune {task_id}: {exc}")
