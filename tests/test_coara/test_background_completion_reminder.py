"""Background completion reminder + bash terminal semantics."""

from __future__ import annotations

from src.background.bash_runner import BashBackgroundRunner
from src.background.task_store import TaskRecord, TaskStatus
from src.coara.injections.background_injector import (
    build_background_completion_message,
    build_background_completion_reminder,
)
from src.coara.matrix_notify import MatrixNotificationBridge


def test_failed_exit_code_is_not_completed_reason() -> None:
    rec = TaskRecord(
        task_id="bash-x",
        kind="bash",
        description="poll",
        status=TaskStatus.FAILED.value,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:01",
        exit_code=1,
        error=None,
    )
    assert BashBackgroundRunner._terminal_reason(rec) == "failed"
    assert BashBackgroundRunner._has_error(rec) is True


def test_completed_exit_zero_is_success() -> None:
    rec = TaskRecord(
        task_id="bash-ok",
        kind="bash",
        description="ok",
        status=TaskStatus.COMPLETED.value,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:01",
        exit_code=0,
    )
    assert BashBackgroundRunner._terminal_reason(rec) == "completed"
    assert BashBackgroundRunner._has_error(rec) is False


def test_reminder_failed_is_compact() -> None:
    text = build_background_completion_reminder(
        task_id="bash-1",
        status="failed",
        has_error=True,
        description="agnes video",
        exit_code=1,
        error="Connection reset",
        result="查询失败: connection reset by peer",
    )
    assert text.startswith("后台任务 bash-1 失败 exit=1")
    assert "❌" not in text
    assert "✅" not in text
    assert "类型:" not in text
    assert "原因:" not in text
    assert "错误: Connection reset" in text
    assert "输出: 查询失败" in text
    assert "task(output)" not in text

    wrapped = build_background_completion_message(text)
    assert wrapped.startswith("<后台结果>")
    assert wrapped.endswith("</后台结果>")


def test_reminder_success_is_compact() -> None:
    text = build_background_completion_reminder(
        task_id="bash-2",
        status="completed",
        has_error=False,
        result="video_url=https://example.com/a.mp4",
    )
    assert text.startswith("后台任务 bash-2 成功")
    assert "video_url=" in text
    assert "❌" not in text


def test_long_output_injected_in_full() -> None:
    preview = "x" * 9000
    text = build_background_completion_reminder(
        task_id="bash-3",
        status="completed",
        has_error=False,
        result=preview,
    )
    # 正常大小结果完整注入，无省略标记
    assert "…" not in text
    assert preview in text
    assert len(text) > 9000


def test_huge_output_protected_without_retrieval_promise() -> None:
    from src.coara.injections.background_injector import INJECT_FULL_MAX

    big = "y" * (INJECT_FULL_MAX + 1000)
    text = build_background_completion_reminder(
        task_id="bash-4",
        status="completed",
        has_error=False,
        result=big,
    )
    # 物理保护：超限保留头尾 + 截断注记；完整日志经提醒附带的 output.log 路径 read
    assert "已截断" in text
    assert "task(action=output)" not in text
    assert len(text) < INJECT_FULL_MAX + 200


def test_bash_reminder_omits_log_path_when_not_truncated() -> None:
    # 结果未截断：全量已在注入里，不附 log 路径（避免诱导 LLM 多读文件、白跑一轮）。
    text = build_background_completion_reminder(
        task_id="bash-5",
        status="completed",
        has_error=False,
        result="done",
        log_path="D:\\coara\\tasks\\bash-5\\output.log",
    )
    assert "完整日志" not in text
    assert "done" in text


def test_bash_reminder_carries_log_path_when_truncated() -> None:
    # 结果被截断：附 log 路径供回取全文；行为指令以「仅模型可见」包裹（用户不可见）。
    from src.coara.injections.background_injector import INJECT_FULL_MAX_BASH

    big = "y" * (INJECT_FULL_MAX_BASH + 1000)
    text = build_background_completion_reminder(
        task_id="bash-6",
        status="completed",
        has_error=False,
        result=big,
        log_path="D:\\coara\\tasks\\bash-6\\output.log",
        kind="bash",
    )
    assert "已截断" in text
    assert "完整日志: D:\\coara\\tasks\\bash-6\\output.log" in text
    assert "<仅模型可见>" in text
    assert "非必要不准读此日志" in text


