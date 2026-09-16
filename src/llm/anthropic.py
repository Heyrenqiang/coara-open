"""Anthropic provider implementation."""

# mypy: ignore-errors

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from typing import Any

import anthropic
from anthropic import AsyncAnthropic
from anthropic.types import Message as AnthropicMessage

from src.core.errors import APIKeyError, ContextWindowExceededError, LLMError
from src.core.logger import logger
from src.core.types import Message, MessageRole, ToolCall
from src.llm._debug import truncate_dict
from src.llm._http_provider import HTTPProviderMixin
from src.llm.call_defaults import clamp_max_tokens_for_model
from src.llm.endpoints import (
    is_kimi_anthropic_endpoint,
    is_minimax_anthropic_endpoint,
    preserves_anthropic_thinking_wire,
)
from src.llm.errors import wrap_api_status_error, wrap_unexpected_error
from src.llm.message_content import anthropic_assistant_wire_content
from src.llm.provider import LLMProvider, LLMResponse, StreamChunk, ToolCallDelta
from src.llm.retry import retry_complete, retry_stream_init
from src.llm.tool_arguments import sanitize_tool_parameters
from src.llm.vendor_options import (
    kimi_messages_kwargs,
    minimax_messages_kwargs,
    minimax_prefers_streaming_complete,
)

_EMPTY_TOOL_RESULT_PLACEHOLDER = "(empty)"


def _nonempty_tool_result_text(raw: Any) -> str:
    text = str(raw if raw is not None else "").strip()
    return text or _EMPTY_TOOL_RESULT_PLACEHOLDER


def _sanitize_tool_result_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ensure tool_result content blocks have non-empty text (Kimi rejects empty)."""
    out: list[dict[str, Any]] = []
    for block in blocks:
        if block.get("type") == "text":
            out.append({"type": "text", "text": _nonempty_tool_result_text(block.get("text"))})
        else:
            out.append(block)
    return out or [{"type": "text", "text": _EMPTY_TOOL_RESULT_PLACEHOLDER}]


ANTHROPIC_CONTEXT_WINDOWS = {
    # MiniMax (Anthropic-compatible endpoint)
    "MiniMax-M3": 1_000_000,
    # Kimi Code (Anthropic-compatible; k3 up to 1M by plan, k3-256k fixed 256K)
    "k3": 1_048_576,
    "k3-256k": 262_144,
}


def _reasoning_from_blocks(blocks: list[dict[str, Any]]) -> str | None:
    parts = [str(block.get("thinking") or "") for block in blocks if block.get("type") == "thinking"]
    text = "".join(parts).strip()
    return text or None


def _user_content_with_vision_gate(content: list[dict[str, Any]], vision: bool) -> list[dict[str, Any]]:
    """USER 消息 content 列表按视觉门清洗：image 块不支持时替换为占位文本。

    text / 其它类型块原样保留，避免破坏既有 meta 标签等结构。
    """
    from src.llm.vision import IMAGE_OMITTED_PLACEHOLDER

    out: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            out.append({"type": "text", "text": block.get("text") or ""})
        elif btype == "image":
            out.append(block if vision else {"type": "text", "text": IMAGE_OMITTED_PLACEHOLDER})
        else:
            out.append(block)
    return out or [{"type": "text", "text": ""}]


def _requires_streaming_for_create(model: str, max_tokens: int) -> bool:
    """Return True when anthropic SDK would refuse a non-streaming messages.create call."""
    try:
        from anthropic._constants import MODEL_NONSTREAMING_TOKENS
    except ImportError:
        model_nonstreaming_tokens: dict[str, int] = {}
    else:
        model_nonstreaming_tokens = MODEL_NONSTREAMING_TOKENS

    model_limit = model_nonstreaming_tokens.get(model)
    return bool(model_limit is not None and max_tokens > model_limit)


def _is_anthropic_compat_endpoint(base_url: str | None) -> bool:
    """Third-party Anthropic-compatible APIs (MiniMax, etc.) — avoid SDK parsed stream."""
    base = (base_url or "").strip().lower()
    return bool(base) and "api.anthropic.com" not in base


def _retry_only_if_stream_not_started(exc: Exception) -> bool:
    """条件重试闸门：流已产出任何 chunk 时拒绝整包重试

    已产 chunk 意味着 provider 可能已计量 token，整包重发会重复扣费且与
    随异常带出的 partial usage 脱节；只有零 chunk（请求未真正开始）才安全重发
    """
    return not getattr(exc, "stream_chunks_seen", 0)


_EPHEMERAL_CACHE_CONTROL = {"type": "ephemeral"}


def _apply_system_cache_control(system: str | None) -> str | list[dict[str, Any]] | None:
    """Return system as Anthropic content blocks with cache_control on the last block."""
    if not system:
        return system
    return [{"type": "text", "text": system, "cache_control": _EPHEMERAL_CACHE_CONTROL}]


def _apply_message_cache_control(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Place cache_control on the last cacheable content block of the final message.

    Copy-on-write: list content blocks may alias the caller's ``Message.content``,
    so the outer message and the marked block are copied instead of mutated.
    Never attach cache_control to empty text (Kimi / Anthropic reject it).
    """
    if not messages:
        return messages
    last = messages[-1]
    content = last.get("content")
    if isinstance(content, str):
        text = content.strip() or _EMPTY_TOOL_RESULT_PLACEHOLDER
        patched = {
            **last,
            "content": [{"type": "text", "text": text, "cache_control": _EPHEMERAL_CACHE_CONTROL}],
        }
        return [*messages[:-1], patched]
    if isinstance(content, list) and content:
        # Only mark text/tool_result blocks; skip if already marked.
        last_block = content[-1]
        if isinstance(last_block, dict) and "cache_control" not in last_block:
            if last_block.get("type") == "text" and not str(last_block.get("text") or "").strip():
                return messages
            patched_block = {**last_block, "cache_control": _EPHEMERAL_CACHE_CONTROL}
            patched = {**last, "content": [*content[:-1], patched_block]}
            return [*messages[:-1], patched]
    return messages


