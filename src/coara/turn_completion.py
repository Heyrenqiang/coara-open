"""LLM turn completion orchestrator"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import AsyncIterator, Callable
from typing import Any

from src.core.abort import AbortSignal
from src.core.errors import EmptyResponseError, LLMError
from src.core.types import Message
from src.llm.provider import LLMProvider, LLMResponse, StreamChunk
from src.llm.stream import StreamAggregator
from src.llm.tool_arguments import sanitize_tool_parameters


def validate_tool_definitions(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sanitize tool definitions before sending to LLM."""
    validated = []
    for tool in tools:
        try:
            name = tool.get("name", "")
            if not name:
                continue
            description = tool.get("description", f"Tool: {name}")
            parameters = sanitize_tool_parameters(tool, strict=True)
            validated.append({"name": name, "description": description, "parameters": parameters})
        except Exception:
            continue
    return validated


def _ensure_non_empty_response(response: LLMResponse, *, operation: str) -> None:
    """EMPTY_RESPONSE：正常收尾但零内容（无文本/工具/思考）按错误处理"""
    if (
        response.finish_reason in ("stop", "end_turn")
        and not response.content
        and not response.tool_calls
        and not response.reasoning_content
    ):
        raise EmptyResponseError(f"模型返回空响应（{operation}）：finish={response.finish_reason} 且无内容/工具/思考")


async def _emit_assistant_delta(
    on_assistant_delta: Callable[[str], Any] | None,
    chunk: StreamChunk,
) -> None:
    if on_assistant_delta is None:
        return
    if not chunk.delta_content:
        return
    result = on_assistant_delta(chunk.delta_content)
    if inspect.isawaitable(result):
        await result


