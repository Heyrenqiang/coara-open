"""Tests for CLI graceful-shutdown signal wiring (SIGTERM / console close)."""

from __future__ import annotations

import signal
import sys
from types import SimpleNamespace

import pytest

from src.cli.shutdown_signals import (
    install_sigterm_handler,
    install_windows_console_close_handler,
    make_minimal_sync_cleanup,
)


def test_install_sigterm_handler_routes_to_callback() -> None:
    calls: list[str] = []
    previous = signal.getsignal(signal.SIGTERM)
    with install_sigterm_handler(lambda: calls.append("exit")):
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)
        assert calls == ["exit"]
    assert signal.getsignal(signal.SIGTERM) == previous


def test_sigterm_handler_fires_only_once() -> None:
    calls: list[str] = []
    with install_sigterm_handler(lambda: calls.append("exit")):
        handler = signal.getsignal(signal.SIGTERM)
        handler(signal.SIGTERM, None)
        handler(signal.SIGTERM, None)
    assert calls == ["exit"]


def test_sigterm_handler_swallows_callback_errors() -> None:
    def _boom() -> None:
        raise RuntimeError("broken callback")

    with install_sigterm_handler(_boom):
        handler = signal.getsignal(signal.SIGTERM)
        handler(signal.SIGTERM, None)  # must not raise


def test_sigterm_handler_does_not_touch_sigint() -> None:
    previous_int = signal.getsignal(signal.SIGINT)
    with install_sigterm_handler(lambda: None):
        assert signal.getsignal(signal.SIGINT) == previous_int
    assert signal.getsignal(signal.SIGINT) == previous_int


def test_minimal_sync_cleanup_persists_sessions_flushes_usage_and_trace(monkeypatch) -> None:
    persisted: list[str] = []
    coara = SimpleNamespace(persist_session_to_disk=lambda: persisted.append("s1"))
    root = SimpleNamespace(_sessions={"w1": SimpleNamespace(coara=coara)})

    usage_calls: list[str] = []
    import src.runtime.usage_collector as usage_collector

    monkeypatch.setattr(usage_collector, "shutdown_usage_collector", lambda r: usage_calls.append("flush"))
    trace_closed: list[str] = []

    cleanup = make_minimal_sync_cleanup(root, close_trace=lambda: trace_closed.append("close"))
    cleanup()

    assert persisted == ["s1"]
    assert usage_calls == ["flush"]
    assert trace_closed == ["close"]


def test_minimal_sync_cleanup_swallows_step_errors(monkeypatch) -> None:
    def _boom() -> None:
        raise RuntimeError("persist failed")

    root = SimpleNamespace(_sessions={"w1": SimpleNamespace(coara=SimpleNamespace(persist_session_to_disk=_boom))})
    import src.runtime.usage_collector as usage_collector

    monkeypatch.setattr(
        usage_collector,
        "shutdown_usage_collector",
        lambda r: (_ for _ in ()).throw(RuntimeError("usage failed")),
    )
    trace_closed: list[str] = []
    cleanup = make_minimal_sync_cleanup(root, close_trace=lambda: trace_closed.append("close"))
    cleanup()  # must not raise despite both steps failing
    assert trace_closed == ["close"]


def test_minimal_sync_cleanup_without_trace_or_sessions() -> None:
    root = SimpleNamespace(_sessions={})
    cleanup = make_minimal_sync_cleanup(root)
    cleanup()  # no sessions, no trace target — still a safe no-op


@pytest.mark.skipif(sys.platform != "win32", reason="SetConsoleCtrlHandler is Windows-only")
def test_windows_console_close_handler_install_and_uninstall() -> None:
    calls: list[str] = []
    uninstall = install_windows_console_close_handler(lambda: calls.append("cleanup"))
    assert callable(uninstall)
    uninstall()
    # Idempotent uninstall must not raise.
    uninstall()
