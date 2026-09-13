"""Thinking 槽常驻占位：空闲空白一行，有 spinner 时不叠空行。"""

from __future__ import annotations

from types import SimpleNamespace

from src.cli.spinner import BackgroundSpinner


def _plain(fragments) -> str:
    return "".join(text for _style, text in fragments)


def test_idle_prompt_reserves_blank_thinking_slot(monkeypatch) -> None:
    spinner = BackgroundSpinner()
    root = SimpleNamespace(
        has_active_turn=lambda: False,
        foreground_coara=SimpleNamespace(_active_turn_source="", _active_turn=None),
        workspace_manager=None,
    )
    spinner.bind_root(root)
    monkeypatch.setattr("src.cli.input_queue_display.pending_input_hint_lines", lambda _r: [])
    monkeypatch.setattr("src.cli.image_paste.is_image_loading", lambda: False)
    monkeypatch.setattr(
        "prompt_toolkit.application.current.get_app_or_none",
        lambda: None,
    )
    monkeypatch.setattr(
        BackgroundSpinner,
        "_background_tasks_snapshot",
        lambda self: SimpleNamespace(idle_prompt_line=lambda: ""),
    )
    monkeypatch.setattr(BackgroundSpinner, "_background_ws_segments", lambda self: ([], 0))

    plain = _plain(spinner())
    assert plain.startswith("\n")
    assert not plain.startswith("\n\n")
    assert "input" in plain
    assert "\n╌" in plain


def test_active_thinking_does_not_stack_blank_slot(monkeypatch) -> None:
    spinner = BackgroundSpinner()
    root = SimpleNamespace(
        has_active_turn=lambda: True,
        foreground_coara=SimpleNamespace(_active_turn_source="cli", _active_turn=None),
    )
    spinner.bind_root(root)
    spinner._cli_turn_display_active = True
    spinner._ensure_turn_timer()

    monkeypatch.setattr("src.cli.input_queue_display.pending_input_hint_lines", lambda _r: [])
    monkeypatch.setattr("src.cli.image_paste.is_image_loading", lambda: False)
    monkeypatch.setattr("prompt_toolkit.application.current.get_app_or_none", lambda: None)
    monkeypatch.setattr(
        BackgroundSpinner,
        "_background_tasks_snapshot",
        lambda self: SimpleNamespace(idle_prompt_line=lambda: ""),
    )
    monkeypatch.setattr(BackgroundSpinner, "_background_ws_segments", lambda self: ([], 0))
    monkeypatch.setattr(
        "src.records.loading_phrases.current_phrase",
        lambda: "Thinking",
    )

    plain = _plain(spinner())
    assert "input" in plain
    assert not plain.startswith("\n")
    assert "Thinking" in plain
    assert "Thinking\n╌" in plain or "Thinking" in plain.split("input")[0]
    head = plain.split("input", 1)[0]
    assert not head.endswith("\n\n")
