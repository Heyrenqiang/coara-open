"""Tests for CLI continuation-queue helpers."""

from __future__ import annotations

from types import SimpleNamespace

from src.cli.input_queue_display import (
    has_popable_queued_followup,
    is_queueable_user_text,
    pending_input_hint_lines,
    pop_latest_queued_followup,
)


def test_is_queueable_user_text() -> None:
    assert is_queueable_user_text("补充一下") is True
    assert is_queueable_user_text("  ") is False
    assert is_queueable_user_text("") is False
    assert is_queueable_user_text("/new") is False
    assert is_queueable_user_text(None) is False
    assert is_queueable_user_text(123) is False


def _root_with_queue(queue: list[str], *, active_turn: bool = True):
    fg = SimpleNamespace(
        _continuation_inputs=list(queue),
        has_active_turn=lambda: active_turn,
    )
    return SimpleNamespace(foreground_coara=fg)


def test_pending_lines_empty_without_queue() -> None:
    root = _root_with_queue([])
    assert pending_input_hint_lines(root) == []


def test_pending_lines_empty_when_idle() -> None:
    """turn 未运行时排队不显示（接续只在回合中排队）。"""
    root = _root_with_queue(["补充一句"], active_turn=False)
    assert pending_input_hint_lines(root) == []


def test_pending_lines_shows_one_line_per_message() -> None:
    """多条排队跟话逐条显示，一条一行，带 → 前缀。"""
    root = _root_with_queue(["第一条", "第二条", "第三条"])
    lines = pending_input_hint_lines(root)
    assert lines == ["→ 第一条", "→ 第二条", "→ 第三条"]


def test_pending_lines_skips_system_injections() -> None:
    """系统注入（后台完成等）不是用户跟话，不进排队提示。"""
    from src.core.message_tags import system_info

    root = _root_with_queue(["用户跟话", system_info("后台任务 bash-x 完成")])
    lines = pending_input_hint_lines(root)
    assert lines == ["→ 用户跟话"]


def test_pending_lines_flattens_newlines_and_caps_length() -> None:
    root = _root_with_queue(["第一行\n第二行", "x" * 5000])
    lines = pending_input_hint_lines(root)
    assert lines[0] == "→ 第一行 第二行"
    assert len(lines[1]) == 2002  # → 前缀 2 + 2000 字符软上限


def test_pending_lines_shows_image_only_followup() -> None:
    from src.core.types import ContinuationInput

    fg = SimpleNamespace(
        _continuation_inputs=[ContinuationInput(text="", image_blocks=[{"type": "image"}])],
        has_active_turn=lambda: True,
    )
    root = SimpleNamespace(foreground_coara=fg)
    assert pending_input_hint_lines(root) == ["→ [图片]"]


def test_pop_latest_queued_followup_removes_newest_user_text() -> None:
    """Esc 语义：按一次移除队尾最新一条用户跟话，系统注入不动。"""
    root = _root_with_queue(["第一条", "<系统消息>\n后台完成\n</系统消息>", "第二条"])
    fg = root.foreground_coara
    assert has_popable_queued_followup(root) is True
    assert pop_latest_queued_followup(root) == "第二条"
    assert fg._continuation_inputs == ["第一条", "<系统消息>\n后台完成\n</系统消息>"]
    assert pop_latest_queued_followup(root) == "第一条"
    assert fg._continuation_inputs == ["<系统消息>\n后台完成\n</系统消息>"]
    # 只剩系统注入：不可再弹
    assert has_popable_queued_followup(root) is False
    assert pop_latest_queued_followup(root) is None


def test_pop_latest_queued_followup_idle_turn() -> None:
    """回合未运行时不可弹（与显示口径一致），队列原样保留。"""
    root = _root_with_queue(["排队中"], active_turn=False)
    assert has_popable_queued_followup(root) is False
    assert root.foreground_coara._continuation_inputs == ["排队中"]
