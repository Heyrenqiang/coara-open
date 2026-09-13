"""File-system watch event source."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from src.core.logger import logger
from src.event_sources.dedupe import path_dedupe_key
from src.event_sources.types import EventSourceDefinition, InboundEvent
from src.workspace.vfs import VfsResolver

EmitCallback = Callable[[InboundEvent], Awaitable[None]]
_DEBOUNCE_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="file-watch-debounce")


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
        self._generation: dict[str, int] = {}
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
        with self._lock:
            generation = self._generation.get(key, 0) + 1
            self._generation[key] = generation

        def _fire(expected_generation: int) -> None:
            time.sleep(self.debounce_seconds)
            with self._lock:
                if self._generation.get(key) != expected_generation:
                    return
                self._generation.pop(key, None)
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

        _DEBOUNCE_EXECUTOR.submit(_fire, generation)

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
        self._observer: Observer | None = None

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
