"""RootCoara foreground proxies: unbound raises; bound delegates to session."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.coara.root import RootCoara


def test_root_foreground_unbound_raises():
    root = RootCoara.__new__(RootCoara)
    root._foreground_session_id = None
    root._sessions = {}

    with pytest.raises(RuntimeError, match="No workspace view bound"):
        _ = root.foreground_coara


def test_root_get_status_delegates_to_foreground_session():
    root = RootCoara.__new__(RootCoara)
    fg = SimpleNamespace(
        get_status=lambda: {"workspace_dir": "/ws/pora", "status": "idle"},
    )
    root._foreground_session_id = "pora-id"
    root._sessions = {"pora-id": SimpleNamespace(coara=fg)}

    status = RootCoara.get_status.__get__(root, RootCoara)()
    assert status["workspace_dir"] == "/ws/pora"


def test_root_interrupt_unbound_raises():
    root = RootCoara.__new__(RootCoara)
    root._foreground_session_id = None
    root._sessions = {}

    with pytest.raises(RuntimeError, match="No workspace view bound"):
        RootCoara.interrupt_current_turn.__get__(root, RootCoara)("user_ctrl_c", interrupt_source="test")


def test_root_interrupt_delegates_to_foreground_session():
    root = RootCoara.__new__(RootCoara)
    called = {"n": 0}

    def _fg_interrupt(reason="user_interrupt", *, interrupt_source=None, cancel_delegates=True):
        called["n"] += 1
        assert reason == "user_stop"
        assert interrupt_source == "matrix_stop_command"
        return True

    fg = SimpleNamespace(interrupt_current_turn=_fg_interrupt)
    root._foreground_session_id = "pora-id"
    root._sessions = {"pora-id": SimpleNamespace(coara=fg)}

    ok = RootCoara.interrupt_current_turn.__get__(root, RootCoara)("user_stop", interrupt_source="matrix_stop_command")
    assert ok is True
    assert called["n"] == 1
