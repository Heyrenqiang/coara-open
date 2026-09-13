"""CLI 活动树不得吞它端（web/matrix）工具事件。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from rich.console import Console

from src.cli.display_controller import CliDisplayController, _cli_shows_activity_event
from src.core.events import TraceEvent


def test_cli_shows_activity_event_filters_other_ends() -> None:
    """CLI 活动树只认本端；web / matrix / event / background 一律不可见。"""
    for src in ("web", "matrix", "event", "background"):
        assert (
            _cli_shows_activity_event(
                TraceEvent(
                    coara_id="x",
                    coara_name="c",
                    event_type="tool_complete",
                    message="",
                    payload={"source": src},
                )
            )
            is False
        ), src
    for src in ("cli", "cli-attached"):
        assert (
            _cli_shows_activity_event(
                TraceEvent(
                    coara_id="x",
                    coara_name="c",
                    event_type="tool_complete",
                    message="",
                    payload={"source": src},
                )
            )
            is True
        ), src
    assert (
        _cli_shows_activity_event(
            TraceEvent(coara_id="x", coara_name="c", event_type="tool_start", message="", payload={})
        )
        is False
    )


def test_activity_event_skips_web_and_matrix_tools_for_spinner() -> None:
    root = SimpleNamespace(
        foreground_coara=SimpleNamespace(
            session_id="s1",
            workspace_dir="/tmp/ws",
            _active_turn_source="",  # attach 常见：它端回合未镜像为活跃
        ),
        identity=SimpleNamespace(coara_id="root"),
    )
    spinner = MagicMock()
    registry = MagicMock()
    ctrl = CliDisplayController(
        root=root,
        console=Console(force_terminal=False),
        background_spinner=MagicMock(workspace_registry=registry),
        subagent_spinner=spinner,
    )
    ctrl.background_tool_history = None

    for src, tool_id in (("web", "tc-web"), ("matrix", "tc-phone")):
        ctrl._on_activity_event(
            TraceEvent(
                coara_id="root",
                coara_name="c",
                event_type="tool_complete",
                message="done",
                payload={
                    "source": src,
                    "tool_name": "shell",
                    "tool_call_id": tool_id,
                    "workspace_dir": "/tmp/ws",
                    "session_id": "s1",
                },
            )
        )
    spinner.handle_event.assert_not_called()

    ctrl._on_activity_event(
        TraceEvent(
            coara_id="root",
            coara_name="c",
            event_type="tool_complete",
            message="done",
            payload={
                "source": "cli-attached",
                "tool_name": "shell",
                "tool_call_id": "tc-cli",
                "workspace_dir": "/tmp/ws",
                "session_id": "s1",
            },
        )
    )
    spinner.handle_event.assert_called_once()
