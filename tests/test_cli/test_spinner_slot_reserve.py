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
        lambda self: SimpleNamespace(count=0, labels=lambda: ()),
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
        lambda self: SimpleNamespace(count=0, labels=lambda: ()),
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
    assert plain.rstrip().endswith("你：")


def test_activity_rows_stay_visible(monkeypatch) -> None:
    spinner = BackgroundSpinner()
    root = SimpleNamespace(
        has_active_turn=lambda: True,
        foreground_coara=SimpleNamespace(_active_turn_source="cli", _active_turn=None),
    )
    spinner.bind_root(root)
    spinner._cli_turn_display_active = True
    spinner._subagent_spinner = SimpleNamespace(
        get_status_rows=lambda: [("read a", "class:prompt.subagent"), ("edit b", "class:prompt.subagent")],
        has_active_blocks=lambda: True,
    )
    monkeypatch.setattr("src.cli.input_queue_display.pending_input_hint_lines", lambda _r: [])
    monkeypatch.setattr("src.cli.image_paste.is_image_loading", lambda: False)
    monkeypatch.setattr("prompt_toolkit.application.current.get_app_or_none", lambda: None)
    monkeypatch.setattr(
        BackgroundSpinner,
        "_background_tasks_snapshot",
        lambda self: SimpleNamespace(count=0, labels=lambda: ()),
    )
    monkeypatch.setattr(BackgroundSpinner, "_background_ws_segments", lambda self: ([], 0))

    plain = _plain(spinner())
    head = plain.split("input", 1)[0]
    assert "edit b" in head
    assert "read a" in head
    assert "2 " not in head
    assert plain.rstrip().endswith("你：")
    assert "input" in plain


def test_activity_rows_visible_while_streaming_pending(monkeypatch) -> None:
    """流式 pending 行在时，子智能体活动树仍要显示（不能被整段吞掉）。"""
    spinner = BackgroundSpinner()
    root = SimpleNamespace(
        has_active_turn=lambda: True,
        foreground_coara=SimpleNamespace(_active_turn_source="cli-attached", _active_turn=None),
    )
    spinner.bind_root(root)
    spinner._cli_turn_display_active = True
    spinner._streaming_block = SimpleNamespace(pending_line="半截正文")
    spinner._subagent_spinner = SimpleNamespace(
        get_status_rows=lambda: [("◌ coaras 编译", "class:prompt.subagent")],
        has_active_blocks=lambda: True,
    )
    monkeypatch.setattr("src.cli.input_queue_display.pending_input_hint_lines", lambda _r: [])
    monkeypatch.setattr("src.cli.image_paste.is_image_loading", lambda: False)
    monkeypatch.setattr("prompt_toolkit.application.current.get_app_or_none", lambda: None)
    monkeypatch.setattr(
        BackgroundSpinner,
        "_background_tasks_snapshot",
        lambda self: SimpleNamespace(count=0, labels=lambda: ()),
    )
    monkeypatch.setattr(BackgroundSpinner, "_background_ws_segments", lambda self: ([], 0))
    monkeypatch.setattr(
        BackgroundSpinner,
        "_should_show_streaming_pending",
        lambda self: True,
    )

    plain = _plain(spinner())
    head = plain.split("input", 1)[0]
    assert "半截正文" in head
    assert "coaras 编译" in head


def test_thinking_visible_while_streaming_pending_without_activity(monkeypatch) -> None:
    """流式 pending 占位且活动树尚空时，Thinking 仍要画——否则状态槽全空像卡住。"""
    spinner = BackgroundSpinner()
    root = SimpleNamespace(
        has_active_turn=lambda: True,
        foreground_coara=SimpleNamespace(_active_turn_source="cli-attached", _active_turn=None),
    )
    spinner.bind_root(root)
    spinner._cli_turn_display_active = True
    spinner._turn_started_at = 0.0
    spinner._streaming_block = SimpleNamespace(pending_line="半截正文")
    spinner._subagent_spinner = SimpleNamespace(
        get_status_rows=lambda: [],
        has_active_blocks=lambda: False,
    )
    monkeypatch.setattr("src.cli.input_queue_display.pending_input_hint_lines", lambda _r: [])
    monkeypatch.setattr("src.cli.image_paste.is_image_loading", lambda: False)
    monkeypatch.setattr("prompt_toolkit.application.current.get_app_or_none", lambda: None)
    monkeypatch.setattr(
        BackgroundSpinner,
        "_background_tasks_snapshot",
        lambda self: SimpleNamespace(count=0, labels=lambda: ()),
    )
    monkeypatch.setattr(BackgroundSpinner, "_background_ws_segments", lambda self: ([], 0))
    monkeypatch.setattr(
        BackgroundSpinner,
        "_should_show_streaming_pending",
        lambda self: True,
    )
    monkeypatch.setattr(BackgroundSpinner, "_get_frame", lambda self: "◉")
    monkeypatch.setattr(BackgroundSpinner, "_turn_elapsed_suffix", lambda self: "")

    plain = _plain(spinner())
    head = plain.split("input", 1)[0]
    assert "半截正文" in head
    assert "◉" in head


def test_activity_rows_visible_after_thinking_ends(monkeypatch) -> None:
    """主回合 Thinking 已关、前台异步子智能体仍在跑时，活动树继续显示。"""
    spinner = BackgroundSpinner()
    root = SimpleNamespace(
        has_active_turn=lambda: False,
        foreground_coara=SimpleNamespace(_active_turn_source="", _active_turn=None),
    )
    spinner.bind_root(root)
    spinner._cli_turn_display_active = False
    spinner._subagent_spinner = SimpleNamespace(
        get_status_rows=lambda: [("◌ aide 调研", "class:prompt.subagent")],
        has_active_blocks=lambda: True,
    )
    monkeypatch.setattr("src.cli.input_queue_display.pending_input_hint_lines", lambda _r: [])
    monkeypatch.setattr("src.cli.image_paste.is_image_loading", lambda: False)
    monkeypatch.setattr("prompt_toolkit.application.current.get_app_or_none", lambda: None)
    monkeypatch.setattr(
        BackgroundSpinner,
        "_background_tasks_snapshot",
        lambda self: SimpleNamespace(count=0, labels=lambda: ()),
    )
    monkeypatch.setattr(BackgroundSpinner, "_background_ws_segments", lambda self: ([], 0))

    plain = _plain(spinner())
    head = plain.split("input", 1)[0]
    assert "aide 调研" in head
    assert "Thinking" not in head
