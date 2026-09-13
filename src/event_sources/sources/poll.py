"""Interval poll event source."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

from src.core.logger import logger
from src.event_sources.dedupe import paths_dedupe_key
from src.event_sources.types import EventSourceDefinition, InboundEvent
from src.workspace.vfs import VfsResolver

EmitCallback = Callable[[InboundEvent], Awaitable[None]]


class IntervalPollSource:
    def __init__(
        self,
        definition: EventSourceDefinition,
        *,
        vfs: VfsResolver,
        emit: EmitCallback,
        get_seen: Callable[[], set[str]],
        save_seen: Callable[[set[str]], None],
    ):
        self.definition = definition
        self.vfs = vfs
        self.emit = emit
        self.get_seen = get_seen
        self.save_seen = save_seen
        self._task: asyncio.Task | None = None
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None

    async def _loop(self) -> None:
        interval = float(self.definition.interval_seconds or 60.0)
        while self._running:
            try:
                await self._scan_once()
            except Exception as exc:
                logger.warning(f"Poll source '{self.definition.id}' error: {exc}")
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break

    async def _scan_once(self) -> None:
        rel = self.definition.watch_path or "."
        resolved = self.vfs.resolve_in_workspace(
            self.definition.workspace,
            rel,
            action="poll",
        )
        target = resolved.path
        if not target.exists():
            return
        names = {target.name} if target.is_file() else {p.name for p in target.iterdir() if p.is_file()}
        previous = self.get_seen()
        new_names = sorted(names - previous)
        if len(new_names) < self.definition.poll_min_count:
            return
        new_paths = [str(target / name) for name in new_names]
        try:
            await self.emit(
                InboundEvent(
                    source_id=self.definition.id,
                    workspace=self.definition.workspace,
                    event_type="poll.new_files",
                    payload={"paths": new_paths, "count": len(new_names)},
                    dedupe_key=paths_dedupe_key(self.definition.workspace, new_paths),
                )
            )
        except Exception as exc:
            logger.warning(f"Poll source '{self.definition.id}' emit failed: {exc}")
            return
        self.save_seen(names)
