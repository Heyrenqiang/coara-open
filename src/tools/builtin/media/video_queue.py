"""Global video generation task queue.

A persistent FIFO queue (JSON files under ``<coara_home>/runtime/video_queue/``)
that serializes video generation across the process. ``enqueue`` adds a task;
the scheduler runs **one job at a time** (submit→poll→download), then spaces the
next submit by its cadence. On failure the task is re-enqueued to the tail
(``attempts``+1) until :data:`MAX_ATTEMPTS`, then fails permanently.

All writes are atomic (temp + replace) under a cross-process file lock so the
manager process and the CLI process can never clobber each other.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from src.core.json_store import write_json_atomic
from src.core.logger import logger

MAX_ATTEMPTS = 3
_TERMINAL = frozenset({"done", "failed", "cancelled"})
_MAX_TERMINAL_RECORDS = 50
_QUEUE_DIRNAME = "video_queue"
_LOCK_FILENAME = "video_queue.lock"


@dataclass
class VideoTask:
    """Serialized state of one queued / running video generation task."""

    task_id: str
    prompt: str
    params: dict[str, Any] = field(default_factory=dict)
    out: str = ""
    status: str = "queued"
    attempts: int = 0
    enqueued_at: float = 0.0
    error: str | None = None
    result: dict[str, Any] | None = None
    origin_source: str = ""
    description: str = ""
    coara_id: str = ""
    session_id: str = ""
    video_id: str | None = None


def _runtime_root() -> Path:
    """Resolve ``<coara_home>/runtime`` the same way the rest of coara does.

    Prefer merged config ``coara_home``, then bootstrap/env ``COARA_HOME``,
    else ``cwd/.coara`` — never silently ignore a configured home when the
    process env var is unset.
    """
    try:
        from src.core.coara_home import resolve_bootstrap_coara_home, resolve_config_home

        try:
            from src.core.config import config_manager

            raw = getattr(config_manager, "_raw_config", None)
            if isinstance(raw, dict) and raw:
                return Path(resolve_config_home(raw)).expanduser().resolve() / "runtime"
        except Exception:
            logger.debug("config coara_home 读取失败，回落 bootstrap 解析")
        bootstrap = resolve_bootstrap_coara_home()
        if bootstrap is not None:
            return Path(bootstrap).expanduser().resolve() / "runtime"
    except Exception:
        logger.debug("coara_home 解析失败，回落 COARA_HOME env / cwd/.coara")
    home = os.environ.get("COARA_HOME", "").strip()
    if home:
        return Path(home).expanduser().resolve() / "runtime"
    return (Path.cwd() / ".coara" / "runtime").resolve()


@contextlib.contextmanager
def _file_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)  # type: ignore[attr-defined]  # POSIX 专有，Windows 走 msvcrt 分支
        yield
    finally:
        with contextlib.suppress(OSError):
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]  # POSIX 专有，Windows 走 msvcrt 分支
        handle.close()


class VideoQueue:
    """Persistent FIFO queue for video generation tasks."""

    def __init__(self, base_dir: Path | None = None):
        root = Path(base_dir) if base_dir else _runtime_root()
        self._dir = root / _QUEUE_DIRNAME
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock_path = root / _LOCK_FILENAME

    def _path(self, task_id: str) -> Path:
        return self._dir / f"{task_id}.json"

    def _load(self, task_id: str) -> VideoTask | None:
        path = self._path(task_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return VideoTask(**data)
        except Exception:
            return None

    def _save(self, task: VideoTask) -> None:
        write_json_atomic(self._path(task.task_id), asdict(task))

    def _prune_terminals_unlocked(self, keep: int = _MAX_TERMINAL_RECORDS) -> int:
        """Drop oldest terminal records beyond ``keep`` (caller holds the lock)."""
        terminals: list[tuple[float, Path]] = []
        for path in self._dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if str(data.get("status") or "") not in _TERMINAL:
                    continue
                terminals.append((float(data.get("enqueued_at") or 0.0), path))
            except Exception:
                continue
        if len(terminals) <= keep:
            return 0
        terminals.sort(key=lambda item: item[0])
        dropped = 0
        for _ts, path in terminals[: len(terminals) - keep]:
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
                dropped += 1
        return dropped

    @classmethod
    def _status_rank(cls, status: str) -> int:
        # queued first (ready to run), then running, then terminal states.
        return {"queued": 0, "running": 1, "pending": 0}.get(status, 2)

    def enqueue(
        self,
        prompt: str,
        params: dict[str, Any] | None = None,
        *,
        out: str = "",
        origin_source: str = "",
        description: str = "",
        coara_id: str = "",
        session_id: str = "",
    ) -> str:
        """Add a task to the queue tail; returns ``task_id``."""
        task = VideoTask(
            task_id=f"vq-{uuid.uuid4().hex[:10]}",
            prompt=prompt,
            params=dict(params or {}),
            out=out,
            status="queued",
            attempts=0,
            enqueued_at=time.time(),
            origin_source=origin_source,
            description=description,
            coara_id=coara_id,
            session_id=session_id,
        )
        with _file_lock(self._lock_path):
            self._save(task)
            self._prune_terminals_unlocked()
        return task.task_id

    def get(self, task_id: str) -> VideoTask | None:
        with _file_lock(self._lock_path):
            return self._load(task_id)

    def status(self) -> list[dict[str, Any]]:
        """Summarize all tasks, queued first then by enqueue time."""
        with _file_lock(self._lock_path):
            tasks: list[VideoTask] = []
            for path in self._dir.glob("*.json"):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    tasks.append(VideoTask(**data))
                except Exception:
                    continue
        tasks.sort(key=lambda t: (self._status_rank(t.status), t.enqueued_at))
        return [
            {
                "task_id": t.task_id,
                "status": t.status,
                "attempts": t.attempts,
                "max_attempts": MAX_ATTEMPTS,
                "out": t.out,
                "description": t.description,
                "error": t.error,
                "enqueued_at": t.enqueued_at,
            }
            for t in tasks
        ]

    def pop_next(self) -> VideoTask | None:
        """Pop the head queued task and mark it ``running``; None if none."""
        with _file_lock(self._lock_path):
            ready: list[tuple[VideoTask, Path]] = []
            for path in self._dir.glob("*.json"):
                try:
                    task = VideoTask(**json.loads(path.read_text(encoding="utf-8")))
                except Exception:
                    continue
                if task.status == "queued":
                    ready.append((task, path))
            if not ready:
                return None
            task, _path = min(ready, key=lambda tp: tp[0].enqueued_at)
            task.status = "running"
            self._save(task)
            return task

    def has_running(self) -> bool:
        with _file_lock(self._lock_path):
            for path in self._dir.glob("*.json"):
                try:
                    task = VideoTask(**json.loads(path.read_text(encoding="utf-8")))
                except Exception:
                    continue
                if task.status == "running":
                    return True
        return False

    def requeue_tail(self, task_id: str, error: str | None = None) -> None:
        """Fail a running task and move it to the tail (attempts+1)."""
        with _file_lock(self._lock_path):
            task = self._load(task_id)
            if task is None:
                return
            task.attempts += 1
            task.status = "queued"
            task.enqueued_at = time.time()  # back to the tail
            task.error = error
            self._save(task)

    def requeue_for_download_retry(self, task_id: str, error: str, download_retries: int) -> None:
        """视频已生成仅下载失败：重排队尾重试下载，不消耗生成 attempts。

        用 params 里的 ``_download_retries`` 独立计数（与生成 attempts 解耦），
        video_id 已落盘，重跑直接进 poll 重新下载，不产生新费用。
        """
        with _file_lock(self._lock_path):
            task = self._load(task_id)
            if task is None:
                return
            task.params["_download_retries"] = download_retries
            task.status = "queued"
            task.enqueued_at = time.time()
            task.error = error
            self._save(task)

    def set_video_id(self, task_id: str, video_id: str) -> None:
        """Persist the submitted ``video_id`` so a restart resumes polling instead of re-submitting."""
        with _file_lock(self._lock_path):
            task = self._load(task_id)
            if task is None:
                return
            task.video_id = video_id
            self._save(task)

    def mark_done(self, task_id: str, result: dict[str, Any]) -> None:
        with _file_lock(self._lock_path):
            task = self._load(task_id)
            if task is None:
                return
            task.status = "done"
            task.result = dict(result)
            self._save(task)
            self._prune_terminals_unlocked()

    def mark_failed(self, task_id: str, error: str) -> None:
        with _file_lock(self._lock_path):
            task = self._load(task_id)
            if task is None:
                return
            task.status = "failed"
            task.error = error
            self._save(task)
            self._prune_terminals_unlocked()

    def cancel(self, task_id: str) -> bool:
        """Cancel a queued/running task (remove it from the pipeline)."""
        with _file_lock(self._lock_path):
            task = self._load(task_id)
            if task is None or task.status in _TERMINAL:
                return False
            task.status = "cancelled"
            task.error = "cancelled"
            self._save(task)
            self._prune_terminals_unlocked()
            return True

    def reset_running(self, *, skip_ids: set[str] | None = None) -> int:
        """On startup recovery: reset orphan ``running`` tasks back to ``queued``.

        ``skip_ids`` are still actively polled in this process and must not be
        reset (would duplicate submit).
        """
        skip = skip_ids or set()
        n = 0
        with _file_lock(self._lock_path):
            for path in self._dir.glob("*.json"):
                try:
                    task = VideoTask(**json.loads(path.read_text(encoding="utf-8")))
                except Exception:
                    continue
                if task.status == "running" and task.task_id not in skip:
                    task.status = "queued"
                    self._save(task)
                    n += 1
        return n

    def has_pending(self) -> bool:
        with _file_lock(self._lock_path):
            for path in self._dir.glob("*.json"):
                try:
                    task = VideoTask(**json.loads(path.read_text(encoding="utf-8")))
                except Exception:
                    continue
                if task.status == "queued":
                    return True
        return False
