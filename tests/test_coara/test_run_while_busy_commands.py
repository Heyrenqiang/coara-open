"""Mid-turn-safe slash command classification."""

from __future__ import annotations

from src.coara.commands.registry import RUN_WHILE_BUSY, is_run_while_busy_command


def test_run_while_busy_includes_ws_and_model() -> None:
    assert "ws" in RUN_WHILE_BUSY
    assert "report" in RUN_WHILE_BUSY
    assert "model" in RUN_WHILE_BUSY
    assert "qrcode" in RUN_WHILE_BUSY
    assert is_run_while_busy_command("/ws switch shop")
    assert is_run_while_busy_command("/report")
    assert is_run_while_busy_command("/report 卡住了")
    assert is_run_while_busy_command("/model")
    assert is_run_while_busy_command("/model 2")
    assert is_run_while_busy_command("/qrcode")


def test_run_while_busy_excludes_session_mutating_commands() -> None:
    assert "new" not in RUN_WHILE_BUSY
    assert "exit" not in RUN_WHILE_BUSY
    assert "quit" not in RUN_WHILE_BUSY
    assert "restart" not in RUN_WHILE_BUSY
    assert "stop" not in RUN_WHILE_BUSY
    assert "usage" not in RUN_WHILE_BUSY
    assert not is_run_while_busy_command("/new")
    assert not is_run_while_busy_command("/exit")
    assert not is_run_while_busy_command("/stop")


def test_run_while_busy_rejects_non_slash() -> None:
    assert not is_run_while_busy_command("hello")
    assert not is_run_while_busy_command("")
