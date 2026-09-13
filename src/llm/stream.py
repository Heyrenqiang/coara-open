"""
Coara v8 - 流式调用包装

提供统一的流式响应处理和聚合功能。
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator
from typing import Any

from src.core.logger import logger
from src.core.types import ToolCall
from src.llm.provider import LLMResponse, StreamChunk, ToolCallDelta
from src.llm.tool_arguments import parse_tool_call_arguments

__all__ = ["StreamAggregator", "collect_stream"]


class StreamAggregator:
    """
    流式响应聚合器。

    将 StreamChunk 流聚合为完整的 LLMResponse。
    正确处理 JSON 字符串片段形式的工具调用参数。
    """

    def __init__(self):
        self.content_parts: list[str] = []
        self.reasoning_parts: list[str] = []
        self.tool_calls: dict[int, ToolCallDelta] = {}
        self._arg_buffers: dict[int, list[str]] = {}
        self.finish_reason: str | None = None
        self.usage: dict[str, int] = {}

    def add_chunk(self, chunk: StreamChunk) -> None:
        if chunk.delta_reasoning:
            self.reasoning_parts.append(chunk.delta_reasoning)
            return

        if chunk.delta_content:
            self.content_parts.append(chunk.delta_content)

        if chunk.delta_tool_calls:
            for tc in chunk.delta_tool_calls:
                # Accumulate by content-block index: the id may only arrive in a
                # later delta, so keying by id would leave a stale empty entry.
                key = tc.index
                if key not in self.tool_calls:
                    self.tool_calls[key] = ToolCallDelta(
                        index=tc.index,
                        id=tc.id,
                        name=tc.name,
                        arguments_fragment="",
                    )
                    self._arg_buffers[key] = []
                else:
                    if tc.id and not self.tool_calls[key].id:
                        self.tool_calls[key].id = tc.id
                    if tc.name and not self.tool_calls[key].name:
                        self.tool_calls[key].name = tc.name

                self._arg_buffers[key].append(tc.arguments_fragment)

        if chunk.finish_reason:
            self.finish_reason = chunk.finish_reason

        if chunk.usage:
            for key, value in chunk.usage.items():
                try:
                    numeric = int(value)
                except (TypeError, ValueError):
                    logger.debug(f"Dropping non-numeric usage value: {key}={value!r}")
                    continue
                if key == "input_tokens" or key == "prompt_tokens":
                    self.usage["input_tokens"] = numeric
                elif key == "output_tokens" or key == "completion_tokens":
                    # Anthropic/OpenAI stream usage is cumulative on output side.
                    self.usage["output_tokens"] = max(self.usage.get("output_tokens", 0), numeric)
                elif key == "total_tokens":
                    self.usage["total_tokens"] = max(self.usage.get("total_tokens", 0), numeric)
                else:
                    self.usage[key] = numeric

    def build_response(self, *, wire_blocks: bool = False) -> LLMResponse:
        content = "".join(self.content_parts)
        reasoning_content = "".join(self.reasoning_parts).strip() or None

        tool_calls = []
        for tc in self.tool_calls.values():
            raw_args = "".join(self._arg_buffers.get(tc.index, [])).strip()
            arguments = parse_tool_call_arguments(raw_args) if raw_args else {}

            tool_calls.append(
                ToolCall(
                    id=tc.id or f"tc_{tc.index}",
                    name=tc.name,
                    arguments=arguments,
                )
            )

        provider_content_blocks: list[dict[str, Any]] | None = None
        if wire_blocks and (reasoning_content or content or tool_calls):
            provider_content_blocks = []
            if reasoning_content:
                provider_content_blocks.append({"type": "thinking", "thinking": reasoning_content})
            if content:
                provider_content_blocks.append({"type": "text", "text": content})
            for tc in tool_calls:
                provider_content_blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.name,
                        "input": tc.arguments,
                    }
                )
        elif reasoning_content and not tool_calls:
            provider_content_blocks = [{"type": "thinking", "thinking": reasoning_content}]
            if content:
                provider_content_blocks.append({"type": "text", "text": content})

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            # 无 finish chunk 的静默截断标 partial：不再谎报 stop 按完整回合
            # 处理，让 usage 入账与截断恢复判断能识别这种残缺形态
            finish_reason=self.finish_reason or "partial",
            usage=self.usage or None,
            reasoning_content=reasoning_content,
            provider_content_blocks=provider_content_blocks,
        )


async def _invoke_on_chunk(on_chunk: Any | None, chunk: StreamChunk) -> None:
    if on_chunk is None:
        return
    try:
        if inspect.iscoroutinefunction(on_chunk):
            await on_chunk(chunk)
        else:
            result = on_chunk(chunk)
            if asyncio.iscoroutine(result):
                await result
    except Exception as exc:
        logger.warning(f"Error in on_chunk callback: {exc}")


async def collect_stream(
    stream: AsyncIterator[StreamChunk],
    on_chunk: Any | None = None,
    *,
    wire_blocks: bool = False,
) -> LLMResponse:
    aggregator = StreamAggregator()
    chunks_seen = 0
    received_delta = False

    try:
        async for chunk in stream:
            chunks_seen += 1
            aggregator.add_chunk(chunk)
            if chunk.delta_content or chunk.delta_reasoning or chunk.delta_tool_calls:
                received_delta = True
            await _invoke_on_chunk(on_chunk, chunk)
    except Exception as exc:
        # 失败断点信息随异常带出（与 turn_completion 层同一模式）：外层仅在
        # 零 chunk 时才允许整包重试，provider 已返回的部分 usage 供 usage
        # 收集以 partial 形态入账
        exc.stream_received_delta = received_delta
        exc.stream_chunks_seen = chunks_seen
        if aggregator.usage:
            exc.partial_usage = dict(aggregator.usage)
        raise

    return aggregator.build_response(wire_blocks=wire_blocks)
