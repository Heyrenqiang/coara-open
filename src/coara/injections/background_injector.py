"""Background-task completion notification injector"""

from __future__ import annotations

from src.core.message_tags import background_result

# completion reminder for read 查看。
_ERROR_MAX = 1000
_DESC_MAX = 120

# 上下文的内容走下方分级上限，持久化保留更多以便回取。
INJECT_FULL_MAX = 100_000

# - agent 类无更长日志可回取，适度放宽。
INJECT_FULL_MAX_BASH = 12_000
INJECT_FULL_MAX_AGENT = 30_000

_STATUS_ZH: dict[str, str] = {
    "running": "运行中",
    "completed": "成功",
    "failed": "失败",
    "killed": "已取消",
    "timed_out": "超时",
    "cancelled": "已取消",
}


def _status_label(status: str, *, has_error: bool) -> str:
    key = str(status or "").strip()
    if has_error and key not in _STATUS_ZH:
        return "失败"
    if has_error and key == "completed":
        # completed + has_error：任务跑完但以非 0 退出码结束，不是「成功」
        return "失败（退出码非 0）"
    if has_error and key == "":
        return "失败"
    return _STATUS_ZH.get(key, key or "未知")


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return "…" + text[-limit:] if limit > 0 else ""


def clip_result_for_persist(text: str, limit: int = INJECT_FULL_MAX) -> str:
    """持久化侧截断：头尾保留 + 已截断注记"""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    half = max(1, limit // 2)
    return text[:half] + f"\n…(内容过长已截断，共 {len(text)} 字符)\n" + text[-half:]


def build_background_completion_reminder(
    task_id: str,
    status: str,
    has_error: bool,
    description: str = "",
    *,
    exit_code: int | None = None,
    error: str | None = None,
    result: str = "",
    log_path: str = "",
    kind: str = "",
) -> str:
    """Minimal Chinese reminder for all background tasks (bash + agent). ``result`` 是任务结果"""
    label = _status_label(status, has_error=has_error)
    head = f"后台任务 {task_id} {label}"
    if exit_code is not None:
        head = f"{head} exit={exit_code}"

    lines = [head]
    desc = _clip(description or "", _DESC_MAX)
    if desc:
        lines.append(f"描述: {desc}")

    inject_max = INJECT_FULL_MAX_BASH if kind == "bash" else INJECT_FULL_MAX_AGENT
    err = _clip(error or "", _ERROR_MAX)
    raw_result = (result or "").strip()
    truncated = len(raw_result) > inject_max
    preview = clip_result_for_persist(result or "", inject_max)
    if err:
        lines.append(f"错误: {err}")
    if preview and preview != err:
        lines.append(f"输出: {preview}")
    # 仅在结果被截断时才附 log 路径（此时确需回取全文）；
    # 未截断时结果已全量在此，附路径只会诱导 LLM 多读一遍文件、白跑一轮。
    if log_path.strip() and truncated:
        from src.core.message_tags import llm_only

        lines.append(f"完整日志: {log_path.strip()}")
        # 行为指令仅给 LLM 看（用户不可见），避免显示层暴露内部约束。
        lines.append(llm_only("（输出已截断。非必要不准读此日志！）"))

    return "\n".join(lines)


def build_background_completion_message(reminder_text: str) -> str:
    """Wrap the reminder text in ``<后台结果>`` tags."""
    return background_result(reminder_text)
