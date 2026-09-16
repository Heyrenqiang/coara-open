"""Shared Matrix sync loop and non-blocking message dispatch helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from src.core.logger import logger

# User-facing notice when a message is dropped after the dispatcher busy timeout.
BUSY_DROP_NOTICE = "正在处理上一条消息，请稍后再发，或用 /stop 中断当前回合。"

SyncTokenCallback = Callable[[str], None]

# Generation id active while nio invokes room callbacks inside ``client.sync()``.
_current_sync_gen: ContextVar[int | None] = ContextVar("matrix_sync_gen", default=None)


async def run_matrix_sync_loop(
    client: Any,
    *,
    on_batch_saved: SyncTokenCallback | None = None,
    on_sync_error: Callable[[object], None] | None = None,
    token_path: Path | None = None,
    timeout_ms: int = 30_000,
    retry_seconds: float = 5.0,
    max_retry_seconds: float = 60.0,
    label: str = "Matrix",
    dispatcher: MatrixMessageDispatcher | None = None,
) -> None:
    """Long-poll /sync with retry. Does not process events — nio invokes callbacks during sync.

    When *dispatcher* is set, the durable sync token advances only after that batch's
    scheduled handlers finish successfully (applied cursor). Sync itself keeps using
    the in-memory ``next_batch`` so long-poll is not blocked on long turns.
    """
    from src.matrix_client.sync_token import (
        is_invalid_sync_token_error,
        reset_gomatrix_sync_token,
        save_gomatrix_sync_token,
    )

    backoff = retry_seconds
    while True:
        try:
            gen: int | None = None
            gen_token = None
            if dispatcher is not None:
                gen = dispatcher.begin_generation()
                gen_token = _current_sync_gen.set(gen)
            try:
                resp = await client.sync(timeout=timeout_ms, full_state=False)
            finally:
                if dispatcher is not None and gen is not None:
                    dispatcher.close_generation(gen)
                if gen_token is not None:
                    _current_sync_gen.reset(gen_token)

            if isinstance(resp, Exception):
                logger.warning("[%s] Sync error: %r", label, resp)
                if token_path is not None and is_invalid_sync_token_error(resp):
                    reset_gomatrix_sync_token(client, token_path)
                    # Re-park at tip without relying on historic timeline delivery.
                    await park_sync_token_without_timeline(
                        client,
                        token_path=token_path,
                        label=label,
                        timeout_ms=timeout_ms,
                    )
                if on_sync_error:
                    on_sync_error(resp)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_retry_seconds)
                continue

            backoff = retry_seconds
            batch = getattr(client, "next_batch", None)
            if batch:
                if dispatcher is not None and gen is not None:
                    dispatcher.schedule_token_persist(
                        gen,
                        str(batch),
                        on_batch_saved=on_batch_saved,
                        token_path=token_path,
                        label=label,
                    )
                else:
                    if on_batch_saved:
                        on_batch_saved(str(batch))
                    if token_path is not None:
                        save_gomatrix_sync_token(token_path, str(batch))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[%s] Sync loop error: %r", label, exc, exc_info=True)
            if token_path is not None and is_invalid_sync_token_error(exc):
                reset_gomatrix_sync_token(client, token_path)
                await park_sync_token_without_timeline(
                    client,
                    token_path=token_path,
                    label=label,
                    timeout_ms=timeout_ms,
                )
            if on_sync_error:
                on_sync_error(exc)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_retry_seconds)


class MatrixMessageDispatcher:
    """Run heavy message handlers off the sync callback stack.

    Handlers for the **same** ``session_key`` (workspace session id) are
    serialised. Different keys may run concurrently so a long turn in
    workspace A does not block chat in workspace B after a switch.

    Sync generations tag handlers scheduled during one ``client.sync()`` so the
    durable next_batch advances only after that batch is applied (or freezes on
    busy-drop / handler failure so a restart can redeliver).
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._gen_seq = 0
        self._gen_tasks: dict[int, set[asyncio.Task[None]]] = {}
        self._gen_closed: set[int] = set()
        self._gen_failed: set[int] = set()
        self._gen_results: dict[int, tuple[str, bool]] = {}
        self._next_flush_gen = 1
        self._flush_frozen = False
        self._durable_lock = asyncio.Lock()
        self._persist_tasks: set[asyncio.Task[None]] = set()

    def begin_generation(self) -> int:
        """Open a sync generation; room callbacks should schedule under this gen."""
        self._gen_seq += 1
        gen = self._gen_seq
        self._gen_tasks[gen] = set()
        return gen

    def close_generation(self, gen: int) -> None:
        """Mark that no further handlers will be tagged with *gen*."""
        self._gen_closed.add(gen)

    def mark_generation_failed(self, gen: int) -> None:
        """Busy-drop or handler error: durable cursor must not advance past *gen*."""
        self._gen_failed.add(gen)

    def _track_task(self, task: asyncio.Task[None], *, gen: int | None) -> None:
        self._tasks.add(task)
        if gen is not None and gen in self._gen_tasks:
            self._gen_tasks[gen].add(task)

        def _on_done(t: asyncio.Task[None]) -> None:
            self._tasks.discard(t)
            if gen is not None:
                bucket = self._gen_tasks.get(gen)
                if bucket is not None:
                    bucket.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                if gen is not None:
                    self.mark_generation_failed(gen)
                logger.error("[%s] Background task failed", t.get_name(), exc_info=exc)

        task.add_done_callback(_on_done)

    def _lock_for(self, session_key: str) -> asyncio.Lock:
        key = session_key or ""
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def schedule(
        self,
        coro: Awaitable[Any],
        *,
        label: str = "matrix-message",
        on_busy: Callable[[], Awaitable[Any]] | None = None,
        busy_timeout: float = 120.0,
        session_key: str = "",
    ) -> None:
        """Run *coro* behind the per-``session_key`` dispatcher lock.

        If the lock is not acquired within ``busy_timeout`` seconds (a previous
        handler is stuck, e.g. a long turn or an approval wait), the coroutine
        is dropped and ``on_busy`` is invoked so the sender gets feedback
        instead of silent queueing (REMAINING_ISSUES #165).
        """
        lock = self._lock_for(session_key)
        gen = _current_sync_gen.get()

        async def _runner() -> None:
            try:
                await asyncio.wait_for(lock.acquire(), timeout=busy_timeout)
            except TimeoutError:
                logger.warning("[Matrix] Dispatcher busy > %.0fs; dropping %s", busy_timeout, label)
                if gen is not None:
                    self.mark_generation_failed(gen)
                if asyncio.iscoroutine(coro):
                    coro.close()
                if on_busy is not None:
                    try:
                        await on_busy()
                    except Exception:
                        logger.exception("[Matrix] on_busy callback failed for %s", label)
                return
            try:
                await coro
            finally:
                lock.release()

        task = asyncio.create_task(_runner(), name=label)
        self._track_task(task, gen=gen)

    def schedule_unlocked(
        self,
        coro: Awaitable[Any],
        *,
        label: str = "matrix-sideband",
    ) -> None:
        """Run *coro* without the session lock (e.g. ``/ws`` while a turn runs)."""
        gen = _current_sync_gen.get()

        async def _runner() -> None:
            await coro

        task = asyncio.create_task(_runner(), name=label)
        self._track_task(task, gen=gen)

    def schedule_token_persist(
        self,
        gen: int,
        batch: str,
        *,
        on_batch_saved: SyncTokenCallback | None,
        token_path: Path | None,
        label: str = "Matrix",
    ) -> None:
        """Persist *batch* only after generation *gen* handlers succeed (ordered)."""

        async def _persist() -> None:
            ok = await self.wait_generation_applied(gen)
            async with self._durable_lock:
                self._gen_results[gen] = (batch, ok)
                await self._flush_durable_tokens(
                    on_batch_saved=on_batch_saved,
                    token_path=token_path,
                    label=label,
                )

        task = asyncio.create_task(_persist(), name=f"matrix-token-persist-{gen}")
        self._persist_tasks.add(task)

        def _on_done(t: asyncio.Task[None]) -> None:
            self._persist_tasks.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                logger.error("[%s] Sync token persist failed for gen %s", label, gen, exc_info=exc)

        task.add_done_callback(_on_done)

    async def wait_generation_applied(self, gen: int) -> bool:
        """True when all handlers for *gen* finished without busy-drop / error."""
        while True:
            closed = gen in self._gen_closed
            pending = [t for t in self._gen_tasks.get(gen, ()) if not t.done()]
            if closed and not pending:
                failed = gen in self._gen_failed
                self._gen_tasks.pop(gen, None)
                self._gen_closed.discard(gen)
                self._gen_failed.discard(gen)
                return not failed
            if pending:
                await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            else:
                await asyncio.sleep(0)

    async def _flush_durable_tokens(
        self,
        *,
        on_batch_saved: SyncTokenCallback | None,
        token_path: Path | None,
        label: str,
    ) -> None:
        """Advance durable token in generation order; freeze after the first hole."""
        from src.matrix_client.sync_token import save_gomatrix_sync_token

        while not self._flush_frozen:
            result = self._gen_results.get(self._next_flush_gen)
            if result is None:
                return
            batch, ok = result
            del self._gen_results[self._next_flush_gen]
            gen = self._next_flush_gen
            self._next_flush_gen += 1
            if not ok:
                self._flush_frozen = True
                logger.warning(
                    "[%s] Not advancing durable sync token after incomplete apply "
                    "(gen=%s batch=%s); restart can redeliver",
                    label,
                    gen,
                    batch,
                )
                return
            if on_batch_saved:
                on_batch_saved(batch)
            if token_path is not None:
                save_gomatrix_sync_token(token_path, batch)

    async def drain(self, *, timeout: float = 60.0) -> None:
        """Wait for scheduled handlers to finish (e.g. before Matrix logout)."""
        pending = [t for t in list(self._tasks) if not t.done()]
        persist = [t for t in list(self._persist_tasks) if not t.done()]
        all_pending = pending + persist
        if not all_pending:
            return
        try:
            await asyncio.wait_for(asyncio.gather(*all_pending, return_exceptions=True), timeout=timeout)
        except TimeoutError:
            logger.warning(
                "[Matrix] %d message handler(s) still running after %.0fs shutdown wait",
                sum(1 for t in all_pending if not t.done()),
                timeout,
            )


