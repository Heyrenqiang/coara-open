"""CLI 跨端元事件：llm_switched / session_started / session_auto_new 一律不上屏。

切模型与 chrome 刷新由 root_shim 处理；滚动区不打断。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from rich.console import Console

from src.cli.display_controller import CliDisplayController
from src.core.events import TraceEvent


def _make_ctrl():
    root = SimpleNamespace(
        foreground_coara=SimpleNamespace(session_id="s1", workspace_dir="/tmp/ws"),
        identity=SimpleNamespace(coara_id="root"),
    )
    ctrl = CliDisplayController(
        root=root,
        console=Console(force_terminal=False),
        background_spinner=MagicMock(),
        subagent_spinner=MagicMock(),
    )
    ctrl.background_tool_history = None
    return ctrl


def _event(event_type: str, **payload) -> TraceEvent:
    return TraceEvent(coara_id="c", coara_name="n", event_type=event_type, message="m", payload=payload)


def test_session_started_notice_silent_on_cli(monkeypatch) -> None:
    """/new 类提示 CLI 一律不上屏（含他端同空间）：与对话无关的元事件不打断滚动区。"""
    ctrl = _make_ctrl()
    writes: list[str] = []
    monkeypatch.setattr("src.cli.display_controller.CliScrollback.write", lambda text, **kw: writes.append(str(text)))

    ctrl._on_session_started_notice(
        _event("session_started", interrupt_source="web_new_command", workspace_dir="/tmp/ws")
    )
    ctrl._on_session_started_notice(_event("session_started", interrupt_source="new_command", workspace_dir="/tmp/ws"))
    ctrl._on_session_auto_new(_event("session_auto_new", idle_timeout_minutes=120, workspace_dir="/tmp/ws"))
    assert writes == []


def test_llm_switched_silent_on_cli(monkeypatch) -> None:
    """他端/本端/异空间切模型：CLI 滚动区一律不上屏（chrome 由 root_shim 刷新）。"""
    ctrl = _make_ctrl()
    writes: list[str] = []
    monkeypatch.setattr("src.cli.display_controller.CliScrollback.write", lambda text, **kw: writes.append(str(text)))

    ctrl._on_llm_switched_notice(
        _event(
            "llm_switched",
            provider="deepseek",
            model="r1",
            origin_source="matrix",
            workspace_dir="/tmp/ws",
        )
    )
    ctrl._on_llm_switched_notice(
        _event("llm_switched", provider="a", model="b", origin_source="web", workspace_dir="/tmp/ws")
    )
    ctrl._on_llm_switched_notice(
        _event("llm_switched", provider="a", model="b", origin_source="", workspace_dir="/tmp/ws")
    )
    ctrl._on_llm_switched_notice(
        _event("llm_switched", provider="a", model="b", origin_source="cli-attached", workspace_dir="/tmp/ws")
    )
    ctrl._on_llm_switched_notice(
        _event("llm_switched", provider="a", model="b", origin_source="web", workspace_dir="/tmp/other-ws")
    )
    assert writes == []
