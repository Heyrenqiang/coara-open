"""Graceful-shutdown signal wiring for the coara CLI process"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
from collections.abc import Callable
from typing import Any

from src.core.logger import logger


def make_sigterm_exit_request(
    root: Any,
    input_queue: Any,
    loop: Any,
) -> Callable[[], None]:
    """Build the SIGTERM callback: interrupt the active turn, then queue exit"""
    from src.cli.session import _PromptExit

    def _request() -> None:
        with contextlib.suppress(Exception):
            foreground = getattr(root, "foreground_coara", None)
            if foreground is not None and foreground.has_active_turn():
                result = foreground.interrupt_current_turn("sigterm", interrupt_source="cli_sigterm")
                # 未调度则 close 掉避免 RuntimeWarning。
                if asyncio.iscoroutine(result):
                    try:
                        asyncio.get_running_loop().create_task(result)
                    except RuntimeError:
                        result.close()
        with contextlib.suppress(Exception):
            loop.call_soon_threadsafe(input_queue.put_nowait, _PromptExit())

    return _request


@contextlib.contextmanager
def install_sigterm_handler(request_exit: Callable[[], None]):
    """Route SIGTERM onto the normal graceful-exit path (no async work inline)"""
    try:
        previous_handler = signal.getsignal(signal.SIGTERM)
        fired = False

        def _handle_sigterm(signum: int, frame: Any) -> None:
            nonlocal fired
            if fired:
                return
            fired = True
            with contextlib.suppress(Exception):
                request_exit()

        signal.signal(signal.SIGTERM, _handle_sigterm)
    except (ValueError, OSError, RuntimeError) as exc:
        logger.debug(f"SIGTERM handler not installed: {exc}")
        previous_handler = None
    try:
        yield
    finally:
        if previous_handler is not None:
            with contextlib.suppress(Exception):
                signal.signal(signal.SIGTERM, previous_handler)


def make_minimal_sync_cleanup(
    root: Any,
    *,
    close_trace: Callable[[], None] | None = None,
) -> Callable[[], None]:
    """Best-effort synchronous flush for the ~5s console-close window"""

    def _cleanup() -> None:
        sessions = getattr(root, "_sessions", None) or {}
        for session in list(sessions.values()):
            with contextlib.suppress(Exception):
                coara = getattr(session, "coara", None)
                if coara is not None:
                    coara.persist_session_to_disk()
        with contextlib.suppress(Exception):
            from src.runtime.usage_collector import shutdown_usage_collector

            shutdown_usage_collector(root)
        if close_trace is not None:
            with contextlib.suppress(Exception):
                close_trace()

    return _cleanup


# they do not target interactive CLI sessions.
_CONSOLE_CLOSE_EVENTS = frozenset({1, 2})
_CTRL_CLOSE_EVENT = 2

# Strong ref to the ctypes callback — the OS calls it from a foreign thread,
# and a GC'd callback would crash the process.
_console_ctrl_callback: Any = None


def install_windows_console_close_handler(cleanup: Callable[[], None]) -> Callable[[], None] | None:
    """Register a SetConsoleCtrlHandler for window close / Ctrl+Break"""
    global _console_ctrl_callback
    if sys.platform != "win32":
        return None
    import ctypes

    handler_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_ulong)

    def _console_ctrl_handler(event: int) -> bool:
        if event not in _CONSOLE_CLOSE_EVENTS:
            return False
        with contextlib.suppress(BaseException):
            cleanup()
        # CLOSE: handled (the OS still terminates the process afterwards).
        # BREAK: pass on so default termination runs after our flush.
        return event == _CTRL_CLOSE_EVENT

    callback = handler_type(_console_ctrl_handler)
    try:
        registered = ctypes.windll.kernel32.SetConsoleCtrlHandler(callback, 1)  # type: ignore[attr-defined]
    except Exception as exc:
        logger.debug(f"SetConsoleCtrlHandler not available: {exc}")
        return None
    if not registered:
        logger.debug("SetConsoleCtrlHandler registration failed")
        return None
    _console_ctrl_callback = callback

    def _uninstall() -> None:
        global _console_ctrl_callback
        with contextlib.suppress(Exception):
            ctypes.windll.kernel32.SetConsoleCtrlHandler(callback, 0)  # type: ignore[attr-defined]
        if _console_ctrl_callback is callback:
            _console_ctrl_callback = None

    return _uninstall
