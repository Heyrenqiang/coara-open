"""wdl 节点执行器 — agentic 工具循环：节点执行 = LLM ↔ 内置工具多轮往返。

httpx 直连 provider（openai 兼容 chat/completions 或 anthropic 兼容
messages）。每轮发起 LLM 调用：响应含 tool_calls 则执行内置工具
（``wdl.tools``，工作目录为根的路径约束）、把结果回注消息序列，再调
LLM，直到无 tool_calls（返回终稿）或达 ``max_tool_rounds`` 上限（返回
已有的最后文本，不报错）。节点未声明 tools = 无工具单回合（向后兼容）。

``LLMNodeExecutor.__call__`` 签名与 kernel_runner 注入的 node_executor
一致（node_id/prompt/task → str）；on_event 回调透出 tool_start /
tool_result 节点事件。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from wdl.errors import WDLError
from wdl.logging import logger
from wdl.providers import (
    ProviderConfig,
    ProvidersConfig,
    ToolSettings,
    load_providers_config,
    merge_node_llm,
    resolve_node_llm,
)
from wdl.tools import DEFAULT_SHELL_TIMEOUT, ToolError, ToolSpec, build_tools, run_tool

DEFAULT_MAX_TOOL_ROUNDS = 20

EventHook = Callable[[str, dict[str, Any]], Awaitable[None]]


def _summarize(value: Any, limit: int = 120) -> str:
    """事件摘要：压平空白并截断。"""
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class LLMNodeExecutor:
    """agentic 工具循环 LLM 节点执行器。

    graph_llm: 图级 (provider, model) 覆盖（空串=跟随配置文件默认）。
    node_llms: node_id → (provider, model) 节点级覆盖。
    node_tools: node_id → tools 白名单（None=无工具，[]=全量内置）。
    tool_settings: 图级工具循环参数（WDL settings > providers.yaml settings > 默认）。
    workdir: 内置工具的工作目录根（默认当前工作目录）。
    """

    def __init__(
        self,
        config: ProvidersConfig | None = None,
        *,
        graph_llm: tuple[str, str] = ("", ""),
        node_llms: dict[str, tuple[str, str]] | None = None,
        node_tools: dict[str, list[str] | None] | None = None,
        tool_settings: ToolSettings | None = None,
        workdir: str | Path | None = None,
        client: httpx.AsyncClient | None = None,
        on_event: EventHook | None = None,
    ) -> None:
        self._config = config if config is not None else load_providers_config()
        self._graph_llm = graph_llm
        self._node_llms = node_llms or {}
        self._node_tools = node_tools or {}
        self._tool_settings = tool_settings or ToolSettings()
        self._workdir = Path(workdir).resolve() if workdir is not None else Path.cwd().resolve()
        self._client = client
        self._owns_client = client is None
        self._on_event = on_event

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient()
            self._owns_client = True
        return self._client

    def resolve_for_node(self, node_id: str) -> tuple[str, ProviderConfig, str]:
        """节点 LLM 解析：节点级 > 图级 > 配置文件默认。"""
        node_p, node_m = self._node_llms.get(node_id, ("", ""))
        explicit_p, explicit_m = merge_node_llm(node_p, node_m, *self._graph_llm)
        return resolve_node_llm(self._config, explicit_p, explicit_m)

    def _settings(self) -> tuple[int, float]:
        """(max_tool_rounds, shell_timeout)：executor（图级）> providers.yaml > 默认。"""
        s = self._config.settings
        rounds = self._tool_settings.max_tool_rounds or s.max_tool_rounds or DEFAULT_MAX_TOOL_ROUNDS
        timeout = self._tool_settings.shell_timeout or s.shell_timeout or DEFAULT_SHELL_TIMEOUT
        return rounds, timeout

    async def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._on_event is None:
            return
        try:
            await self._on_event(event_type, payload)
        except Exception as exc:  # noqa: BLE001 — 事件回调不得炸穿工具循环
            logger.warning(f"tool event hook failed ({event_type}): {exc}")

    async def __call__(self, *, node_id: str, prompt: str, task: str) -> str:
        provider_name, cfg, model = self.resolve_for_node(node_id)
        logger.debug(f"node {node_id} → {provider_name}/{model}")
        if cfg.type == "anthropic":
            return await self._run_loop(node_id, cfg, model, prompt, family="anthropic")
        if cfg.type == "openai":
            return await self._run_loop(node_id, cfg, model, prompt, family="openai")
        raise WDLError(f"provider {provider_name} type 非法：{cfg.type}（openai | anthropic）")

    async def _run_loop(self, node_id: str, cfg: ProviderConfig, model: str, prompt: str, *, family: str) -> str:
        whitelist = self._node_tools.get(node_id)
        if whitelist is None:
            # 节点未声明 tools：无工具单回合（向后兼容旧行为）
            return await self._single_call(cfg, model, prompt, family=family)
        max_rounds, shell_timeout = self._settings()
        try:
            specs = build_tools(self._workdir, shell_timeout, names=whitelist or None)
        except ToolError as exc:
            raise WDLError(str(exc)) from exc
        loop = (
            _OpenAIToolLoop(self, node_id, cfg, model, specs)
            if family == "openai"
            else _AnthropicToolLoop(self, node_id, cfg, model, specs)
        )
        return await loop.run(prompt, max_rounds)

    async def _single_call(self, cfg: ProviderConfig, model: str, prompt: str, *, family: str) -> str:
        """无工具单回合调用（节点未声明 tools 时的旧行为）。"""
        if family == "openai":
            data = await self._post_openai(cfg, model, [{"role": "user", "content": prompt}], tools=None)
            return _openai_message_text(data["choices"][0]["message"])
        data = await self._post_anthropic(cfg, model, [{"role": "user", "content": prompt}], tools=None)
        return _anthropic_text(data)

    # ── HTTP ────────────────────────────────────────────────────────────

    async def _post_openai(
        self, cfg: ProviderConfig, model: str, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None
    ) -> dict[str, Any]:
        url = cfg.base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {cfg.resolved_api_key()}"}
        payload: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": cfg.max_tokens}
        if tools:
            payload["tools"] = tools
        resp = await self._get_client().post(url, headers=headers, json=payload, timeout=cfg.timeout)
        if resp.status_code != 200:
            raise WDLError(f"openai 兼容接口 HTTP {resp.status_code}：{resp.text[:500]}")
        data = resp.json()
        if not isinstance(data, dict) or not data.get("choices"):
            raise WDLError(f"openai 兼容接口响应缺少 choices：{str(data)[:500]}")
        return data

    async def _post_anthropic(
        self, cfg: ProviderConfig, model: str, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None
    ) -> dict[str, Any]:
        url = cfg.base_url.rstrip("/") + "/v1/messages"
        headers = {
            "x-api-key": cfg.resolved_api_key(),
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload: dict[str, Any] = {"model": model, "max_tokens": cfg.max_tokens, "messages": messages}
        if tools:
            payload["tools"] = tools
        resp = await self._get_client().post(url, headers=headers, json=payload, timeout=cfg.timeout)
        if resp.status_code != 200:
            raise WDLError(f"anthropic 兼容接口 HTTP {resp.status_code}：{resp.text[:500]}")
        data = resp.json()
        if not isinstance(data, dict) or "content" not in data:
            raise WDLError(f"anthropic 兼容接口响应缺少 content：{str(data)[:500]}")
        return data


# ── 响应解析工具函数 ─────────────────────────────────────────────────────


def _openai_message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, list):  # 少数实现返回分块数组
        return "".join(str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in content)
    return str(content or "")


def _anthropic_text(data: dict[str, Any]) -> str:
    blocks = data.get("content")
    if not isinstance(blocks, list):
        raise WDLError(f"anthropic 兼容接口 content 不是数组：{str(data)[:500]}")
    return "".join(str(b.get("text", "")) for b in blocks if isinstance(b, dict) and b.get("type") == "text")


# ── 工具循环（两协议各一） ───────────────────────────────────────────────


class _OpenAIToolLoop:
    """openai 兼容工具循环：tools + assistant.tool_calls + role=tool 消息。"""

    def __init__(
        self, ex: LLMNodeExecutor, node_id: str, cfg: ProviderConfig, model: str, specs: list[ToolSpec]
    ) -> None:
        self._ex, self._node_id, self._cfg, self._model = ex, node_id, cfg, model
        self._specs = {s.name: s for s in specs}

    async def run(self, prompt: str, max_rounds: int) -> str:
        tools = [
            {"type": "function", "function": {"name": s.name, "description": "", "parameters": s.schema}}
            for s in self._specs.values()
        ]
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        last_text = ""
        for _round in range(max_rounds):
            data = await self._ex._post_openai(self._cfg, self._model, messages, tools=tools)
            message = data["choices"][0].get("message") or {}
            last_text = _openai_message_text(message) or last_text
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                return last_text
            messages.append(message)
            for call in tool_calls:
                # openai 侧 tool 消息无 is_error 字段，错误以 [工具错误] 前缀文本表达
                result, _is_error = await self._exec(call)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(call.get("id") or ""),
                        "content": result,
                    }
                )
        logger.warning(f"node {self._node_id} 达到工具循环上限 {max_rounds} 轮，返回已有文本")
        return last_text

    async def _exec(self, call: dict[str, Any]) -> tuple[str, bool]:
        function = call.get("function") or {}
        name = str(function.get("name") or "")
        raw_args = function.get("arguments") or "{}"
        await self._ex._emit(
            "tool_start", {"node_id": self._node_id, "tool": name, "arguments_summary": _summarize(raw_args)}
        )
        spec = self._specs.get(name)
        if spec is None:
            result, ok = f"[工具错误] 未声明的工具：{name!r}", False
        else:
            try:
                arguments = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except (json.JSONDecodeError, TypeError, ValueError):
                result, ok = f"[工具错误] 参数不是合法 JSON：{_summarize(raw_args)}", False
            else:
                result, ok = await run_tool(spec, arguments)
        await self._ex._emit(
            "tool_result", {"node_id": self._node_id, "tool": name, "ok": ok, "result_summary": _summarize(result)}
        )
        return result, ok


class _AnthropicToolLoop:
    """anthropic 兼容工具循环：tools + tool_use / tool_result content blocks。"""

    def __init__(
        self, ex: LLMNodeExecutor, node_id: str, cfg: ProviderConfig, model: str, specs: list[ToolSpec]
    ) -> None:
        self._ex, self._node_id, self._cfg, self._model = ex, node_id, cfg, model
        self._specs = {s.name: s for s in specs}

    async def run(self, prompt: str, max_rounds: int) -> str:
        tools = [{"name": s.name, "description": "", "input_schema": s.schema} for s in self._specs.values()]
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        last_text = ""
        for _round in range(max_rounds):
            data = await self._ex._post_anthropic(self._cfg, self._model, messages, tools=tools)
            blocks = data.get("content") or []
            last_text = _anthropic_text(data) or last_text
            tool_uses = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"]
            if not tool_uses:
                return last_text
            messages.append({"role": "assistant", "content": blocks})
            results: list[dict[str, Any]] = []
            for block in tool_uses:
                result, is_error = await self._exec(block)
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": str(block.get("id") or ""),
                        "content": result,
                        "is_error": is_error,
                    }
                )
            messages.append({"role": "user", "content": results})
        logger.warning(f"node {self._node_id} 达到工具循环上限 {max_rounds} 轮，返回已有文本")
        return last_text

    async def _exec(self, block: dict[str, Any]) -> tuple[str, bool]:
        name = str(block.get("name") or "")
        arguments = block.get("input")
        await self._ex._emit(
            "tool_start", {"node_id": self._node_id, "tool": name, "arguments_summary": _summarize(arguments)}
        )
        spec = self._specs.get(name)
        if spec is None:
            result, ok = f"[工具错误] 未声明的工具：{name!r}", False
        else:
            result, ok = await run_tool(spec, arguments if isinstance(arguments, dict) else {})
        await self._ex._emit(
            "tool_result", {"node_id": self._node_id, "tool": name, "ok": ok, "result_summary": _summarize(result)}
        )
        return result, not ok  # (result, is_error)
