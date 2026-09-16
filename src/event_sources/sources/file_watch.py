"""File-system watch event source."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from src.core.logger import logger
from src.event_sources.dedupe import path_dedupe_key
from src.event_sources.types import EventSourceDefinition, InboundEvent
from src.workspace.vfs import VfsResolver

EmitCallback = Callable[[InboundEvent], Coroutine[Any, Any, None]]


class _DebouncedHandler(FileSystemEventHandler):
    def __init__(
        self,
        *,
        definition: EventSourceDefinition,
        watch_dir: Path,
        loop: asyncio.AbstractEventLoop,
        emit: EmitCallback,
        debounce_seconds: float = 0.5,
    ):
        super().__init__()
        self.definition = definition
        self.watch_dir = watch_dir
        self.loop = loop
        self.emit = emit
        self.debounce_seconds = debounce_seconds
        # 去抖用 threading.Timer：延时在 timer 自己的守护线程里度过，
        # 不占任何共享工作线程池，慢路径（网络盘 resolve）不会堵死后续事件。
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def on_created(self, event) -> None:
        if event.is_directory:
            return
        if "created" not in self.definition.watch_events:
            return
        self._schedule(event.src_path, "file.created")

    def on_modified(self, event) -> None:
        if event.is_directory:
            return
        if "modified" not in self.definition.watch_events:
            return
        self._schedule(event.src_path, "file.modified")

    def _schedule(self, src_path: str, event_type: str) -> None:
        path = Path(src_path)
        if not self._matches_pattern(path.name):
            return
        key = str(path.resolve())

        fired_timer: threading.Timer | None = None

        def _fire() -> None:
            # 锁内原子收编：若本 timer 已不是登记在册者（被同 key 新事件替换），
            # 直接放弃——等价于旧 generation 失配；身份比对在锁内完成，无错位窗口。
            with self._lock:
                if self._timers.get(key) is not fired_timer:
                    return
                self._timers.pop(key, None)
            coro = self.emit(
                InboundEvent(
                    source_id=self.definition.id,
                    workspace=self.definition.workspace,
                    event_type=event_type,
                    payload={"path": str(path), "watch_dir": str(self.watch_dir)},
                    dedupe_key=path_dedupe_key(self.definition.workspace, path),
                )
            )
            try:
                asyncio.run_coroutine_threadsafe(coro, self.loop)
            except RuntimeError as exc:
                coro.close()
                logger.warning(
                    f"FileWatch '{self.definition.id}': dropped {event_type} for {path} — event loop unavailable: {exc}"
                )

        timer = threading.Timer(self.debounce_seconds, _fire)
        timer.daemon = True
        fired_timer = timer
        with self._lock:
            old = self._timers.get(key)
            self._timers[key] = timer
        if old is not None:
            old.cancel()
        timer.start()

    def _matches_pattern(self, name: str) -> bool:
        from fnmatch import fnmatch

        return fnmatch(name, self.definition.watch_pattern)


class FileWatchSource:
    def __init__(
        self,
        definition: EventSourceDefinition,
        *,
        vfs: VfsResolver,
        emit: EmitCallback,
        loop: asyncio.AbstractEventLoop,
    ):
        self.definition = definition
        self.vfs = vfs
        self.emit = emit
        self.loop = loop
        # watchdog 的 Observer 是平台选择出的变量别名，不是可用作类型的类
        self._observer: Observer | None = None  # type: ignore[valid-type]

    def start(self) -> None:
        rel = self.definition.watch_path or "."
        resolved = self.vfs.resolve_in_workspace(
            self.definition.workspace,
            rel,
            action="watch",
        )
        watch_dir = resolved.path
        watch_dir.mkdir(parents=True, exist_ok=True)
        handler = _DebouncedHandler(
            definition=self.definition,
            watch_dir=watch_dir,
            loop=self.loop,
            emit=self.emit,
        )
        observer = Observer()
        observer.schedule(handler, str(watch_dir), recursive=False)
        observer.start()
        self._observer = observer
        logger.info(f"FileWatchSource '{self.definition.id}' watching {watch_dir}")

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=2)
            self._observer = None
