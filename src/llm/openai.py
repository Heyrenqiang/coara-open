"""
Coara v8 - OpenAI Provider

实现 OpenAI API（GPT 系列）的调用。
"""

# mypy: ignore-errors

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import openai
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion

from src.core.errors import APIKeyError, ContextWindowExceededError, LLMError
from src.core.logger import logger
from src.core.types import Message, MessageRole, ToolCall
from src.llm._http_provider import HTTPProviderMixin
from src.llm.call_defaults import clamp_max_tokens_for_model
from src.llm.endpoints import (
    is_agnes_openai_endpoint,
    is_kimi_openai_endpoint,
    is_mimo_openai_endpoint,
    is_zhipu_openai_endpoint,
)
from src.llm.message_content import openai_assistant_content
from src.llm.orphan_repair import close_orphan_tool_calls
from src.llm.provider import LLMProvider, LLMResponse, StreamChunk, ToolCallDelta
from src.llm.retry import retry_complete, retry_stream_init
from src.llm.tool_arguments import parse_tool_call_arguments, sanitize_tool_parameters
from src.llm.usage import usage_dict_from_openai
from src.llm.vendor_options import (
    agnes_chat_extra_body,
    kimi_openai_chat_kwargs,
    zhipu_glm_chat_kwargs,
)
from src.utils.text_utils import sanitize_json_payload, sanitize_surrogates

# OpenAI 模型上下文窗口映射（≠ max_tokens 输出上限）
OPENAI_CONTEXT_WINDOWS = {
    # MiMo-V2.5(-Pro) API/instruct: 1M context (Base variant is 256K).
    "mimo-v2.5-pro": 1_048_576,
    "mimo-v2.5": 1_048_576,
    # DeepSeek Flash（官方：1M context / 384K max output）
    "deepseek-flash": 1_000_000,
    # Agnes Flash (OpenAI-compatible; 2.5 official doc: 512K context, 65.5K max output).
    "agnes-2.5-flash": 512_000,
    "glm-5.3": 1_000_000,
    "glm-5.2": 1_000_000,
    "glm-5": 200_000,
    # Kimi Code OpenAI 端 K3 系列
    "k3": 1_048_576,
    "k3-256k": 262_144,
    "kimi-for-coding": 262_144,
    "kimi-for-coding-highspeed": 262_144,
    "glm-5-turbo": 200_000,
}


def _openai_tool_calls(tool_calls: list[ToolCall]) -> list[dict[str, Any]]:
    return [
        {
            "id": tc.id,
            "type": "function",
            "function": {
                "name": tc.name,
                "arguments": json.dumps(tc.arguments, default=str) if isinstance(tc.arguments, dict) else tc.arguments,
            },
        }
        for tc in tool_calls
    ]


