"""子智能体失败分类：瞬时（transient）vs 确定（permanent）。

瞬时失败（provider 抖动、空响应、超时/预算耗尽、可重试的 HTTP/网络错误）
值得从 checkpoint 自动续跑一次；确定性失败（参数校验、权限拒绝、配额耗尽、
未分类）续跑无意义，直接判死。

判定一律优先复用 src.llm.retry 的结构化分类（_is_retryable_exception），
isinstance 链次之，字符串匹配只作兜底——不另造一套关键词逻辑。
"""

from __future__ import annotations

from src.core.errors import EmptyResponseError, LLMError
from src.llm.retry import _is_retryable_exception

# 确定性失败的类型名/文案特征（兜底，仅当结构化判定未命中时使用）
_PERMANENT_TYPE_NAMES = {
    "ValueError",  # 参数校验错
    "PermissionError",
    "APIKeyError",
    "ProviderContentPolicyError",
    "ContextWindowExceededError",
}


def classify_subagent_failure(
    exc: BaseException | None,
    error_text: str | None = None,
) -> str:
    """判定子智能体失败性质，返回 "transient" 或 "permanent"。

    Args:
        exc: 原始异常（FAILED 分支由 turn_orchestrator 记录在
            ``subagent._turn_failure``；Exception 分支直接传入）
        error_text: 无异常对象时的错误文案（兜底匹配）
    """
    # 1) 结构化/类型链判定（优先，复用 retry.py）
    if exc is not None:
        # EmptyResponseError 与预算耗尽单独成类/文案，先按 transient 兜住，
        # 再走 retry.py 的通用判定（网络/HTTP 错误靠它）
        if isinstance(exc, EmptyResponseError):
            return "transient"
        if _is_budget_error(exc):
            return "transient"
        retryable, _reason = _is_retryable_exception(exc) if isinstance(exc, Exception) else (False, "")
        if retryable:
            return "transient"
        # 确定性类型
        if type(exc).__name__ in _PERMANENT_TYPE_NAMES or isinstance(exc, LLMError):
            return "permanent"
        # 其余异常未分类 → permanent（不拿用户任务冒险重试）
        return "permanent"

    # 2) 无异常对象时的文案兜底
    if error_text:
        return _classify_by_text(error_text)
    return "permanent"


def _is_budget_error(exc: BaseException) -> bool:
    """with_retry 预算耗尽抛的 LLMError（文案含「等待模型完整响应超过 N 秒」）。"""
    if not isinstance(exc, LLMError):
        return False
    text = str(exc)
    return "等待模型完整响应" in text and "已中止" in text


def _classify_by_text(text: str) -> str:
    """字符串兜底：仅当没有异常对象时用（如 FAILED 分支错误文本）。

    关键词来自 retry.py 的同源判定，保持一致。
    """
    lowered = text.lower()
    # 瞬时：空响应 / 超时 / 预算 / 可重试 HTTP
    transient_markers = (
        "emptyresponse",
        "empty response",
        "空响应",
        "timeout",
        "timed out",
        "超时",
        "等待模型完整响应",
        "rate limit",
        "too many requests",
        "throttled",
        "overloaded",
        "temporarily unavailable",
        "connection",
        "429",
        "500",
        "502",
        "503",
        "504",
        "529",
    )
    for marker in transient_markers:
        if marker in lowered:
            return "transient"
    # 其余（含 401/403/404/400/quota/权限/参数）→ permanent
    return "permanent"
