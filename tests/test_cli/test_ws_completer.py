"""Tests for CLI slash ↑↓ pickers."""

from __future__ import annotations

from types import SimpleNamespace

from prompt_toolkit.document import Document

from src.cli.completers import SlashCommandCompleter
from src.cli.slash_pickers import (
    SlashPickerOption,
    WorkspaceSwitchCompleter,
    is_slash_picker_submit_line,
    is_ws_switch_submit_line,
    slash_picker_cancel_stem,
    workspace_options_from_root,
)


def _doc(text: str) -> Document:
    return Document(text, cursor_position=len(text))


def test_is_ws_switch_submit_line() -> None:
    assert is_ws_switch_submit_line("/ws switch shop")
    assert not is_ws_switch_submit_line("/ws")
    assert not is_ws_switch_submit_line("/ws switch shop --default")


def test_is_slash_picker_submit_line_covers_toggles_and_lists() -> None:
    assert is_slash_picker_submit_line("/thinking high")
    assert is_slash_picker_submit_line("/sandbox")
    assert is_slash_picker_submit_line("/events reload")
    assert is_slash_picker_submit_line("/events")
    assert is_slash_picker_submit_line("/vault lock")
    assert not is_slash_picker_submit_line("/help")
    assert not is_slash_picker_submit_line("/workflow")
    assert not is_slash_picker_submit_line("/usage 7")


def test_slash_picker_cancel_stem() -> None:
    assert slash_picker_cancel_stem("/ws switch alpha") == "/ws"
    assert slash_picker_cancel_stem("/model deepseek/x") == "/model"
    assert slash_picker_cancel_stem("/ws") is None


def test_workspace_switch_completer_lists_on_bare_ws() -> None:
    opts = [
        SlashPickerOption("/ws switch alpha", "alpha", "#1"),
        SlashPickerOption("/ws switch beta", "beta", "#2"),
    ]
    c = WorkspaceSwitchCompleter(lambda: opts)
    texts = [comp.text for comp in c.get_completions(_doc("/ws"), None)]
    assert texts == ["/ws switch alpha", "/ws switch beta"]


def test_workspace_switch_completer_filters_and_skips_owned() -> None:
    opts = [
        SlashPickerOption("/ws switch alpha", "alpha", ""),
        SlashPickerOption("/ws switch beta", "beta", ""),
    ]
    c = WorkspaceSwitchCompleter(lambda: opts)
    assert [x.text for x in c.get_completions(_doc("/ws be"), None)] == ["/ws switch beta"]
    assert list(c.get_completions(_doc("/ws updates"), None)) == []


def test_slash_completer_defers_picker_commands() -> None:
    slash = SlashCommandCompleter()
    assert list(slash.get_completions(_doc("/ws"), None)) == []
    assert list(slash.get_completions(_doc("/model"), None)) == []
    assert "/ws" in [c.text for c in slash.get_completions(_doc("/w"), None)]
    assert "/qrcode" in [c.text for c in slash.get_completions(_doc("/q"), None)]
    assert "/usage" not in [c.text for c in slash.get_completions(_doc("/"), None)]


def test_workspace_options_from_root() -> None:
    e1 = SimpleNamespace(name="alpha", id="a", path=r"D:\a")
    e2 = SimpleNamespace(name="beta", id="b", path=r"D:\b")
    root = SimpleNamespace(
        sync_workspace_manager_to_foreground=lambda: None,
        workspace_manager=SimpleNamespace(
            list_workspaces=lambda: [e1, e2],
        ),
    )
    from unittest.mock import patch

    with (
        patch("src.workspace.catalog.resolve_foreground_active_id", return_value="a"),
        patch("src.workspace.catalog.resolve_workspace_summary", return_value="sum"),
    ):
        rows = workspace_options_from_root(root)
    assert [r.insert for r in rows] == ["/ws switch alpha", "/ws switch beta"]
    assert "当前" in rows[0].meta
