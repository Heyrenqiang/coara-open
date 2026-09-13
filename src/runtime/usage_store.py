"""Append-only usage event store with a background writer (hot path: queue only)."""

from __future__ import annotations

import contextlib
import json
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.core.coara_home import CoaraHomePaths
from src.core.logger import logger
from src.core.time import utc_now_iso

_EVENTS_PATH_KEY = "_events_path"


@dataclass(slots=True)
class UsageStoreConfig:
    enabled: bool = True
    max_queue: int = 8192


def load_usage_store_config(raw_config: dict[str, Any] | None) -> UsageStoreConfig:
    usage = (raw_config or {}).get("usage") or {}
    enabled = usage.get("enabled", True)
    max_queue = int(usage.get("max_queue", 8192) or 8192)
    return UsageStoreConfig(enabled=bool(enabled), max_queue=max(256, max_queue))


def resolve_usage_events_path(workspace_dir: Path, *, coara_home: Path | None = None) -> Path:
    paths = CoaraHomePaths.for_workspace(workspace_dir, configured_home=coara_home, migrate=False)
    return paths.usage_dir / "events.jsonl"


class UsageStore:
    """Non-blocking usage recorder: sync enqueue, async JSONL append.

    Each record may target a different ``events.jsonl`` via ``events_path=``;
    the constructor path is the fallback (foreground workspace at attach time).
    """

    def __init__(self, events_path: Path, *, config: UsageStoreConfig | None = None) -> None:
        self._config = config or UsageStoreConfig()
        self._events_path = events_path
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=self._config.max_queue)
        self._dropped = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="usage-store-writer", daemon=True)
        self._thread.start()

    @property
    def events_path(self) -> Path:
        return self._events_path

    @property
    def dropped_events(self) -> int:
        return self._dropped

    def record(self, event: dict[str, Any], *, events_path: Path | None = None) -> None:
        if not self._config.enabled:
            return
        payload = dict(event)
        payload.setdefault("ts", utc_now_iso())
        target = events_path if events_path is not None else self._events_path
        payload[_EVENTS_PATH_KEY] = str(target)
        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            self._dropped += 1
            # 首次丢弃告警一次（此后按计数暴露，不刷日志）：高负载下账面会比
            # provider 实际扣费少，此前静默丢、完全不可观测。
            if self._dropped == 1:
                logger.warning("UsageStore queue full; dropping usage events (dropped count in `coara usage summary`)")

    def flush(self, timeout: float = 2.0) -> None:
        if not self._config.enabled:
            return
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks:
            if not self._thread.is_alive():
                logger.warning(
                    "UsageStore writer thread is not alive; flush aborted with {} pending event(s)",
                    self._queue.unfinished_tasks,
                )
                return
            if time.monotonic() >= deadline:
                return
            time.sleep(0.05)

    def close(self, timeout: float = 2.0) -> None:
        if not self._thread.is_alive():
            return
        self._stop.set()
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(None)
        self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if item is None:
                self._queue.task_done()
                break
            try:
                self._append(item)
            except Exception as exc:
                logger.debug("UsageStore append failed: {}", exc)
            finally:
                self._queue.task_done()

    def _append(self, record: dict[str, Any]) -> None:
        path_raw = record.pop(_EVENTS_PATH_KEY, None)
        target = Path(path_raw) if path_raw else self._events_path
        target.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        self._maybe_rotate(target)

    def _maybe_rotate(self, target: Path) -> None:
        """按大小轮转 events.jsonl（与 TraceStore / session_log 同款防无限增长）。

        超过阈值时把当前文件另存为 ``events.<时间戳>.jsonl``（多代保留、从不删除），
        从空文件续写。读侧（usage_query）会把 usage 目录下**所有** ``events*.jsonl*``
        一起统计，所以轮转不改变任何统计语义——归档不是丢弃历史。
        """
        try:
            size = target.stat().st_size
        except OSError:
            return
        if size < _USAGE_ROTATE_MAX_BYTES:
            return
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        archive = target.with_name(f"{target.stem}.{stamp}{target.suffix}")
        if archive.exists():
            # 同一秒内二次轮转（阈值调小/写入极快时才可能）：加毫秒后缀避让，绝不覆盖
            archive = target.with_name(f"{target.stem}.{stamp}{int(time.time() * 1000) % 1000:03d}{target.suffix}")
        try:
            target.replace(archive)
        except OSError as exc:
            logger.debug("UsageStore rotate failed: {}", exc)


_USAGE_ROTATE_MAX_BYTES = 32 * 1024 * 1024


__all__ = [
    "UsageStore",
    "UsageStoreConfig",
    "load_usage_store_config",
    "resolve_usage_events_path",
]
