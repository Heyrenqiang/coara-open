"""OpenAI Responses API driver（DeepSeek + 智谱 GLM Coding Plan / Codex 端点）。

DeepSeek（api-docs.deepseek.com/guides/responses_api）：
- deepseek-flash（V4.1 Flash）；无状态；tools 仅 function；reasoning.effort；原生多模态。

智谱（docs.bigmodel.cn Coding Plan / Codex）：
- base_url ``https://open.bigmodel.cn/api/v1``，wire_api=responses；
- 模型如 glm-5.3；思考用 ``reasoning.effort``（low/high/max，默认 max；关→low）。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import openai
from openai import AsyncOpenAI

from src.core.errors import APIKeyError, ContextWindowExceededError, LLMError
from src.core.logger import logger
from src.core.types import Message, MessageRole, ToolCall
from src.llm._http_provider import HTTPProviderMixin
from src.llm.call_defaults import clamp_max_tokens_for_model
from src.llm.deepseek_file_api import get_file_id
from src.llm.endpoints import is_deepseek_openai_endpoint, is_zhipu_openai_endpoint
from src.llm.message_content import openai_assistant_content
from src.llm.openai import OPENAI_CONTEXT_WINDOWS
from src.llm.provider import LLMProvider, LLMResponse, StreamChunk, ToolCallDelta
from src.llm.retry import retry_complete, retry_stream_init
from src.llm.tool_arguments import parse_tool_call_arguments, sanitize_tool_parameters
from src.llm.vendor_options import deepseek_responses_reasoning, zhipu_glm_responses_reasoning
from src.utils.text_utils import sanitize_json_payload, sanitize_surrogates

_DEFAULT_CONTEXT_WINDOW = 128_000

_IMAGE_PLACEHOLDER = "[图片内容：当前模型不支持图像输入，已省略]"

_CONTEXT_ERROR_MARKERS = ("context", "window", "length", "token")


async def _user_content_blocks(
    content: Any,
    *,
    vision: bool,
    file_api: bool = False,
    base_url: str | None = None,
    api_key: str | None = None,
) -> list[dict[str, Any]]:
    """coara 消息内容 → Responses input 块数组（user 消息）。
    视觉模型下图片转 input_image（data URL）；file_api 开启且上传成功时转
    DeepSeek file 块（file_id 引用）；否则退回文本占位。"""
    if not isinstance(content, list):
        return [{"type": "input_text", "text": str(content or "")}]
    blocks: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = str(block.get("text") or "")
            if text:
                blocks.append({"type": "input_text", "text": text})
        elif block.get("type") == "image":
            src = block.get("source") or {}
            if vision and src.get("type") == "base64":
                if file_api and base_url and api_key:
                    file_id = await get_file_id(base_url, api_key, str(src["data"]), str(src["media_type"]))
                    if file_id:
                        # Responses API 用 input_image（file_id 与 image_url 互斥）
                        blocks.append({"type": "input_image", "file_id": file_id})
                        continue
                blocks.append(
                    {
                        "type": "input_image",
                        "image_url": f"data:{src['media_type']};base64,{src['data']}",
                    }
                )
            else:
                blocks.append({"type": "input_text", "text": _IMAGE_PLACEHOLDER})
    return blocks or [{"type": "input_text", "text": ""}]


def _usage_from_responses(usage_obj: Any) -> dict[str, int]:
    """Normalize Responses API usage（cached_tokens / reasoning_tokens 细项）。"""
    if usage_obj is None:
        return {}
    usage: dict[str, int] = {}

    def _int(attr: str) -> int | None:
        value = getattr(usage_obj, attr, None)
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    input_tokens = _int("input_tokens")
    output_tokens = _int("output_tokens")
    total_tokens = _int("total_tokens")
    if input_tokens is not None:
        usage["input_tokens"] = input_tokens
    if output_tokens is not None:
        usage["output_tokens"] = output_tokens
    if total_tokens is not None:
        usage["total_tokens"] = total_tokens

    def _detail(obj: Any, field: str) -> int | None:
        if obj is None:
            return None
        value = obj.get(field) if isinstance(obj, dict) else getattr(obj, field, None)
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    cached = _detail(getattr(usage_obj, "input_tokens_details", None), "cached_tokens")
    if cached is not None:
        usage["cached_tokens"] = cached
    reasoning = _detail(getattr(usage_obj, "output_tokens_details", None), "reasoning_tokens")
    if reasoning is not None:
        usage["reasoning_tokens"] = reasoning
    return usage


def _content_text(content: Any) -> str:
    """Flatten coara message content (str or blocks) into plain text."""
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif block.get("type") == "image":
                parts.append(_IMAGE_PLACEHOLDER)
        return "\n".join(p for p in parts if p)
    return str(content or "")


class ResponsesProvider(HTTPProviderMixin, LLMProvider):
    """OpenAI Responses API Provider（DeepSeek V4 / 智谱 GLM）。"""

    _client_cls = AsyncOpenAI

    def __init__(
        self,
        name: str,
        api_key: str,
        base_url: str | None = None,
        default_model: str = "deepseek-flash",
        default_max_tokens: int | None = None,
        vision_model_ids: frozenset[str] | None = None,
    ):
        super().__init__(name, api_key, base_url, default_model, default_max_tokens)
        self._vision_model_ids = vision_model_ids
        from src.llm._http_provider import init_sdk_client

        self.client = init_sdk_client(AsyncOpenAI, api_key, base_url)
        self._closed = False
        self._abort_lock = None
        self._abort_task = None

    def _require_api_key(self) -> None:
        if not (self.api_key or "").strip():
            raise APIKeyError(f"API key is required for Responses provider '{self.name}'")

    # ------------------------------------------------------------------
    # Conversion: coara Message → Responses input items
    # ------------------------------------------------------------------

    async def _convert_input(self, messages: list[Message], *, model: str = "") -> list[dict[str, Any]]:
        from src.llm.orphan_repair import close_orphan_tool_calls
        from src.llm.vision import model_supports_vision

        # 发送前闭合孤儿 tool_call 配对（与 OpenAI/Anthropic 驱动同一共享逻辑）
        messages = close_orphan_tool_calls(messages)
        vision = model_supports_vision(
            model, provider_name=self.name, vision_model_ids=getattr(self, "_vision_model_ids", None)
        )
        use_file_api = vision and is_deepseek_openai_endpoint(self.base_url)
        items: list[dict[str, Any]] = []
        for msg in messages:
            if msg.role == MessageRole.SYSTEM:
                items.append({"type": "message", "role": "system", "content": _content_text(msg.content)})
            elif msg.role == MessageRole.USER:
                items.append(
                    {
                        "type": "message",
                        "role": "user",
                        "content": await _user_content_blocks(
                            msg.content,
                            vision=vision,
                            file_api=use_file_api,
                            base_url=self.base_url,
                            api_key=self.api_key,
                        ),
                    }
                )
            elif msg.role == MessageRole.ASSISTANT:
                if msg.reasoning_content:
                    items.append(
                        {
                            "type": "reasoning",
                            "content": [{"type": "reasoning_text", "text": msg.reasoning_content}],
                        }
                    )
                text = openai_assistant_content(msg.content)
                if text:
                    items.append(
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": text}],
                        }
                    )
                for tc in msg.tool_calls or []:
                    items.append(
                        {
                            "type": "function_call",
                            "call_id": tc.id,
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, default=str, ensure_ascii=False)
                            if isinstance(tc.arguments, dict)
                            else str(tc.arguments or "{}"),
                        }
                    )
            elif msg.role == MessageRole.TOOL_RESULT:
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": msg.tool_call_id,
                        "output": _content_text(msg.content) or "(empty)",
                    }
                )
        return items

    def _convert_tools(self, tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        if not tools:
            return None
        converted = []
        for tool in tools:
            converted.append(
                {
                    "type": "function",
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "parameters": sanitize_tool_parameters(tool),
                }
            )
        return converted

    def _reasoning_kwargs(self, model: str) -> dict[str, Any]:
        if is_deepseek_openai_endpoint(self.base_url):
            reasoning = deepseek_responses_reasoning(model)
            return {"reasoning": reasoning} if reasoning else {}
        if is_zhipu_openai_endpoint(self.base_url):
            reasoning = zhipu_glm_responses_reasoning(model)
            return {"reasoning": reasoning} if reasoning else {}
        return {}

    async def _build_request(
        self,
        messages: list[Message],
        *,
        model: str,
        max_tokens: int,
        temperature: float,
        tools: list[dict[str, Any]] | None,
        system_prompt: str | None,
        stream: bool,
        extra: dict[str, Any],
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model": model,
            "input": await self._convert_input(messages, model=model),
            "temperature": temperature,
            "max_output_tokens": clamp_max_tokens_for_model(
                model, max_tokens, base_url=self.base_url, provider=self.name
            ),
            "stream": stream,
        }
        if system_prompt:
            request["instructions"] = sanitize_surrogates(system_prompt)
        converted_tools = self._convert_tools(tools)
        if converted_tools:
            request["tools"] = converted_tools
            request["tool_choice"] = "auto"
        request.update(self._reasoning_kwargs(model))
        request.update(sanitize_json_payload(extra))
        return request

    @staticmethod
    def _map_api_error(exc: openai.APIStatusError, model: str) -> Exception:
        msg = str(exc).lower()
        if exc.status_code == 400 and any(marker in msg for marker in _CONTEXT_ERROR_MARKERS):
            return ContextWindowExceededError(f"Context window exceeded for model {model}")
        return exc

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_response(self, response: Any) -> LLMResponse:
        status = str(getattr(response, "status", "") or "")
        if status == "failed":
            error = getattr(response, "error", None)
            raise LLMError(f"Responses API request failed: {error}")
        content = str(getattr(response, "output_text", "") or "")
        tool_calls: list[ToolCall] = []
        reasoning_parts: list[str] = []
        for item in getattr(response, "output", None) or []:
            item_type = getattr(item, "type", "")
            if item_type == "function_call":
                tool_calls.append(
                    ToolCall(
                        id=str(getattr(item, "call_id", "") or getattr(item, "id", "")),
                        name=str(getattr(item, "name", "")),
                        arguments=parse_tool_call_arguments(str(getattr(item, "arguments", "") or "")),
                    )
                )
            elif item_type == "reasoning":
                for part in getattr(item, "content", None) or []:
                    text = getattr(part, "text", None)
                    if text:
                        reasoning_parts.append(str(text))
        finish = "stop" if status == "completed" else ("length" if status == "incomplete" else "stop")
        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish,
            usage=_usage_from_responses(getattr(response, "usage", None)),
            reasoning_content="".join(reasoning_parts).strip() or None,
        )

    # ------------------------------------------------------------------
    # LLMProvider interface
    # ------------------------------------------------------------------

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
        resolved_model = model or self.default_model
        # 不捕获局部 client：abort() 会关闭并重建 self.client，在途重试必须
        # 每次调用时取当前 client，否则仍向已关闭的旧 client 发请求。
        request = await self._build_request(
            messages,
            model=resolved_model,
            max_tokens=max_tokens,
            temperature=temperature,
            tools=tools,
            system_prompt=system_prompt,
            stream=False,
            extra=kwargs,
        )

        async def _call() -> LLMResponse:
            try:
                response = await self.ensure_client().responses.create(**request)
                return self._parse_response(response)
            except openai.APIStatusError as exc:
                raise self._map_api_error(exc, resolved_model) from exc

        return await retry_complete(_call, provider_name=self.name, max_retries=3)

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
        resolved_model = model or self.default_model
        # 同 complete()：不捕获局部 client，闭包内每次取 self.ensure_client()，
        # abort() 重建 client 后流初始化重试不会打到已关闭的旧 client。
        request = await self._build_request(
            messages,
            model=resolved_model,
            max_tokens=max_tokens,
            temperature=temperature,
            tools=tools,
            system_prompt=system_prompt,
            stream=True,
            extra=kwargs,
        )

        async def _init_stream():
            try:
                return await self.ensure_client().responses.create(**request)
            except openai.APIStatusError as exc:
                raise self._map_api_error(exc, resolved_model) from exc

        stream = await retry_stream_init(_init_stream, provider_name=self.name, max_retries=3)

        try:
            async for event in stream:
                event_type = str(getattr(event, "type", "") or "")
                if event_type == "response.output_text.delta":
                    yield StreamChunk(delta_content=str(getattr(event, "delta", "") or ""))
                elif event_type == "response.reasoning_text.delta":
                    yield StreamChunk(delta_reasoning=str(getattr(event, "delta", "") or ""))
                elif event_type == "response.output_item.added":
                    item = getattr(event, "item", None)
                    if item is not None and getattr(item, "type", "") == "function_call":
                        yield StreamChunk(
                            delta_tool_calls=[
                                ToolCallDelta(
                                    index=int(getattr(event, "output_index", 0) or 0),
                                    id=str(getattr(item, "call_id", "") or getattr(item, "id", "")),
                                    name=str(getattr(item, "name", "")),
                                )
                            ]
                        )
                elif event_type == "response.function_call_arguments.delta":
                    yield StreamChunk(
                        delta_tool_calls=[
                            ToolCallDelta(
                                index=int(getattr(event, "output_index", 0) or 0),
                                arguments_fragment=str(getattr(event, "delta", "") or ""),
                            )
                        ]
                    )
                elif event_type in ("response.completed", "response.incomplete"):
                    final = getattr(event, "response", None)
                    usage = _usage_from_responses(getattr(final, "usage", None))
                    finish = "stop" if event_type == "response.completed" else "length"
                    yield StreamChunk(finish_reason=finish, usage=usage)
                elif event_type == "response.failed":
                    error = getattr(getattr(event, "response", None), "error", None)
                    raise LLMError(f"Responses API stream failed: {error}")
        finally:
            # 对齐 openai/anthropic provider：消费方提前中断或中途异常时也要
            # 释放底层 HTTP 连接，避免流泄漏。
            try:
                await stream.close()
            except Exception as close_exc:
                logger.debug(f"Responses stream close error: {close_exc}")

    def get_context_window(self, model: str | None = None) -> int:
        model_key = (model or self.default_model or "").lower()
        if model_key in OPENAI_CONTEXT_WINDOWS:
            return OPENAI_CONTEXT_WINDOWS[model_key]
        return _DEFAULT_CONTEXT_WINDOW
