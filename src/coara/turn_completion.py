"""LLM turn completion orchestrator.

Encapsulates provider.complete() calls, tool validation, and interruptible
wrapping. Prefer LLMService.complete() for new code.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import AsyncIterator, Callable
from typing import Any

from src.core.abort import AbortSignal
from src.core.errors import EmptyResponseError, LLMError
from src.core.types import Message
from src.llm.endpoints import preserves_anthropic_thinking_wire
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
    """EMPTY_RESPONSE：正常收尾但零内容（无文本/工具/思考）按错误处理。

    参考 deepseek-harness 的 EMPTY_RESPONSE 语义——finish 时零内容块不当作
    成功空消息。零 delta 的调用方（流式回退/重发）不受影响：空响应没有产出
    任何 token，整 prompt 重发不重复计费；若重试后仍空则向上报错，不再静默。
    """
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
    wire_blocks: bool = False,
    deadline: float | None = None,
    provider: LLMProvider | None = None,
) -> LLMResponse:
    """Aggregate a provider stream, honouring abort signals and optional CLI deltas.

    ``deadline``（monotonic 墙钟）是单次调用的全程硬上限：滴漏式分片会让
    httpx read 超时不断重置，唯有这里比对墙钟才能兜底。超限时 abort provider
    的 HTTP client 释放卡死连接，并抛出带预算说明的 LLMError。
    """
    import time as _time

    aggregator = StreamAggregator()
    stream_iter = aiter(stream)
    received_delta = False

    def _remaining() -> float | None:
        if deadline is None:
            return None
        return deadline - _time.monotonic()

    def _raise_budget_exceeded() -> None:
        if provider is not None:
            with contextlib.suppress(Exception):
                provider.abort()
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
                chunk_task = asyncio.create_task(anext(stream_iter))
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
            if chunk.delta_content or chunk.delta_reasoning or chunk.delta_tool_calls:
                received_delta = True
            await _emit_assistant_delta(on_assistant_delta, chunk)
    except LLMError as exc:
        # 失败断点信息随异常带出：上层仅在零 delta 时才允许整 prompt 重发；
        # provider 已返回的部分 usage 供 usage 收集以 partial 形态入账
        exc.stream_received_delta = received_delta
        if aggregator.usage:
            exc.partial_usage = dict(aggregator.usage)
        # 流式续传用：已聚合的部分内容随异常带出
        partial = aggregator.build_response(wire_blocks=wire_blocks)
        exc.partial_content = partial.content or ""
        raise

    response = aggregator.build_response(wire_blocks=wire_blocks)
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
    wire_blocks = preserves_anthropic_thinking_wire(getattr(provider, "base_url", None))

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
            stream,
            signal,
            on_assistant_delta,
            wire_blocks=wire_blocks,
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
                # 会重复计费且丢弃已产 token，与流式路径的零 delta 守卫对齐：不重发
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

    # tools non-empty but supports_tools is False (base class is always True):
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
    """Wrap an async operation so it can be cancelled via an AbortSignal.

    ``join_on_cancel``：取消后先等 operation 收尾再抛中断。仅用于对取消
    有界响应的 operation（工具执行器会在有界 drain 后把批次真实结果写入
    interrupt_sink），让上层拿到收尾产物
    """
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
                # CancelledError），裸 await 会把中断/关停挂死。超时放弃等待，
                # 残留任务交 finally 的 _cleanup_abandoned_task 兜底。
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


class PlanSubmittedError(Exception):
    """plan_mode(submit) 成功后抛：计划已展示给用户，回合正常收尾（非打断）。

    与 ``CoaraRunCancelledError`` 语义区分：那是「打断/取消」，会注入「会话已
    打断」并走中断收尾；本异常是「正常完成」，编排器捕获后只做静默收尾——
    不注入打断文案、不回滚历史。executor 透传通道与 CoaraRunCancelledError 同
    路（emit tool_complete 后 raise）。
    """

    def __init__(self, plan_file: str = ""):
        super().__init__(plan_file or "plan_submitted")
        self.plan_file = plan_file