async def _collect_stream_interruptible(
    stream: AsyncIterator[StreamChunk],
    signal: AbortSignal | None,
    on_assistant_delta: Callable[[str], Any] | None,
    *,
    deadline: float | None = None,
    provider: LLMProvider | None = None,
) -> LLMResponse:
    """Aggregate a provider stream, honouring abort signals and optional CLI deltas.

    超时双判据：流空闲看门狗（主，每收到一帧续命）+ 总预算硬上限（兜底半开连接）。
    """
    import time as _time

    from src.llm.timeouts import llm_stream_idle_timeout_seconds

    aggregator = StreamAggregator()
    stream_iter = aiter(stream)
    received_delta = False
    idle_seconds = llm_stream_idle_timeout_seconds()
    last_frame_at = _time.monotonic()

    def _remaining() -> float | None:
        """下一次等待的时限：空闲预算与总预算取更近者。"""
        candidates = [last_frame_at + idle_seconds]
        if deadline is not None:
            candidates.append(deadline)
        remain = min(candidates) - _time.monotonic()
        return max(remain, 0.0)

    def _raise_budget_exceeded() -> None:
        if provider is not None:
            with contextlib.suppress(Exception):
                provider.abort()
        idle_deadline = last_frame_at + idle_seconds
        if deadline is None or idle_deadline <= deadline:
            raise LLMError(f"模型响应流空闲超时（{int(idle_seconds)}s 无新帧），已中止本次调用")
        raise LLMError("等待模型完整响应超时（llm.total_timeout_seconds 硬上限），已中止本次调用")

    try:
        while True:
            if signal is not None and signal.aborted:
                raise CoaraRunCancelledError(signal.reason or "interrupted")

            remaining = _remaining()
            if remaining is not None and remaining <= 0:
                _raise_budget_exceeded()

            if signal is None:
                try:
                    if remaining is None:
                        chunk = await anext(stream_iter)
                    else:
                        chunk = await asyncio.wait_for(anext(stream_iter), timeout=remaining)
                except StopAsyncIteration:
                    break
                except TimeoutError:
                    _raise_budget_exceeded()
            else:
                # anext() 给出的是 asend 可等待对象，ensure_future 与 create_task 等价且类型可推断
                chunk_task: asyncio.Task[StreamChunk] = asyncio.ensure_future(anext(stream_iter))
                signal_task: asyncio.Task | None = None
                try:
                    signal_task = asyncio.create_task(signal.wait())
                    done, _pending = await asyncio.wait(
                        {chunk_task, signal_task},
                        timeout=remaining,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if not done:
                        chunk_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await chunk_task
                        _raise_budget_exceeded()
                    if signal_task in done:
                        chunk_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await chunk_task
                        raise CoaraRunCancelledError(signal.reason or "interrupted") from None
                    chunk = chunk_task.result()
                except StopAsyncIteration:
                    break
                finally:
                    if signal_task is not None:
                        signal_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await signal_task

            aggregator.add_chunk(chunk)
            last_frame_at = _time.monotonic()  # 每帧续命：看门狗计时归零重数
            if chunk.delta_content or chunk.delta_reasoning or chunk.delta_tool_calls:
                received_delta = True
            await _emit_assistant_delta(on_assistant_delta, chunk)
    except LLMError as exc:
        # 失败断点信息随异常带出：上层仅在零 delta 时才允许整 prompt 重发；
        # provider 已返回的部分 usage 供 usage 收集以 partial 形态入账；断点信息动态挂载
        exc.stream_received_delta = received_delta  # type: ignore[attr-defined]
        if aggregator.usage:
            exc.partial_usage = dict(aggregator.usage)  # type: ignore[attr-defined]
        # 流式续传用：已聚合的部分内容随异常带出
        partial = aggregator.build_response()
        exc.partial_content = partial.content or ""  # type: ignore[attr-defined]
        raise

    response = aggregator.build_response()
    _ensure_non_empty_response(response, operation="stream")
    return response


async def _complete_with_provider_streaming(
    provider: LLMProvider,
    model_name: str,
    tool_definitions: list[dict[str, Any]] | None,
    system_prompt: str | None,
    messages: list[Message],
    *,
    max_tokens: int | None = None,
    temperature: float = 0.7,
    tool_choice: dict[str, Any] | None = None,
    signal: AbortSignal | None = None,
    on_assistant_delta: Callable[[str], Any] | None = None,
) -> LLMResponse:
    """Streaming twin of ``_complete_with_provider`` — same fallbacks, live text deltas."""
    effective_max_tokens = max_tokens or provider.get_default_max_tokens(model_name)
    extra_kwargs: dict[str, Any] = {}
    if tool_choice is not None:
        extra_kwargs["tool_choice"] = tool_choice

    tools = tool_definitions or []

    async def _run_stream(*, validated_tools: list[dict[str, Any]] | None) -> LLMResponse:
        import time as _time

        from src.llm.timeouts import llm_total_timeout_seconds

        stream = provider.stream_complete(
            messages=messages,
            model=model_name,
            max_tokens=effective_max_tokens,
            temperature=temperature,
            system_prompt=system_prompt,
            tools=validated_tools,
            **extra_kwargs,
        )
        # 单次调用全程硬上限（连接 + 首字节 + 分片消费），滴漏式响应也逃不掉
        deadline = _time.monotonic() + llm_total_timeout_seconds()
        return await _collect_stream_interruptible(
            # provider.stream_complete 运行时是 async generator（基类签名标成了 coroutine）
            stream,  # type: ignore[arg-type]
            signal,
            on_assistant_delta,
            deadline=deadline,
            provider=provider,
        )

    if tools and provider.supports_tools(model_name):
        validated_tools = validate_tool_definitions(tools)
        if validated_tools:
            try:
                return await _run_stream(validated_tools=validated_tools)
            except LLMError as exc:
                # 已收到部分内容：整 prompt 重发会重复计费且与已流式输出脱节，
                # 直接报错；只有零 delta（请求未真正开始）才允许回退重发
                if tool_choice is not None or getattr(exc, "stream_received_delta", False):
                    raise

    if not tools:
        try:
            return await _run_stream(validated_tools=None)
        except LLMError as exc:
            if getattr(exc, "stream_received_delta", False):
                raise

    return await _complete_with_provider(
        provider,
        model_name,
        tool_definitions,
        system_prompt,
        messages,
        max_tokens=max_tokens,
        temperature=temperature,
        tool_choice=tool_choice,
        on_assistant_delta=on_assistant_delta,
    )


async def _emit_assistant_delta_text(
    on_assistant_delta: Callable[[str], Any] | None,
    text: str,
) -> None:
    if on_assistant_delta is None or not text:
        return
    result = on_assistant_delta(text)
    if inspect.isawaitable(result):
        await result


async def _complete_with_provider(
    provider: LLMProvider,
    model_name: str,
    tool_definitions: list[dict[str, Any]] | None,
    system_prompt: str | None,
    messages: list[Message],
    *,
    max_tokens: int | None = None,
    temperature: float = 0.7,
    tool_choice: dict[str, Any] | None = None,
    on_assistant_delta: Callable[[str], Any] | None = None,
) -> LLMResponse:
    """Perform a single LLM completion with optional native tool calling."""
    effective_max_tokens = max_tokens or provider.get_default_max_tokens(model_name)
    extra_kwargs: dict[str, Any] = {}
    if tool_choice is not None:
        extra_kwargs["tool_choice"] = tool_choice

    tools = tool_definitions or []

    if tools and provider.supports_tools(model_name):
        validated_tools = validate_tool_definitions(tools)
        if validated_tools:
            try:
                response = await provider.complete(
                    messages=messages,
                    model=model_name,
                    max_tokens=effective_max_tokens,
                    temperature=temperature,
                    system_prompt=system_prompt,
                    tools=validated_tools,
                    **extra_kwargs,
                )
                _ensure_non_empty_response(response, operation="complete")
                await _emit_assistant_delta_text(on_assistant_delta, response.content)
                return response
            except LLMError as exc:
                if tool_choice is not None:
                    raise
                # 流式已产出部分内容（如 _stream_aggregate 中途失败）时整 prompt 重发
                if getattr(exc, "stream_received_delta", False):
                    raise
                # Native tool-calling failure must surface: silent text-protocol
                # degrade used to hide provider errors and reshape the request.
                raise

    if not tools:
        response = await provider.complete(
            messages=messages,
            model=model_name,
            max_tokens=effective_max_tokens,
            temperature=temperature,
            system_prompt=system_prompt,
            tools=None,
            **extra_kwargs,
        )
        _ensure_non_empty_response(response, operation="complete")
        await _emit_assistant_delta_text(on_assistant_delta, response.content)
        return response

    # complete without tools rather than drop the tool definitions silently.
    response = await provider.complete(
        messages=messages,
        model=model_name,
        max_tokens=effective_max_tokens,
        temperature=temperature,
        system_prompt=system_prompt,
        tools=None,
        **extra_kwargs,
    )
    _ensure_non_empty_response(response, operation="complete")
    await _emit_assistant_delta_text(on_assistant_delta, response.content)
    return response


# Strong references to in-flight abandoned-task cleanups so they are not
# garbage-collected mid-run. Each cleanup is bounded by its own timeout.
_cleanup_tasks: set[asyncio.Task] = set()


async def _await_interruptible(
    operation: Any,
    signal: AbortSignal,
    *,
    join_on_cancel: bool = False,
) -> Any:
    """Wrap an async operation so it can be cancelled via an AbortSignal."""
    operation_task = asyncio.create_task(operation)
    signal_task = asyncio.create_task(signal.wait())

    try:
        done, _pending = await asyncio.wait(
            {operation_task, signal_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if signal_task in done:
            operation_task.cancel()
            if join_on_cancel:
                # 收尾等待必须有界：operation 若不响应取消（同步阻塞/吞
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.wait_for(asyncio.shield(operation_task), timeout=5.0)
            raise CoaraRunCancelledError(signal.reason or "interrupted") from None
        if signal.aborted:
            operation_task.cancel()
            raise CoaraRunCancelledError(signal.reason or "interrupted") from None
        return operation_task.result()
    except asyncio.CancelledError:
        if signal.aborted:
            raise CoaraRunCancelledError(signal.reason or "interrupted") from None
        raise
    finally:
        signal_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await signal_task
        if not operation_task.done():
            operation_task.cancel()
            cleanup_task = asyncio.create_task(_cleanup_abandoned_task(operation_task))
            _cleanup_tasks.add(cleanup_task)
            cleanup_task.add_done_callback(_cleanup_tasks.discard)


async def _cleanup_abandoned_task(task: asyncio.Task, timeout: float = 0.5) -> None:
    """Give an abandoned task a short grace period to clean up, then move on."""
    with contextlib.suppress(Exception):
        await asyncio.wait_for(task, timeout=timeout)


class CoaraRunCancelledError(Exception):
    """Raised when the current turn is interrupted."""

    def __init__(self, reason: str = "interrupted"):
        super().__init__(reason)
        self.reason = reason