async def park_sync_token_without_timeline(
    client: Any,
    *,
    token_path: Path | None = None,
    label: str = "Matrix",
    timeout_ms: int = 30_000,
) -> str | None:
    """Advance ``next_batch`` to the server tip without message callbacks registered.

    Used on cold start when no saved sync token exists. GoMatrix initial /sync used
    to dump full room history into timeline; even with an empty-timeline server,
    parking the token before registering callbacks is the safe client-side guard.
    """
    from src.matrix_client.sync_token import save_gomatrix_sync_token

    resp = await client.sync(timeout=timeout_ms, full_state=False)
    if isinstance(resp, Exception):
        logger.warning("[%s] Token park sync failed: %r", label, resp)
        return None
    batch = getattr(client, "next_batch", None)
    if batch and token_path is not None:
        save_gomatrix_sync_token(token_path, str(batch))
    if batch:
        logger.info("[%s] Parked sync token at %s (no timeline callbacks yet)", label, batch)
    return str(batch) if batch else None


async def bootstrap_matrix_catchup(
    client: Any,
    join_room: Callable[[str], Awaitable[None]],
    *,
    label: str = "Matrix",
    timeout_ms: int = 30_000,
) -> None:
    """One full incremental /sync at startup — deliver timeline, then join invites.

    Do NOT use a timeline.limit=0 filter here: matrix-nio may advance internal
    dedupe state without invoking message callbacks, which skips the first post-restart
    message even when next_batch is restored afterward.

    Callers must register room-message callbacks only after a sync token is present
    (loaded from disk or parked via ``park_sync_token_without_timeline``); otherwise
    an initial /sync can replay historic ``m.room.message`` events into the agent.
    """
    resp = await client.sync(timeout=timeout_ms, full_state=False)
    if isinstance(resp, Exception):
        logger.warning("[%s] Bootstrap sync failed: %r", label, resp)
        return
    pending = list(getattr(client, "invited_rooms", {}).keys())
    if pending:
        logger.info("[%s] Bootstrap pending invites: %d", label, len(pending))
    for room_id in pending:
        await join_room(room_id)


async def sync_for_pending_invites(
    client: Any,
    join_room: Callable[[str], Awaitable[None]],
    *,
    label: str = "Matrix",
) -> None:
    """Join pending invite rooms after a full catch-up sync (safe for restart)."""
    await bootstrap_matrix_catchup(client, join_room, label=label)
