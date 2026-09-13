"""Connection recovery and fine-grained retry logic for LLM API calls.

Tenacity-based retry:
- Exponential backoff with jitter
- Distinguish retryable vs non-retryable errors
- Intermediate retries log at INFO (file; quiet console); exhaustion at ERROR
- Support for connection errors, timeouts, 429, and 5xx
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

from src.core.errors import LLMError
from src.core.logger import logger

T = TypeVar("T")

# Retryable HTTP status codes
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504, 529}

# 配额耗尽（欠费/额度用尽/余额不足）：非瞬时，重试无意义，与 RATE_LIMIT 区分。
# DeepSeek 官方 402 = 余额不足（Insufficient Balance）；OpenAI 兼容端点也可能在
# 400/429 的错误体里带 insufficient_quota / exceeded quota 等标记。
_QUOTA_EXHAUSTED_MARKERS = (
    "insufficient_quota",
    "insufficient_balance",
    "quota exhausted",
    "exceeded your current quota",
    "balance insufficient",
    "payment required",
    "余额不足",
    "欠费",
)

# Base delay in seconds; each attempt multiplies by 2 and adds jitter
_BASE_DELAY = 2.0
_MAX_DELAY = 30.0
# 服务端 Retry-After 的封顶值：超过此值按此值等待（防御异常大的 Retry-After）
_RETRY_AFTER_MAX_SECONDS = 60.0


def _exception_chain(exc: BaseException) -> list[BaseException]:
    """``exc`` then ``__cause__`` / ``__context__`` (deduped)."""
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _is_retryable_exception(exc: Exception) -> tuple[bool, str]:
    """Classify an exception as retryable or not.

    Walks ``__cause__`` so Anthropic/OpenAI ``APIStatusError`` still classifies
    after being wrapped as ``LLMError`` (compat stream path).

    Prefer structured signals (status_code / transport type) over message
    heuristics so a wrapper like ``LLMError("… rate limited …")`` does not
    mask the underlying HTTP code.

    Returns (should_retry, reason).
    """
    chain = _exception_chain(exc)
    # Pass 1: structured classification only
    for item in chain:
        structured = _classify_structured(item)
        if structured is not None:
            return structured
    # Pass 2: message heuristics (any frame in the chain)
    for item in chain:
        heuristic = _classify_message_heuristic(item)
        if heuristic is not None:
            return heuristic
    label = " → ".join(type(item).__name__ for item in chain) or type(exc).__name__
    return False, f"{label}: not classified as retryable"


def _looks_like_quota_exhausted(exc: BaseException) -> bool:
    """True when an HTTP error indicates account quota/balance exhaustion.

    Walks the exception chain collecting SDK ``body`` (OpenAI-style
    ``{"error": {...}}``) and response text; 402 itself is treated as quota by
    ``_classify_structured`` regardless of markers (DeepSeek 官方 402=余额不足).
    """
    for item in _exception_chain(exc):
        body = getattr(item, "body", None)
        text = ""
        if isinstance(body, dict):
            text = json.dumps(body, ensure_ascii=False)
        else:
            response = getattr(item, "response", None)
            if response is not None:
                resp_body = getattr(response, "text", None) or getattr(response, "content", None)
                if isinstance(resp_body, bytes):
                    resp_body = resp_body.decode("utf-8", "replace")
                if resp_body:
                    text = str(resp_body)
        if not text:
            text = str(getattr(item, "message", "") or "") + " " + str(item)
        lowered = text.lower()
        if any(marker in lowered for marker in _QUOTA_EXHAUSTED_MARKERS):
            return True
    return False


def _classify_structured(exc: BaseException) -> tuple[bool, str] | None:
    """status_code / transport exception names — no message heuristics."""
    exc_name = type(exc).__name__

    if exc_name in (
        "ConnectionError",
        "ConnectTimeout",
        "ReadTimeout",
        "TimeoutError",
        "SSLCertVerificationError",
        "SSLError",
        "APIConnectionError",
        "APITimeoutError",
        "ConnectError",
        "RemoteProtocolError",
        "ReadError",
        "WriteError",
    ):
        return True, f"{exc_name}: network/transport failure"

    status_code = getattr(exc, "status_code", None)
    if status_code is not None:
        try:
            status_code = int(status_code)
        except (TypeError, ValueError):
            status_code = None
    if status_code is not None:
        # 配额耗尽优先判定：402（DeepSeek 余额不足）与 400/429 错误体带
        # insufficient_quota / balance 标记 → 不可重试。正常 429 限流不受影响。
        if status_code in (400, 402, 429) and _looks_like_quota_exhausted(exc):
            return False, f"HTTP {status_code} (quota exhausted)"
        if status_code == 402:
            return False, "HTTP 402 (payment required / 余额不足)"
        if status_code in _RETRYABLE_STATUS_CODES:
            return True, f"HTTP {status_code}"
        if status_code == 401:
            return False, "HTTP 401 (authentication failed)"
        if status_code == 400:
            return False, "HTTP 400 (bad request)"
        if status_code == 403:
            return False, "HTTP 403 (forbidden)"
        if status_code == 404:
            return False, "HTTP 404 (not found)"
        if status_code >= 500:
            return True, f"HTTP {status_code}"
        return False, f"HTTP {status_code}"
    return None


def _classify_message_heuristic(exc: BaseException) -> tuple[bool, str] | None:
    exc_str = str(exc).lower()
    for code in (429, 500, 502, 503, 504, 529):
        if f"error code: {code}" in exc_str or f"status code {code}" in exc_str or f"http {code}" in exc_str:
            return True, f"HTTP {code} (from message)"
    for code, label in (
        (401, "authentication failed"),
        (403, "forbidden"),
        (404, "not found"),
        (400, "bad request"),
    ):
        if f"error code: {code}" in exc_str or f"status code {code}" in exc_str:
            return False, f"HTTP {code} ({label})"
    if any(k in exc_str for k in ("rate limit", "ratelimit", "too many requests", "throttled")):
        # quota 耗尽（周额度/余额）不是瞬时过载：文案里常同时带 "rate limit"，
        # 3 次指数退避重试一个必败请求（浪费 14s + 3 次请求）。先查 quota marker。
        if _looks_like_quota_exhausted(exc):
            return False, "quota exhausted (not retryable)"
        return True, "rate limit (text heuristic)"
    if any(k in exc_str for k in ("overloaded", "server error", "temporarily unavailable", "try again")):
        if _looks_like_quota_exhausted(exc):
            return False, "quota exhausted (not retryable)"
        return True, "server overload (text heuristic)"
    return None


def _compute_delay(attempt: int) -> float:
    """Exponential backoff with jitter: delay = min(base * 2^attempt + jitter, max)."""
    delay = _BASE_DELAY * (2**attempt)
    jitter = random.uniform(0, delay * 0.2)  # up to 20% jitter
    return min(delay + jitter, _MAX_DELAY)


def _retry_after_seconds(exc: Exception) -> float | None:
    """Server-provided Retry-After delay (integer seconds) carried by an SDK exception, if any."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None or not hasattr(headers, "get"):
        return None
    raw = headers.get("retry-after")
    if raw is None:
        raw = headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(int(str(raw).strip()))
    except (TypeError, ValueError):
        return None


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    max_retries: int = 3,
    operation_name: str = "LLM call",
    provider_name: str = "",
    retry_if: Callable[[Exception], bool] | None = None,
) -> T:
    """Execute an async function with connection-recovery retry logic.

    总预算上限（``llm.total_timeout_seconds``，默认 300s）对单次调用**全程**生效：
    每次 attempt 外包 ``asyncio.wait_for(remaining)``，单次 attempt 挂到预算耗尽
    即被杀掉并抛预算错误（滴漏式响应也逃不掉）；重试间隙同样检查预算。
    超预算时尽力 abort provider 的 HTTP client，释放卡死的连接。

    Args:
        fn: The async callable to retry.
        max_retries: Maximum number of retry attempts.
        operation_name: Human-readable name for logging.
        provider_name: Provider registry key, used to abort the stuck HTTP client
            when the total budget is exceeded mid-attempt.
        retry_if: Optional gate consulted after an error is classified as
            retryable — returning False declines the retry (e.g. a stream that
            already produced chunks must not be regenerated wholesale, or
            already-metered tokens would be billed twice)

    Returns:
        The result of ``fn()``.

    Raises:
        LLMError: If all retries are exhausted, the total budget is exceeded,
            or the error is not retryable.
    """
    import time

    from src.llm.timeouts import llm_total_timeout_seconds

    def _abort_provider_client() -> None:
        if not provider_name:
            return
        with contextlib.suppress(Exception):
            from src.llm.registry import provider_registry

            provider_registry.get(provider_name).abort()

    def _budget_error() -> LLMError:
        return LLMError(f"等待模型完整响应超过 {budget:.0f} 秒（llm.total_timeout_seconds），已中止本次调用")

    last_exception: Exception | None = None
    started = time.monotonic()
    budget = llm_total_timeout_seconds()

    for attempt in range(max_retries + 1):
        remaining = budget - (time.monotonic() - started)
        if remaining <= 0:
            logger.error(
                f"{operation_name} giving up: total time budget {budget:.0f}s exhausted before attempt {attempt + 1}"
            )
            raise _budget_error() from last_exception
        try:
            return await asyncio.wait_for(fn(), timeout=remaining)
        except Exception as exc:
            elapsed = time.monotonic() - started
            over_budget = elapsed >= budget
            if isinstance(exc, TimeoutError) and over_budget:
                # wait_for 在预算处杀死挂住的 attempt（滴漏式响应绕开 read 超时的
                # 唯一出口）；fn 自己在预算内抛的读超时不会走到这里（elapsed < budget）
                _abort_provider_client()
                logger.error(f"{operation_name}: total time budget {budget:.0f}s exceeded mid-attempt; aborted")
                raise _budget_error() from last_exception
            last_exception = exc
            should_retry, reason = _is_retryable_exception(exc)
            if should_retry and retry_if is not None and not retry_if(exc):
                should_retry = False
                reason = f"{reason}; conditional retry declined by retry_if"

            if not should_retry or attempt >= max_retries or over_budget:
                if over_budget and should_retry and attempt < max_retries:
                    logger.error(
                        f"{operation_name} giving up: total time budget {budget:.0f}s exceeded (last error: {reason})"
                    )
                elif should_retry:
                    logger.error(f"{operation_name} failed after {max_retries} retries. Last error: {reason}")
                else:
                    logger.error(f"{operation_name} failed with non-retryable error: {reason}")
                raise

            delay = _compute_delay(attempt)
            retry_after = _retry_after_seconds(exc)
            if retry_after is not None:
                # Retry-After 封顶 60s：恶意/异常大的服务端值（如 24h）不该
                # 让回合挂死；总预算检查（循环开头）仍是最终兜底
                delay = min(max(retry_after, delay), _RETRY_AFTER_MAX_SECONDS)
            logger.info(
                f"{operation_name} attempt {attempt + 1} failed ({reason}), "
                f"retrying in {delay:.1f}s (attempt {attempt + 1}/{max_retries})"
            )
            await asyncio.sleep(delay)

    # Should never reach here, but satisfies type checker
    raise LLMError(f"{operation_name} failed after {max_retries} retries") from last_exception


# Convenience wrapper for provider methods


def retry_complete(
    fn: Callable[[], Awaitable[T]],
    *,
    provider_name: str = "unknown",
    max_retries: int = 3,
    retry_if: Callable[[Exception], bool] | None = None,
) -> Awaitable[T]:
    """Wrap a provider ``complete()`` call with retry logic.

    ``retry_if``：可选条件重试闸门，错误可重试时再经它裁决；聚合整条流的
    complete 实现应传「零 chunk 才重试」，避免已计量 token 重复扣费
    """
    return with_retry(
        fn,
        max_retries=max_retries,
        operation_name=f"{provider_name} complete",
        provider_name=provider_name,
        retry_if=retry_if,
    )


def retry_stream_init(
    fn: Callable[[], Awaitable[T]],
    *,
    provider_name: str = "unknown",
    max_retries: int = 3,
) -> Awaitable[T]:
    """Wrap the *initial connection* of a streaming call with retry logic.

    Note: this only retries the initial HTTP request that starts the stream.
    Mid-stream errors are NOT retried because the consumer may have already
    processed partial chunks.
    """
    return with_retry(
        fn,
        max_retries=max_retries,
        operation_name=f"{provider_name} stream_init",
        provider_name=provider_name,
    )
