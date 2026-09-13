"""CLI 跨端对称提示：他端（web/手机）同空间 /new、切模型在 CLI scrollback 打一行。

本端自己敲的命令由命令回执打印，事件里 origin 标识跳过防双显。
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


def test_session_started_other_end_same_workspace_writes_notice(monkeypatch) -> None:
    """web 端同空间 /new：CLI 打「[web 端] 已开始新会话」。"""
    ctrl = _make_ctrl()
    writes: list[str] = []
    monkeypatch.setattr("src.cli.display_controller.CliScrollback.write", lambda text, **kw: writes.append(str(text)))

    ctrl._on_session_started_notice(
        _event("session_started", interrupt_source="web_new_command", workspace_dir="/tmp/ws")
    )
    assert writes == ["[web 端] 已开始新会话"]


def test_session_started_own_cli_new_skipped(monkeypatch) -> None:
    """本端 CLI 自己 /new：命令回执已打印，事件不再重复。"""
    ctrl = _make_ctrl()
    writes: list[str] = []
    monkeypatch.setattr("src.cli.display_controller.CliScrollback.write", lambda text, **kw: writes.append(str(text)))

    ctrl._on_session_started_notice(_event("session_started", interrupt_source="new_command", workspace_dir="/tmp/ws"))
    ctrl._on_session_started_notice(
        _event("session_started", interrupt_source="cli_attached_new_command", workspace_dir="/tmp/ws")
    )
    assert writes == []


def test_session_started_other_workspace_silent(monkeypatch) -> None:
    """异空间 /new：不打扰本端 CLI。"""
    ctrl = _make_ctrl()
    writes: list[str] = []
    monkeypatch.setattr("src.cli.display_controller.CliScrollback.write", lambda text, **kw: writes.append(str(text)))

    ctrl._on_session_started_notice(
        _event("session_started", interrupt_source="web_new_command", workspace_dir="/tmp/other-ws")
    )
    assert writes == []


def test_llm_switched_other_end_writes_notice(monkeypatch) -> None:
    """手机端同空间切模型：CLI 打「[手机端] 已切换模型 → …」。"""
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
    assert writes == ["[手机端] 已切换模型 → deepseek·r1"]


def test_llm_switched_own_cli_skipped(monkeypatch) -> None:
    """本端/外挂 CLI 自己 /model：回执已打印，事件跳过；无 origin 的旧事件同样跳过。"""
    ctrl = _make_ctrl()
    writes: list[str] = []
    monkeypatch.setattr("src.cli.display_controller.CliScrollback.write", lambda text, **kw: writes.append(str(text)))

    ctrl._on_llm_switched_notice(
        _event("llm_switched", provider="a", model="b", origin_source="", workspace_dir="/tmp/ws")
    )
    ctrl._on_llm_switched_notice(
        _event("llm_switched", provider="a", model="b", origin_source="cli-attached", workspace_dir="/tmp/ws")
    )
    assert writes == []


def test_llm_switched_other_workspace_silent(monkeypatch) -> None:
    """异空间切模型：不打扰。"""
    ctrl = _make_ctrl()
    writes: list[str] = []
    monkeypatch.setattr("src.cli.display_controller.CliScrollback.write", lambda text, **kw: writes.append(str(text)))

    ctrl._on_llm_switched_notice(
        _event("llm_switched", provider="a", model="b", origin_source="web", workspace_dir="/tmp/other-ws")
    )
    assert writes == []