def test_clip_result_for_persist_marks_truncation_without_task_output_promise() -> None:
    from src.coara.injections.background_injector import INJECT_FULL_MAX, clip_result_for_persist

    big = "z" * (INJECT_FULL_MAX + 500)
    clipped = clip_result_for_persist(big)
    assert "已截断" in clipped
    assert "task(action=output)" not in clipped
    assert len(clipped) < INJECT_FULL_MAX + 100


def test_llm_only_instruction_reaches_model_but_never_display() -> None:
    """红线：行为指令进 history 供模型读；显示侧（web 用户行 / CLI 行 / 动态卡片）剥净。

    显示面各自的收敛点：
    - web  ``web_views.user_frame_display_text``（``WebServer._stream_chat_turn`` 的
      user_message 帧正文 = 上屏 + 落视图带）
    - CLI ``display_controller._extract_system_body``（系统注入行正文）
    - 收件箱/动态 ``updates.display.format_update_display_text|_title``（web 消息页、
      手机端卡片、``[COARA_UPDATES]`` 推送）
    """
    from src.cli.display_controller import _extract_system_body
    from src.coara.injections.background_injector import INJECT_FULL_MAX_BASH
    from src.ui.web_views import user_frame_display_text
    from src.workspace.updates.display import (
        format_update_display_text,
        format_update_display_title,
    )

    big = "y" * (INJECT_FULL_MAX_BASH + 1000)
    reminder = build_background_completion_reminder(
        task_id="bash-7",
        status="completed",
        has_error=False,
        result=big,
        log_path="D:\\coara\\tasks\\bash-7\\output.log",
        kind="bash",
    )
    history_text = build_background_completion_message(reminder)

    # 模型侧（message_history）：标签与指令原样保留
    assert "<仅模型可见>" in history_text
    assert "非必要不准读此日志" in history_text

    # 显示侧四处：标签与指令一律不见，其余正文（任务头 / 截断注记 / 日志路径）仍在
    displays = {
        "web 用户行": user_frame_display_text(history_text),
        "CLI 系统行": _extract_system_body(history_text) or "",
        "动态正文": format_update_display_text(payload={"message": history_text}),
        "动态标题": format_update_display_title(payload={"message": history_text}),
    }
    for where, shown in displays.items():
        assert "<仅模型可见>" not in shown, where
        assert "非必要不准读此日志" not in shown, where
        assert "后台任务 bash-7 成功" in shown, where
    assert "完整日志: D:\\coara\\tasks\\bash-7\\output.log" in displays["CLI 系统行"]
    assert "已截断" in displays["web 用户行"]
    assert "已截断" in displays["动态正文"]
    # 动态标题按 120 字截断（"已截断"注记在正文中段，标题里不必出现）
    assert len(displays["动态标题"]) <= 121


def test_llm_only_only_text_falls_back_without_leaking_tag() -> None:
    """整条动态只有一句仅模型可见指令时，显示回退也不能漏出标签原文。"""
    from src.core.message_tags import llm_only
    from src.workspace.updates.display import (
        format_update_display_text,
        format_update_display_title,
    )

    payload = {"message": llm_only("（内部约束）")}
    assert format_update_display_text(payload=payload) == "(无摘要)"
    assert format_update_display_title(payload=payload) == "工作空间动态"
    assert format_update_display_title(payload=payload, fallback=llm_only("（内部约束）")) == "工作空间动态"


def test_matrix_event_response_is_plain_text() -> None:
    assert MatrixNotificationBridge.format_event_response("background", "你好") == "你好"
    assert MatrixNotificationBridge.format_event_response("inbox-watch", "你好") == "你好"


def test_status_label_completed_with_error_mentions_exit_code() -> None:
    """completed + has_error：语义修正为「失败（退出码非 0）」，不再报「成功」或裸「失败」。"""
    from src.coara.injections.background_injector import _status_label

    assert _status_label("completed", has_error=True) == "失败（退出码非 0）"
    # 正常完成仍是成功
    assert _status_label("completed", has_error=False) == "成功"
    # 已知失败状态不受影响
    assert _status_label("failed", has_error=True) == "失败"
    # 超时保留超时字样
    assert _status_label("timed_out", has_error=True) == "超时"
    assert _status_label("timed_out", has_error=False) == "超时"
    # 未知状态保留原词
    assert _status_label("weird_state", has_error=False) == "weird_state"
    assert _status_label("weird_state", has_error=True) == "失败"
    assert _status_label("", has_error=True) == "失败"