class OpenAIProvider(HTTPProviderMixin, LLMProvider):
    """OpenAI Provider 实现"""

    _client_cls = AsyncOpenAI

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
        # 显式声明支持图像的模型集合（来自 providers.yaml models.available 的 vision:true），
        # 供转换层判断是否透传图片；None 时仅走白名单/通用 marker。
        self._vision_model_ids = vision_model_ids

        # Empty key is allowed so CLI/UI can start; complete()/stream_complete() raise APIKeyError.
        from src.llm._http_provider import init_sdk_client

        self.client = init_sdk_client(AsyncOpenAI, api_key, base_url)
        self._closed = False
        self._abort_lock = asyncio.Lock()
        self._abort_task: asyncio.Task | None = None

    def _require_api_key(self) -> None:
        if not (self.api_key or "").strip():
            raise APIKeyError(f"API key is required for OpenAI provider '{self.name}'")

    def _dynamic_tool_loading_enabled(self) -> bool:
        """K3 dynamic tool loading：仅 Kimi Code OpenAI 端 + k3 模型支持（官方限制）。

        其它模型走该端点发声明消息会 tokenization failed，故按当前默认模型判定。
        """
        return is_kimi_openai_endpoint(self.base_url) and ((self.default_model or "").lower().startswith("k3"))

    @staticmethod
    def _close_orphan_tool_calls(messages: list[Message]) -> list[Message]:
        """转换前闭合双向孤儿配对（只作用于请求负载，不改 message_history）。

        委托 provider 无关的共享实现（src/llm/orphan_repair.py），
        Anthropic / Responses 驱动走同一逻辑。
        """
        return close_orphan_tool_calls(messages)

    def _convert_messages(self, messages: list[Message], *, model: str = "") -> list[dict[str, Any]]:
        """转换消息为 OpenAI 格式（入口先闭合孤儿 tool_call 配对，防历史污染 400）。"""
        from src.llm.vision import IMAGE_OMITTED_PLACEHOLDER, model_supports_vision

        vision = model_supports_vision(
            model, provider_name=self.name, vision_model_ids=self._vision_model_ids
        )
        messages = self._close_orphan_tool_calls(messages)
        converted = []

        for msg in messages:
            if msg.role == MessageRole.SYSTEM:
                converted.append({"role": "system", "content": msg.content})
            elif msg.role == MessageRole.USER:
                if isinstance(msg.content, str):
                    decl = None
                    if self._dynamic_tool_loading_enabled():
                        from src.core.message_tags import strip_tool_declaration

                        decl = strip_tool_declaration(msg.content)
                    if decl is not None:
                        try:
                            payload = json.loads(decl)
                            converted.append({"role": "system", "tools": payload["tools"]})
                            continue
                        except (json.JSONDecodeError, KeyError, TypeError):
                            pass  # 非法声明按普通文本透传，不炸请求
                    converted.append({"role": "user", "content": msg.content})
                elif isinstance(msg.content, list):
                    user_content: list[dict[str, Any]] = []
                    for block in msg.content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "text":
                            user_content.append({"type": "text", "text": str(block.get("text", ""))})
                        elif block.get("type") == "image":
                            src = block.get("source", {})
                            if vision and src.get("type") == "base64":
                                user_content.append(
                                    {
                                        "type": "image_url",
                                        "image_url": {
                                            "url": f"data:{src['media_type']};base64,{src['data']}",
                                        },
                                    }
                                )
                            else:
                                user_content.append(
                                    {"type": "text", "text": IMAGE_OMITTED_PLACEHOLDER}
                                )
                    converted.append({"role": "user", "content": user_content or [{"type": "text", "text": ""}]})
                else:
                    converted.append({"role": "user", "content": msg.content})
            elif msg.role == MessageRole.ASSISTANT:
                assistant_content = openai_assistant_content(msg.content)
                msg_dict: dict[str, Any] = {
                    "role": "assistant",
                    # OpenAI spec: content may be null when the assistant only issues tool calls.
                    "content": None if (msg.tool_calls and not assistant_content) else assistant_content,
                }
                # MiMo / 智谱：reasoning_content 必须随 tool-call 回合回传
                # （GLM 思考链同理；DeepSeek 走 Responses 驱动，不在此路径）。
                if (
                    is_mimo_openai_endpoint(self.base_url) or is_zhipu_openai_endpoint(self.base_url)
                ) and msg.reasoning_content:
                    msg_dict["reasoning_content"] = msg.reasoning_content
                if msg.tool_calls:
                    msg_dict["tool_calls"] = _openai_tool_calls(msg.tool_calls)
                converted.append(msg_dict)
            elif msg.role == MessageRole.TOOL_RESULT:
                # Support multimodal content blocks in tool results.
                # OpenAI tool role does not support image content blocks,
                # so we extract images and inject them as a separate user message.
                if isinstance(msg.content, list):
                    text_parts: list[str] = []
                    image_blocks: list[dict[str, Any]] = []
                    for block in msg.content:
                        if isinstance(block, dict):
                            if block.get("type") == "text":
                                text_parts.append(block["text"])
                            elif block.get("type") == "image":
                                image_blocks.append(block)

                    # Text parts go into the tool result
                    tool_text = "\n".join(text_parts) if text_parts else str(msg.content)
                    converted.append(
                        {
                            "role": "tool",
                            "tool_call_id": msg.tool_call_id,
                            "content": tool_text,
                        }
                    )

                    # Image parts become a separate user message (OpenAI vision format)
                    if image_blocks:
                        user_content: list[dict[str, Any]] = [{"type": "text", "text": "[Attached images]"}]
                        for img in image_blocks:
                            src = img.get("source", {})
                            if vision and src.get("type") == "base64":
                                user_content.append(
                                    {
                                        "type": "image_url",
                                        "image_url": {"url": f"data:{src['media_type']};base64,{src['data']}"},
                                    }
                                )
                            else:
                                user_content.append(
                                    {"type": "text", "text": IMAGE_OMITTED_PLACEHOLDER}
                                )
                        converted.append({"role": "user", "content": user_content})
                else:
                    converted.append(
                        {
                            "role": "tool",
                            "tool_call_id": msg.tool_call_id,
                            "content": str(msg.content),
                        }
                    )

        return sanitize_json_payload(converted)

    def _convert_tools(self, tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        """转换工具定义为 OpenAI 格式，带验证和清理。"""
        if not tools:
            return None

        converted = []
        for tool in tools:
            name = tool.get("name", "")
            description = tool.get("description", "")
            parameters = sanitize_tool_parameters(tool)

            converted.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": description,
                        "parameters": parameters,
                    },
                }
            )

        logger.debug(f"Converted {len(converted)} tools for OpenAI API")
        return converted

    def _apply_token_limit_kwargs(self, request_kwargs: dict[str, Any], max_tokens: int) -> None:
        model = str(request_kwargs.get("model") or self.default_model or "")
        capped = clamp_max_tokens_for_model(model, max_tokens, base_url=self.base_url, provider=self.name)
        request_kwargs["max_tokens"] = capped

    def _merge_extra_body(self, request_kwargs: dict[str, Any], extra: dict[str, Any] | None) -> None:
        if not extra:
            return
        existing = request_kwargs.get("extra_body")
        if isinstance(existing, dict):
            existing.update(extra)
        else:
            request_kwargs["extra_body"] = extra

    def _apply_vendor_extra_body(
        self,
        request_kwargs: dict[str, Any],
        *,
        model: str,
    ) -> None:
        if is_agnes_openai_endpoint(self.base_url):
            self._merge_extra_body(request_kwargs, agnes_chat_extra_body(model))
        if is_zhipu_openai_endpoint(self.base_url):
            extras = zhipu_glm_chat_kwargs(model)
            extra_body = extras.pop("extra_body", None)
            self._merge_extra_body(request_kwargs, extra_body)
            request_kwargs.update(extras)
        if is_kimi_openai_endpoint(self.base_url):
            extras = kimi_openai_chat_kwargs(model)
            extra_body = extras.pop("extra_body", None)
            self._merge_extra_body(request_kwargs, extra_body)
            request_kwargs.update(extras)

    def _parse_response(self, response: ChatCompletion) -> LLMResponse:
        """解析 OpenAI 响应"""
        if not response.choices:
            raise LLMError("Empty response from OpenAI: no choices returned")
        choice = response.choices[0]
        message = choice.message

        content = message.content or ""
        tool_calls = []

        if message.tool_calls:
            for tc in message.tool_calls:
                raw_args = tc.function.arguments
                if isinstance(raw_args, str):
                    arguments = parse_tool_call_arguments(raw_args)
                else:
                    arguments = {"_raw": str(raw_args)}

                tool_calls.append(
                    ToolCall(
                        id=tc.id,
                        name=tc.function.name,
                        arguments=arguments,
                    )
                )

        raw_reasoning = getattr(message, "reasoning_content", None)
        reasoning_content = str(raw_reasoning).strip() if raw_reasoning else None

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
            usage=usage_dict_from_openai(getattr(response, "usage", None)),
            reasoning_content=reasoning_content or None,
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
        """单次完成调用，带精细化重试机制。"""
        self._require_api_key()
        converted_messages = self._convert_messages(messages, model=model or self.default_model)
        # 不捕获局部 client：abort() 会关闭并重建 self.client，在途重试必须
        # 每次调用时取当前 client，否则仍向已关闭的旧 client 发请求。

        # 如果有单独的系统提示，插入到消息列表开头
        if system_prompt:
            converted_messages.insert(0, {"role": "system", "content": sanitize_surrogates(system_prompt)})

        converted_tools = self._convert_tools(tools)
        resolved_model = model or self.default_model

        async def _call() -> LLMResponse:
            request_kwargs = {
                "model": resolved_model,
                "messages": converted_messages,
                "temperature": temperature,
            }
            self._apply_token_limit_kwargs(request_kwargs, max_tokens)
            if converted_tools:
                request_kwargs["tools"] = converted_tools
            self._apply_vendor_extra_body(
                request_kwargs,
                model=resolved_model,
            )
            request_kwargs.update(sanitize_json_payload(kwargs))

            try:
                response = await self.ensure_client().chat.completions.create(**request_kwargs)
                return self._parse_response(response)
            except openai.APIStatusError as exc:
                error_msg = str(exc).lower()
                if "tool" in error_msg or "function" in error_msg:
                    logger.error(
                        f"OpenAI API error related to tool calling: {exc}\n"
                        f"Model: {resolved_model}, Tools: {len(converted_tools) if converted_tools else 0}"
                    )
                if "maximum context length" in error_msg:
                    raise ContextWindowExceededError(f"Context window exceeded for model {resolved_model}") from exc
                raise

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
        converted_messages = self._convert_messages(messages, model=model or self.default_model)
        # 同 complete()：不捕获局部 client，闭包内每次取 self.ensure_client()，
        # abort() 重建 client 后流初始化重试不会打到已关闭的旧 client。

        if system_prompt:
            converted_messages.insert(0, {"role": "system", "content": sanitize_surrogates(system_prompt)})

        async def _init_stream():
            stream_kwargs: dict[str, Any] = {
                "model": model or self.default_model,
                "messages": converted_messages,
                "temperature": temperature,
                "stream": True,
                "stream_options": {"include_usage": True},
            }
            self._apply_token_limit_kwargs(stream_kwargs, max_tokens)
            converted_tools = self._convert_tools(tools)
            # 与 complete() 对齐：无工具时不置 tools 键——显式 tools:null 会被
            # 部分 OpenAI 兼容端点（严格 schema 校验）拒绝为 400
            if converted_tools:
                stream_kwargs["tools"] = converted_tools
            self._apply_vendor_extra_body(
                stream_kwargs,
                model=stream_kwargs["model"],
            )
            stream_kwargs.update(sanitize_json_payload(kwargs))
            try:
                return await self.ensure_client().chat.completions.create(**stream_kwargs)
            except openai.APIStatusError as exc:
                # Some OpenAI-compatible endpoints reject stream_options entirely;
                # degrade once by dropping it (never disabled by default — MiMo
                # usage stats depend on include_usage).
                if (
                    exc.status_code == 400
                    and "stream_options" in str(exc).lower()
                    and "stream_options" in stream_kwargs
                ):
                    logger.info(f"OpenAI ({self.name}) endpoint rejected stream_options; retrying without it")
                    stream_kwargs.pop("stream_options", None)
                    return await self.ensure_client().chat.completions.create(**stream_kwargs)
                raise

        try:
            stream = await retry_stream_init(
                _init_stream,
                provider_name=self.name,
                max_retries=3,
            )

            current_tool_calls: dict[int, tuple[str, str]] = {}

            try:
                async for chunk in stream:
                    # OpenAI stream_options.include_usage 的最终 usage chunk
                    # 按 spec 携带 usage、choices 为空：先取 usage 再跳过空
                    # choices，否则计费/上下文记账永远收不到流式 usage。
                    usage = usage_dict_from_openai(getattr(chunk, "usage", None))
                    if usage:
                        yield StreamChunk(usage=usage)

                    if not chunk.choices:
                        continue

                    delta = chunk.choices[0].delta

                    # reasoning_content：MiMo / 智谱 / Agnes 以该字段流式下发思考。
                    # 主会话恒走流式，此前只读 delta.content → 思考链整段丢失（不进
                    # UI、不入 history），且下回合 openai.py:188 依赖 msg.reasoning_content
                    # 回传这些端点（缺它部分端点 400）。非流式路径 _parse_response 已收集，
                    # 这里把流式补上（StreamAggregator / Responses driver 同款读取）。
                    reasoning = getattr(delta, "reasoning_content", None)
                    if reasoning:
                        yield StreamChunk(delta_reasoning=reasoning)

                    if delta.content:
                        yield StreamChunk(delta_content=delta.content)

                    if delta.tool_calls:
                        deltas = []
                        for tc_delta in delta.tool_calls:
                            idx = tc_delta.index
                            if tc_delta.id:
                                tc_name = ""
                                if tc_delta.function and tc_delta.function.name:
                                    tc_name = tc_delta.function.name
                                current_tool_calls[idx] = (tc_delta.id, tc_name)

                            tool_id, tool_name = current_tool_calls.get(idx, ("", ""))
                            if tc_delta.function and tc_delta.function.name:
                                tool_name = tc_delta.function.name
                                current_tool_calls[idx] = (tool_id, tool_name)

                            arguments_fragment = ""
                            if tc_delta.function and tc_delta.function.arguments:
                                arguments_fragment = tc_delta.function.arguments

                            deltas.append(
                                ToolCallDelta(
                                    index=idx,
                                    id=tool_id,
                                    name=tool_name,
                                    arguments_fragment=arguments_fragment,
                                )
                            )
                        yield StreamChunk(delta_tool_calls=deltas)

                    if chunk.choices and chunk.choices[0].finish_reason:
                        yield StreamChunk(finish_reason=chunk.choices[0].finish_reason)
            finally:
                # Always release the underlying HTTP connection, whether the
                # consumer iterated to completion, broke early, or an
                # exception fired mid-stream.
                try:
                    await stream.close()
                except Exception as close_exc:
                    logger.debug(f"OpenAI stream close error: {close_exc}")

        except openai.APIStatusError as e:
            if "maximum context length" in str(e).lower():
                raise ContextWindowExceededError(
                    f"Context window exceeded for model {model or self.default_model}"
                ) from e
            raise LLMError(f"OpenAI API error: {e}") from e
        except Exception as e:
            raise LLMError(f"Unexpected error: {e}") from e

    def get_context_window(self, model: str | None = None) -> int:
        """获取上下文窗口大小（与 max_tokens 输出上限无关）。"""
        model_name = model or self.default_model
        # Coding Plan / Claude Code 可能带后缀，如 glm-5.3[1m]
        base = (model_name or "").split("[", 1)[0].strip()
        if base in OPENAI_CONTEXT_WINDOWS:
            return OPENAI_CONTEXT_WINDOWS[base]
        return OPENAI_CONTEXT_WINDOWS.get(model_name, 128_000)
