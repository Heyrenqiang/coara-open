"""Tests for the /status turn-phase line helper (src/coara/commands/session.py)."""

from __future__ import annotations

from types import SimpleNamespace

from src.coara.commands.session import _turn_phase_line
from src.coara.turn_phase import TurnPhase


def _root_with_phase(*, active: bool, phase: TurnPhase) -> SimpleNamespace:
    fg = SimpleNamespace(
        has_active_turn=lambda: active,
        _turn_phase=phase,
    )
    return SimpleNamespace(foreground_coara=fg)


def test_phase_line_shown_when_turn_active() -> None:
    phase = TurnPhase()
    phase.set("awaiting_llm", "kimi/k3")
    root = _root_with_phase(active=True, phase=phase)
    line = _turn_phase_line(root)
    assert line.startswith("等待模型响应 kimi/k3（")


def test_phase_line_hidden_when_idle() -> None:
    phase = TurnPhase()
    phase.set("tool_running", "shell")
    root = _root_with_phase(active=False, phase=phase)
    assert _turn_phase_line(root) == ""


def test_phase_line_never_raises_on_odd_root() -> None:
    assert _turn_phase_line(SimpleNamespace()) == ""
    assert _turn_phase_line(SimpleNamespace(foreground_coara=None)) == ""
