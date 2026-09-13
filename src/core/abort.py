"""AbortSignal / AbortController — cancellation architecture for Coara.

Inspired by the Web API AbortController:
- Multi-source aggregation (user interrupt, timeout, internal error)
- Listener-based propagation (child agents can register their own listeners)
- Graceful exit support (signal is informative; receivers decide how to stop)
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TypeVar

T = TypeVar("T")


class OperationAborted(Exception):  # noqa: N818
    """Raised when an awaitable is cancelled because an :class:`AbortSignal` fired."""

    def __init__(self, reason: str = "interrupted") -> None:
        self.reason = reason
        super().__init__(reason)


class AbortSignal:
    """A signal that can be watched for cancellation requests.

    Unlike ``asyncio.Event`` (binary flag), AbortSignal supports:
    - Multiple listeners (child agents, tools, HTTP requests)
    - Reason tracking (why was it cancelled?)
    - Both ``await`` and callback-based consumption
    """

    def __init__(self) -> None:
        self._aborted = False
        self._reason: str | None = None
        self._listeners: list[Callable[[], None]] = []
        self._event = asyncio.Event()

    @property
    def aborted(self) -> bool:
        return self._aborted

    @property
    def reason(self) -> str | None:
        return self._reason

    def add_listener(self, callback: Callable[[], None]) -> None:
        """Register a callback invoked when the signal is aborted.

        If already aborted, the callback is invoked immediately.
        """
        if self._aborted:
            with contextlib.suppress(Exception):
                callback()
            return
        self._listeners.append(callback)
        if self._aborted and callback in self._listeners:
            # append 与 _aborted 检查之间 abort() 可能已触发：此时监听器
            # 已错过 abort() 的广播，补一次调用；回调内再 abort()（重入）会
            # 命中 abort() 开头的幂等守卫，不会二次广播
            with contextlib.suppress(Exception):
                callback()

    def remove_listener(self, callback: Callable[[], None]) -> None:
        """Unregister a previously added callback."""
        if callback in self._listeners:
            self._listeners.remove(callback)

    def abort(self, reason: str = "cancelled") -> None:
        """Mark the signal as aborted and notify all listeners."""
        if self._aborted:
            return
        self._aborted = True
        self._reason = reason
        self._event.set()
        for cb in list(self._listeners):
            with contextlib.suppress(Exception):
                cb()

    async def wait(self) -> None:
        """Await until the signal is aborted."""
        await self._event.wait()


class AbortController:
    """Controller that owns and can trigger an AbortSignal."""

    def __init__(self) -> None:
        self.signal = AbortSignal()

    def abort(self, reason: str = "cancelled") -> None:
        self.signal.abort(reason)


async def wait_for_abortable(
    awaitable: Awaitable[T],
    signal: AbortSignal | None = None,
    *,
    timeout: float | None = None,
) -> T:
    """Await *awaitable*, aborting promptly when *signal* fires.

    Raises:
        OperationAborted: *signal* was already aborted or fires while waiting
        TimeoutError: *timeout* elapsed (same as ``asyncio.wait_for``)
    """
    if signal is not None and signal.aborted:
        raise OperationAborted(signal.reason or "interrupted")

    # Build the operation task. When *timeout* is set, wrap with wait_for so
    # TimeoutError surfaces from the operation task (not the outer wait).
    inner = asyncio.ensure_future(awaitable)
    operation: asyncio.Future[T] = (
        inner if timeout is None else asyncio.ensure_future(asyncio.wait_for(inner, timeout=timeout))
    )

    if signal is None:
        return await operation

    signal_task = asyncio.create_task(signal.wait())
    try:
        done, _pending = await asyncio.wait(
            {operation, signal_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if signal_task in done or signal.aborted:
            operation.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError, Exception):
                await operation
            raise OperationAborted(signal.reason or "interrupted")
        return operation.result()
    except asyncio.CancelledError:
        if signal.aborted:
            raise OperationAborted(signal.reason or "interrupted") from None
        raise
    finally:
        signal_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await signal_task
        if not operation.done():
            operation.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError, Exception):
                await operation


@asynccontextmanager
async def abortable_lock(
    lock: asyncio.Lock,
    signal: AbortSignal | None = None,
) -> AsyncIterator[None]:
    """Acquire *lock*, aborting while waiting if *signal* fires.

    If the lock was acquired and then the wait is aborted (signal preferred in
    a race), the lock is released before re-raising. Callers that successfully
    enter the ``async with`` body always release in ``finally``.
    """
    if signal is not None and signal.aborted:
        raise OperationAborted(signal.reason or "interrupted")

    if signal is None:
        await lock.acquire()
        try:
            yield
        finally:
            lock.release()
        return

    acquire_task = asyncio.ensure_future(lock.acquire())
    acquired = False
    try:
        await wait_for_abortable(acquire_task, signal)
        acquired = True
        if signal.aborted:
            raise OperationAborted(signal.reason or "interrupted")
        yield
    except OperationAborted:
        if not acquired and acquire_task.done() and not acquire_task.cancelled() and acquire_task.exception() is None:
            # Acquired in the same tick the signal won — do not leak the lock.
            lock.release()
        raise
    finally:
        if acquired:
            lock.release()
