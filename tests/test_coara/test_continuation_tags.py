"""Tests for continuation-input formatting (user vs system payloads)."""

from __future__ import annotations

from src.core.message_tags import (
    CONTINUATION_CLOSE,
    CONTINUATION_OPEN,
    continuation_input,
    continuation_subagent_display_text,
    continuation_user_display_text,
    format_continuation_for_history,
    is_preformatted_injection,
    subagent_message,
    subagent_scrollback_label,
    system_info,
)


def test_continuation_formatting() -> None:
    wrapped = continuation_input("补充一下")
    assert wrapped == f"{CONTINUATION_OPEN}\n补充一下\n{CONTINUATION_CLOSE}"
    assert "<系统提醒>" not in wrapped

    payload = system_info("[前台子智能体已完成] result")
    assert format_continuation_for_history(payload) == payload

    out = format_continuation_for_history("请改成用 pytest")
    assert CONTINUATION_OPEN in out and "请改成用 pytest" in out
    assert "<系统提醒>" not in out

    # 队列项恒为接续：默认 is_mid_turn=True；显式 False 才裸入（仅旁路）
    assert CONTINUATION_OPEN in format_continuation_for_history("同端跟话")
    assert format_continuation_for_history("新回合旁路", is_mid_turn=False) == "新回合旁路"

    # 接续跟话正文裸文本（来源标签已废弃），只套 <接续输入> 包裹
    remote_out = format_continuation_for_history("发我文档")
    assert CONTINUATION_OPEN in remote_out
    assert "发我文档" in remote_out
    assert is_preformatted_injection(remote_out)

    assert is_preformatted_injection(system_info("x"))
    assert is_preformatted_injection(continuation_input("y"))
    assert not is_preformatted_injection("plain user text")


def test_continuation_subagent_display_for_foreground_done() -> None:
    payload = system_info("[前台子智能体已完成] [sa-coaras-ab12]\n任务：摸底仓库\n结果：找到 3 个入口文件")
    assert continuation_user_display_text(payload) is None
    shown = continuation_subagent_display_text(payload)
    assert shown is not None
    assert shown.startswith("[sa-coaras-ab12] 摸底仓库")
    assert "找到 3 个入口文件" in shown


def test_continuation_subagent_display_for_report() -> None:
    payload = subagent_message("已完成第一步", task_id="sa-coaras-r1", description="长任务")
    assert continuation_user_display_text(payload) is None
    shown = continuation_subagent_display_text(payload)
    assert shown is not None
    assert "[sa-coaras-r1] 长任务" in shown
    assert "已完成第一步" in shown


def test_continuation_subagent_display_ignores_other_system() -> None:
    assert continuation_subagent_display_text(system_info("后台任务完成")) is None
    assert continuation_subagent_display_text("普通跟话") is None


def test_subagent_scrollback_label_from_task_id() -> None:
    assert subagent_scrollback_label("[sa-coaras-ab12] 摸底\n结果") == "coaras子智能体"
    assert subagent_scrollback_label("[sa-aide-ab12] 摸底\n结果") == "aide子智能体"
    assert subagent_scrollback_label("无 task id") == "子智能体"