class AnthropicProvider(HTTPProviderMixin, LLMProvider):
    """Anthropic provider."""

    _client_cls = AsyncAnthropic

    def __init__(
        self,
        name: str,
        api_key: str,
        base_url: str | None = None,
        default_model: str = "",
        default_max_tokens: int | None = None,
        vision_model_ids: frozenset[str] | None = None,
    ):
        super().__init__(name, api_key, base_url, default_model, default_max_tokens)
        self._vision_model_ids = vision_model_ids

        # Empty key is allowed so CLI/UI can start; complete()/stream_complete() raise APIKeyError.
        from src.llm._http_provider import init_sdk_client

        self.client = init_sdk_client(AsyncAnthropic, api_key, base_url)
        self._closed = False
        self._abort_lock = asyncio.Lock()
        self._abort_task: asyncio.Task | None = None

    def _require_api_key(self) -> None:
        if not (self.api_key or "").strip():
            raise APIKeyError(f"API key is required for Anthropic provider '{self.name}'")

    def _convert_messages(
        self,
        messages: list[Message],
        system_prompt: str | None = None,
        *,
        model: str = "",
    ) -> tuple[str | list[dict[str, Any]] | None, list[dict[str, Any]]]:
        from src.llm.orphan_repair import close_orphan_tool_calls
        from src.llm.vision import IMAGE_OMITTED_PLACEHOLDER, model_supports_vision

        # 发送前闭合孤儿 tool_call 配对（与 OpenAI/Responses 驱动同一共享逻辑），
        # 防压缩/回滚/恢复造成的历史污染被严格端点 400 拒掉
        messages = close_orphan_tool_calls(messages)
        vision = model_supports_vision(model, provider_name=self.name, vision_model_ids=self._vision_model_ids)
        system = system_prompt
        converted: list[dict[str, Any]] = []

        for msg in messages:
            if msg.role == MessageRole.SYSTEM:
                system = f"{system}\n\n{msg.content}" if system else str(msg.content)
                continue

            if msg.role == MessageRole.USER:
                content = msg.content
                if isinstance(content, list):
                    content = _user_content_with_vision_gate(content, vision)
                if isinstance(content, str) and not content.strip():
                    content = _EMPTY_TOOL_RESULT_PLACEHOLDER
                converted.append({"role": "user", "content": content})
                continue

            if msg.role == MessageRole.ASSISTANT:
                converted.append({"role": "assistant", "content": anthropic_assistant_wire_content(msg)})
                continue

            if msg.role == MessageRole.TOOL_RESULT:
                # Support multimodal content blocks (text + image) in tool results.
                # When content is a list of dicts, preserve each block independently
                # so that meta tags (e.g. <系统消息>) and actual content remain separate.
                if isinstance(msg.content, list):
                    tool_content: list[dict[str, Any]] = []
                    for block in msg.content:
                        if isinstance(block, dict):
                            if block.get("type") == "text":
                                tool_content.append(
                                    {
                                        "type": "text",
                                        "text": _nonempty_tool_result_text(block.get("text")),
                                    }
                                )
                            elif block.get("type") == "image":
                                if vision:
                                    tool_content.append({"type": "image", "source": block["source"]})
                                else:
                                    tool_content.append({"type": "text", "text": IMAGE_OMITTED_PLACEHOLDER})

                    # Fallback: if no recognized blocks, serialize as text
                    if not tool_content:
                        tool_content.append({"type": "text", "text": _nonempty_tool_result_text(msg.content)})
                    else:
                        tool_content = _sanitize_tool_result_blocks(tool_content)

                    converted.append(
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": msg.tool_call_id,
                                    "content": tool_content,
                                }
                            ],
                        }
                    )
                else:
                    converted.append(
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": msg.tool_call_id,
                                    "content": _nonempty_tool_result_text(msg.content),
                                }
                            ],
                        }
                    )

        # Prompt caching applies to real Anthropic endpoints as well as
        # Anthropic-compatible ones (3 breakpoints, within the limit of 4).
        converted = _apply_message_cache_control(converted)
        system = _apply_system_cache_control(system)

        return system, converted

    def _convert_tools(self, tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        """Convert tool definitions to Anthropic format.

        Adds validation and sanitization to prevent schema errors.
        """
        if not tools:
            return None

        converted = []
        for tool in tools:
            name = tool.get("name", "")
            description = tool.get("description", "")
            parameters = sanitize_tool_parameters(tool)

            converted.append(
                {
                    "name": name,
                    "description": description,
                    "input_schema": parameters,
                }
            )

        if converted:
            converted[-1]["cache_control"] = {"type": "ephemeral"}

        logger.debug(f"Converted {len(converted)} tools for Anthropic API")
        return converted

    def _is_compat_endpoint(self) -> bool:
        return _is_anthropic_compat_endpoint(self.base_url)

    _USAGE_FIELDS = (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    )

    @classmethod
    def _usage_dict_from_obj(cls, usage_obj: Any) -> dict[str, int]:
        if usage_obj is None:
            return {}
        usage: dict[str, int] = {}
        for field in cls._USAGE_FIELDS:
            value = getattr(usage_obj, field, None)
            if value is not None:
                usage[field] = int(value)
        return usage

    @classmethod
    def _usage_from_stream_event(cls, event: Any) -> dict[str, int] | None:
        usage_obj = None
        if event.type == "message_start":
            message = getattr(event, "message", None)
            usage_obj = getattr(message, "usage", None) if message is not None else None
        elif event.type == "message_delta":
            usage_obj = getattr(event, "usage", None)
        usage = cls._usage_dict_from_obj(usage_obj)
        return usage or None

    @staticmethod
    def _iter_stream_events(
        event: Any,
        current_tool_calls: dict[int, tuple[str, str]],
    ) -> list[StreamChunk]:
        chunks: list[StreamChunk] = []
        usage = AnthropicProvider._usage_from_stream_event(event)
        if usage:
            chunks.append(StreamChunk(usage=usage))
        if event.type == "content_block_start":
            block = event.content_block
            if getattr(block, "type", None) == "tool_use":
                tool_id = getattr(block, "id", "") or ""
                tool_name = getattr(block, "name", "") or ""
                current_tool_calls[event.index] = (tool_id, tool_name)
                chunks.append(
                    StreamChunk(delta_tool_calls=[ToolCallDelta(index=event.index, id=tool_id, name=tool_name)])
                )
        elif event.type == "content_block_delta":
            delta_type = getattr(event.delta, "type", None)
            if delta_type == "text_delta":
                chunks.append(StreamChunk(delta_content=event.delta.text))
            elif delta_type == "thinking_delta":
                thinking = getattr(event.delta, "thinking", "") or ""
                if thinking:
                    chunks.append(StreamChunk(delta_reasoning=thinking))
            elif delta_type == "input_json_delta":
                partial_json = getattr(event.delta, "partial_json", "") or ""
                tool_id, tool_name = current_tool_calls.get(event.index, ("", ""))
                chunks.append(
                    StreamChunk(
                        delta_tool_calls=[
                            ToolCallDelta(
                                index=event.index,
                                id=tool_id,
                                name=tool_name,
                                arguments_fragment=partial_json,
                            )
                        ]
                    )
                )
        elif event.type == "message_delta" and event.delta.stop_reason:
            chunks.append(StreamChunk(finish_reason=event.delta.stop_reason))
        return chunks

    def _block_to_dict(self, block: Any) -> dict[str, Any] | None:
        block_type = getattr(block, "type", None)
        if block_type == "thinking":
            thinking = getattr(block, "thinking", "") or ""
            block_dict: dict[str, Any] = {"type": "thinking", "thinking": thinking}
            # Real Anthropic extended-thinking blocks carry a signature; keep it when present.
            signature = getattr(block, "signature", None)
            if signature:
                block_dict["signature"] = signature
            return block_dict
        if block_type == "text":
            return {"type": "text", "text": getattr(block, "text", "") or ""}
        if block_type == "tool_use":
            return {
                "type": "tool_use",
                "id": getattr(block, "id", "") or "",
                "name": getattr(block, "name", "") or "",
                "input": getattr(block, "input", None) or {},
            }
        return None

    def _parse_response(self, response: AnthropicMessage) -> LLMResponse:
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        content_blocks: list[dict[str, Any]] = []
        preserve_blocks = preserves_anthropic_thinking_wire(self.base_url)

        if not response.content:
            return LLMResponse(
                content="",
                tool_calls=tool_calls,
                finish_reason=response.stop_reason or "stop",
                usage=self._usage_dict_from_obj(getattr(response, "usage", None)),
            )

        for block in response.content:
            if preserve_blocks:
                serialized = self._block_to_dict(block)
                if serialized is not None:
                    content_blocks.append(serialized)
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.id,
                        name=block.name,
                        arguments=block.input,
                    )
                )

        reasoning_content = _reasoning_from_blocks(content_blocks) if preserve_blocks else None

        return LLMResponse(
            content="".join(text_parts),
            tool_calls=tool_calls,
            finish_reason=response.stop_reason or "stop",
            usage=self._usage_dict_from_obj(getattr(response, "usage", None)),
            provider_content_blocks=content_blocks if preserve_blocks and content_blocks else None,
            reasoning_content=reasoning_content,
        )

    def _vendor_request_kwargs(self, *, model: str) -> dict[str, Any]:
        if is_kimi_anthropic_endpoint(self.base_url):
            return kimi_messages_kwargs(model)
        if not is_minimax_anthropic_endpoint(self.base_url):
            return {}
        return minimax_messages_kwargs(model)

    def _log_request_payload(
        self,
        *,
        model: str,
        system: str | list[dict[str, Any]] | None,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        max_tokens: int,
        temperature: float,
    ) -> None:
        truncated_messages = [truncate_dict(m) for m in messages]

        system_preview = system
        if isinstance(system, str) and len(system) > 200:
            system_preview = system[:200] + "..."
        elif isinstance(system, list):
            system_json = json.dumps(system, ensure_ascii=False)
            system_preview = system_json[:200] + "..." if len(system_json) > 200 else system_json

        payload = {
            "provider": self.name,
            "base_url": self.base_url,
            "model": model,
            "system": system_preview,
            "messages": truncated_messages,
            "tools": tools,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        logger.opt(lazy=True).debug(
            "Anthropic request payload:\n{payload}",
            payload=lambda: json.dumps(payload, ensure_ascii=False, indent=2),
        )

    async def complete(
        self,
        messages: list[Message],
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        tools: list[dict[str, Any]] | None = None,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        self._require_api_key()
        # 不捕获局部 client：abort() 会关闭并重建 self.client，在途重试必须
        # 每次调用时取当前 client，否则仍向已关闭的旧 client 发请求。
        system, converted_messages = self._convert_messages(messages, system_prompt, model=model or self.default_model)
        converted_tools = self._convert_tools(tools)
        resolved_model = model or self.default_model
        max_tokens = clamp_max_tokens_for_model(resolved_model, max_tokens, base_url=self.base_url, provider=self.name)

        self._log_request_payload(
            model=resolved_model,
            system=system,
            messages=converted_messages,
            tools=converted_tools,
            max_tokens=max_tokens,
            temperature=temperature,
        )

        async def _call() -> LLMResponse:
            request_kwargs = {
                "model": resolved_model,
                "messages": converted_messages,
                "system": system,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
            if converted_tools:
                request_kwargs["tools"] = converted_tools
            request_kwargs.update(self._vendor_request_kwargs(model=resolved_model))
            request_kwargs.update(kwargs)

            try:
                response = await self.ensure_client().messages.create(**request_kwargs)
                return self._parse_response(response)
            except ValueError as exc:
                if "Streaming is required" in str(exc):
                    return await _stream_aggregate()
                raise
            except anthropic.APIStatusError as exc:
                error_msg = str(exc).lower()
                if "tool" in error_msg or "function" in error_msg:
                    logger.error(
                        f"Anthropic API error related to tool calling: {exc}\n"
                        f"Model: {resolved_model}, Tools: {len(converted_tools) if converted_tools else 0}"
                    )
                if exc.status_code == 400 and "prompt is too long" in error_msg:
                    raise ContextWindowExceededError(f"Context window exceeded for model {resolved_model}") from exc
                # 透传 status_code：retry 的结构化分类与降级决策靠它走确定性路径，
                # 而不是退化到错误消息文案运气（403 额度 / 429 过载 / 401 认证各归各）。
                raise wrap_api_status_error(exc, prefix="Anthropic API error", status_code=exc.status_code) from exc
            except LLMError:
                raise
            except Exception as exc:
                raise wrap_unexpected_error(exc) from exc

        async def _stream_aggregate() -> LLMResponse:
            from src.llm.stream import collect_stream

            try:
                return await collect_stream(
                    self.stream_complete(
                        messages=messages,
                        model=model,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        tools=tools,
                        system_prompt=system_prompt,
                        **kwargs,
                    )
                )
            except LLMError as exc:
                cause = exc.__cause__
                if isinstance(cause, IndexError) or "list index out of range" in str(exc):
                    # 已产出 chunk 时禁止回退整 prompt 重发：provider 已计量，
                    # 重发会重复扣费；只有零 chunk（解析在首个事件即失败）才回退
                    if getattr(exc, "stream_chunks_seen", 0):
                        raise
                    # 互递归护栏：模型本就要求 streaming 时，回退 _call 会再抛
                    # "Streaming is required" 回到本分支，构成无界互递归——每轮
                    # 打一次真实 HTTP 请求直到 RecursionError。直接抛错终止。
                    if _requires_streaming_for_create(resolved_model, max_tokens):
                        raise
                    logger.info(f"Anthropic ({self.name}) stream parse failed; falling back to non-streaming create")
                    return await _call()
                raise

        use_compat_stream = self._is_compat_endpoint() and minimax_prefers_streaming_complete(
            resolved_model, max_tokens
        )
        use_sdk_parsed_stream = (
            _requires_streaming_for_create(resolved_model, max_tokens) and not self._is_compat_endpoint()
        )
        if use_compat_stream:
            return await retry_complete(
                _stream_aggregate,
                provider_name=self.name,
                max_retries=3,
                retry_if=_retry_only_if_stream_not_started,
            )
        if use_sdk_parsed_stream:
            return await retry_complete(
                _stream_aggregate,
                provider_name=self.name,
                max_retries=3,
                retry_if=_retry_only_if_stream_not_started,
            )

        return await retry_complete(
            _call,
            provider_name=self.name,
            max_retries=3,
        )

    async def stream_complete(
        self,
        messages: list[Message],
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        tools: list[dict[str, Any]] | None = None,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        self._require_api_key()
        # 同 complete()：不捕获局部 client，闭包内每次取 self.ensure_client()，
        # abort() 重建 client 后流初始化重试不会打到已关闭的旧 client。
        system, converted_messages = self._convert_messages(messages, system_prompt, model=model or self.default_model)
        converted_tools = self._convert_tools(tools)
        resolved_model = model or self.default_model
        max_tokens = clamp_max_tokens_for_model(resolved_model, max_tokens, base_url=self.base_url, provider=self.name)
        self._log_request_payload(
            model=resolved_model,
            system=system,
            messages=converted_messages,
            tools=converted_tools,
            max_tokens=max_tokens,
            temperature=temperature,
        )

        request_kwargs: dict[str, Any] = {
            "model": resolved_model,
            "messages": converted_messages,
            "system": system,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if converted_tools:
            request_kwargs["tools"] = converted_tools
        request_kwargs.update(self._vendor_request_kwargs(model=resolved_model))
        request_kwargs.update(kwargs)

        async def _init_parsed_stream():
            # messages.stream() is a lazy context manager — the HTTP request only
            # fires inside __aenter__. Enter it here so init failures (429/5xx)
            # stay inside the retried phase; mid-stream errors are not retried.
            stream_context = self.ensure_client().messages.stream(**request_kwargs)
            try:
                stream = await stream_context.__aenter__()
            except BaseException:
                # 重试放弃/预算耗尽等失败路径没有 finally 关口（成功路径由
                # 下方 finally 负责 __aexit__）：这里先 __aexit__ 释放已建立的
                # 连接，避免泄漏未进入消费循环的 stream context
                try:
                    await stream_context.__aexit__(*sys.exc_info())
                except Exception as close_exc:
                    logger.debug(f"Anthropic stream init close error: {close_exc}", exc_info=True)
                raise
            return stream_context, stream

        async def _init_raw_stream():
            return await self.ensure_client().messages.create(**request_kwargs, stream=True)

        try:
            current_tool_calls: dict[int, tuple[str, str]] = {}
            if self._is_compat_endpoint():
                stream = await retry_stream_init(
                    _init_raw_stream,
                    provider_name=self.name,
                    max_retries=3,
                )
                try:
                    async for event in stream:
                        for chunk in self._iter_stream_events(event, current_tool_calls):
                            yield chunk
                finally:
                    # Release the underlying HTTP connection even when the consumer aborts early.
                    try:
                        await stream.close()
                    except Exception as close_exc:
                        logger.debug(f"Anthropic stream close error: {close_exc}", exc_info=True)
                return

            stream_context, stream = await retry_stream_init(
                _init_parsed_stream,
                provider_name=self.name,
                max_retries=3,
            )
            try:
                async for event in stream:
                    for chunk in self._iter_stream_events(event, current_tool_calls):
                        yield chunk
            finally:
                # Same close semantics as `async with stream_context` (#34):
                # release the underlying HTTP connection even on early abort.
                try:
                    await stream_context.__aexit__(*sys.exc_info())
                except Exception as close_exc:
                    logger.debug(f"Anthropic stream close error: {close_exc}", exc_info=True)
        except anthropic.APIStatusError as exc:
            if exc.status_code == 400 and "prompt is too long" in str(exc).lower():
                raise ContextWindowExceededError(f"Context window exceeded for model {resolved_model}") from exc
            raise wrap_api_status_error(exc, prefix="Anthropic API error") from exc
        except anthropic.APIConnectionError as exc:
            # 带上端点：连接失败时「无法连接谁」比「Unexpected error」有用得多
            endpoint = getattr(self, "base_url", None) or "Anthropic 端点"
            raise LLMError(f"无法连接 {endpoint}：{exc}") from exc
        except Exception as exc:
            raise wrap_unexpected_error(exc) from exc

    def get_context_window(self, model: str | None = None) -> int:
        return ANTHROPIC_CONTEXT_WINDOWS.get(model or self.default_model, 200_000)
