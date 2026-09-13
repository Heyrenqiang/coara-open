"""Latest-LLM-call 镜像写盘 — 每个工作空间 × 每个智能体保留最后一轮调用。

所有智能体（主会话 root 与子智能体 sa-*）的最后一轮 LLM 调用追加写入工作空间
本地镜像 ``<workspace>/.coara/llm/llm-calls.jsonl``（每实例一行全文 entry）。
flow / daily 等不与物理空间绑定的会话落在 coara_home 级伪键镜像。

镜像含完整 system_prompt / conversation / response，属敏感调试数据，只写本地，
不进入仓库与用量统计。读取方是独立的开发者工具 ``src/devtools``（coara-devtools，
``python -m src.devtools`` 启动），直接读盘，与发布版 WebUI 零耦合。

``llm_call`` 为本会话累计 LLM API 调用序号（跨 turn；``/new`` 清会话时归零）。
``tool_call_count`` 为本会话累计工具调用次数（一轮 API 可含多个 tool_calls；``/new`` 归零）。
``tool_count`` 仍为本次请求暴露给模型的可用工具定义个数（与累计调用数无关）。
``overview`` 为侧栏智能体概况（主会话 / 前台|后台 · 类型 · 可编辑|只读）。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.core.types import Message, MessageRole
from src.llm.message_content import visible_text_from_blocks
from src.llm.provider import LLMResponse
from src.llm.usage import prompt_cache_hit_ratio, total_prompt_tokens

MAX_TOOL_RESULT_CHARS = 4000
# 写类工具：daily 经 record 写记忆与日报，也应标为可编辑
_WRITE_TOOLS = frozenset({"write", "edit", "delete", "shell", "record"})

# FlowRoot（构建对话）的独立归属桶：工作流会话不与任何工作空间绑定，
# 其 LLM 调用挂在该伪工作空间键下，镜像落 coara_home 级独立文件。
FLOW_WORKSPACE_KEY = "工作流（独立）"
# daily（日报整理）面向全部工作空间，不绑定派发时刻的前台空间：同 FlowRoot 机制。
DAILY_WORKSPACE_KEY = "日报（全局）"
# 伪工作空间键集合：有独立落盘目录（coara_home 级），不做物理路径解析。
PSEUDO_WORKSPACE_KEYS = frozenset({FLOW_WORKSPACE_KEY, DAILY_WORKSPACE_KEY})

# 镜像文件写入锁：并发 LLM 调用 append 同一文件时防交错。
_mirror_lock = threading.Lock()

# 磁盘镜像：每工作空间一个 JSONL，每个智能体实例一行全文 entry（含
# system_prompt/conversation/response），供 devtools 读取每实例最后一轮完整调用。
_MIRROR_SUBDIR = Path(".coara") / "llm"
_MIRROR_FILE_NAME = "llm-calls.jsonl"
# 淘汰：时间超过 _MIRROR_MAX_AGE_DAYS 天不保留；每空间实例数超 _MAX_MIRROR_AGENTS
# 删最旧。双维度把总量卡在实例级上限，不会无限膨胀。
_MIRROR_MAX_AGE_DAYS = 7
_MAX_MIRROR_AGENTS = 50


@dataclass(frozen=True, slots=True)
class Meta:
    session_id: str = ""
    turn_id: str = ""
    user_input: str = ""
    llm_call: int = 0
    tool_call_count: int = 0
    provider_name: str = ""
    agent_id: str = ""
    agent_kind: str = ""
    overview: str = ""


def build_agent_overview(
    *,
    user_facing: bool,
    persona_name: str = "",
    background: bool | None = None,
    bound_tool_names: list[str] | None = None,
) -> str:
    """侧栏智能体概况：主会话，或「前台/后台 · 类型 · 可编辑/只读」。"""
    if user_facing:
        return "主会话"
    mode = "后台" if background else "前台"
    names = {str(n) for n in (bound_tool_names or []) if n}
    # 空白名单 = 继承全部 / 未限制 → 可编辑
    can_edit = (not names) or bool(names & _WRITE_TOOLS)
    access = "可编辑" if can_edit else "只读"
    type_label = (persona_name or "").strip() or "子智能体"
    return f"{mode} · {type_label} · {access}"


def log_llm_call(
    workspace_dir: str,
    meta: Meta,
    *,
    system_prompt: str,
    messages: list[Message],
    model: str,
    max_tokens: int,
    temperature: float,
    tools: list[dict[str, Any]] | None,
    response: LLMResponse | None,
    call_err: BaseException | None,
) -> None:
    """Record one LLM request/response pair as the workspace×agent latest entry."""
    if not workspace_dir:
        return

    normalized_messages = _normalize_messages(system_prompt, messages)
    record, conversation = _build_record(
        meta,
        system_prompt=system_prompt,
        messages=normalized_messages,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        tools=tools or [],
        response=response,
        call_err=call_err,
    )
    agent_id = meta.agent_id or "unknown"
    entry: dict[str, Any] = {
        "ts": record["ts"],
        "agent_id": agent_id,
        "kind": meta.agent_kind or agent_id,
        "overview": meta.overview or meta.agent_kind or agent_id,
        "session_id": record.get("session_id"),
        "turn_id": record.get("turn_id"),
        "user_input": record.get("user_input"),
        "llm_call": record.get("llm_call") or 0,
        "tool_call_count": record.get("tool_call_count") or 0,
        "provider_name": record.get("provider_name"),
        "model": record.get("model"),
        "max_tokens": record.get("max_tokens") or 0,
        "temperature": record.get("temperature") or 0.0,
        "system_prompt": record.get("system_prompt"),
        "conversation": conversation,
        "response": record.get("response"),
        "error": record.get("error"),
        "tools": record.get("tools") or [],
        "tool_count": record.get("tool_count") or 0,
    }
    _append_mirror(workspace_dir, entry)


def _mirror_path(workspace_dir: str) -> Path:
    if workspace_dir in PSEUDO_WORKSPACE_KEYS:
        from src.core.coara_home import resolve_coara_home

        home = resolve_coara_home(None, None)
        return Path(home) / _MIRROR_SUBDIR / f"llm-calls-{_pseudo_slug(workspace_dir)}.jsonl"
    return Path(workspace_dir) / _MIRROR_SUBDIR / _MIRROR_FILE_NAME


def _pseudo_slug(key: str) -> str:
    return {FLOW_WORKSPACE_KEY: "flow", DAILY_WORKSPACE_KEY: "daily"}.get(key, "misc")


def _append_mirror(workspace_dir: str, entry: dict[str, Any]) -> None:
    """把一条调用全文写入镜像（按实例覆盖），失败不影响调用本身。

    镜像每实例一行全文 entry：读出现有行→按 agent_id 覆盖/追加→淘汰→原子写回。
    总量由 _MAX_MIRROR_AGENTS 卡实例数、_MIRROR_MAX_AGE_DAYS 卡龄期，不会无限膨胀。
    同会话连续调用时沿用并累加 ``iteration_usages``，把历史小轮 usage 戳回 conversation。
    """
    with _mirror_lock:
        try:
            path = _mirror_path(workspace_dir)
            path.parent.mkdir(parents=True, exist_ok=True)
            agents: dict[str, dict[str, Any]] = {}
            for old in _read_mirror(workspace_dir):
                agents[str(old.get("agent_id") or "unknown")] = old
            agent_id = str(entry.get("agent_id") or "unknown")
            prev = agents.get(agent_id)
            _merge_iteration_usages(entry, prev)
            agents[agent_id] = entry
            _prune_agents_map(agents)
            from src.core.json_store import write_text_atomic

            payload = "\n".join(json.dumps(e, ensure_ascii=False) for e in agents.values())
            write_text_atomic(path, payload + ("\n" if payload else ""))
        except Exception as exc:  # noqa: BLE001 — 写盘失败不影响调用本身，但留观测
            from src.core.logger import logger

            logger.warning(f"llmlog mirror write failed: {exc}")


def _count_conversation_iterations(conversation: list[dict[str, Any]] | None) -> int:
    if not conversation:
        return 0
    return sum(len(r.get("iterations") or []) for r in conversation if isinstance(r, dict))


def _stamp_usages_on_conversation(
    conversation: list[dict[str, Any]] | None,
    hist_usages: list[dict[str, Any] | None],
) -> None:
    """把历史小轮 usage 按顺序戳到 conversation.iterations（当前 response 不在内）。"""
    if not conversation or not hist_usages:
        return
    idx = 0
    for round_obj in conversation:
        for it in round_obj.get("iterations") or []:
            if idx >= len(hist_usages):
                return
            usage = hist_usages[idx]
            idx += 1
            if usage:
                it["usage"] = usage


def _merge_iteration_usages(entry: dict[str, Any], prev: dict[str, Any] | None) -> None:
    """同会话累加小轮 usage：历史戳回 conversation，全量列表进 iteration_usages。"""
    conversation = entry.get("conversation")
    n_hist = _count_conversation_iterations(conversation if isinstance(conversation, list) else None)
    current = None
    response = entry.get("response")
    if isinstance(response, dict):
        usage = response.get("usage")
        if isinstance(usage, dict) and usage:
            current = usage

    prev_usages: list[Any] = []
    if (
        isinstance(prev, dict)
        and prev.get("session_id")
        and prev.get("session_id") == entry.get("session_id")
    ):
        raw = prev.get("iteration_usages")
        if isinstance(raw, list):
            prev_usages = [u for u in raw if isinstance(u, dict)]

    # 压缩/截断后历史变短：只保留末尾对齐的 n_hist 条
    hist_usages: list[dict[str, Any] | None]
    if n_hist <= 0:
        hist_usages = []
    elif len(prev_usages) >= n_hist:
        hist_usages = list(prev_usages[-n_hist:])
    else:
        # 缺口补 None，避免错位把旧费用贴到新小轮上
        pad = n_hist - len(prev_usages)
        hist_usages = [None] * pad + list(prev_usages)

    _stamp_usages_on_conversation(conversation if isinstance(conversation, list) else None, hist_usages)

    iteration_usages: list[dict[str, Any]] = [u for u in hist_usages if isinstance(u, dict)]
    if current is not None:
        iteration_usages.append(current)
    entry["iteration_usages"] = iteration_usages


def _prune_agents_map(agents: dict[str, dict[str, Any]]) -> None:
    """与写侧实例淘汰规则一致，作用于任意 agents map。"""
    cutoff = datetime.now(UTC).timestamp() - _MIRROR_MAX_AGE_DAYS * 86400

    def _ts(entry: dict[str, Any]) -> float:
        try:
            return datetime.fromisoformat(str(entry.get("ts") or "")).timestamp()
        except ValueError:
            return 0.0

    for aid in [aid for aid, e in agents.items() if _ts(e) < cutoff]:
        agents.pop(aid, None)
    if len(agents) > _MAX_MIRROR_AGENTS:
        ordered = sorted(agents.items(), key=lambda kv: _ts(kv[1]))
        for aid, _ in ordered[: len(agents) - _MAX_MIRROR_AGENTS]:
            agents.pop(aid, None)


def _read_mirror(workspace_dir: str) -> list[dict[str, Any]]:
    path = _mirror_path(workspace_dir)
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(entry, dict):
            continue
        # 旧格式（append-only 摘要行，无全文）直接废弃：不认、不恢复，
        # 下次 _append_mirror 写回时随之清掉，不做兼容迁移
        if "response" not in entry and "conversation" not in entry:
            continue
        entries.append(entry)
    return entries


def _normalize_messages(system_prompt: str, messages: list[Message]) -> list[Message]:
    if not (system_prompt or "").strip():
        return list(messages)
    return [message for message in messages if message.role != MessageRole.SYSTEM]


def _build_record(
    meta: Meta,
    *,
    system_prompt: str,
    messages: list[Message],
    model: str,
    max_tokens: int,
    temperature: float,
    tools: list[dict[str, Any]],
    response: LLMResponse | None,
    call_err: BaseException | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    conversation = _build_conversation(messages)
    record: dict[str, Any] = {
        "ts": datetime.now(UTC).isoformat(),
        "session_id": meta.session_id or None,
        "turn_id": meta.turn_id or None,
        "user_input": meta.user_input or None,
        "llm_call": meta.llm_call,
        "tool_call_count": meta.tool_call_count,
        "provider_name": meta.provider_name or None,
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "system_prompt": system_prompt or None,
    }

    if call_err is not None:
        record["error"] = str(call_err)

    response_record = _response_record(response)
    if response_record is not None:
        record["response"] = response_record

    record["tool_count"] = len(tools)
    if tools:
        record["tools"] = _compact_tool_definitions(tools)

    return record, conversation


def _message_text(message: Message) -> str:
    content = message.content
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return visible_text_from_blocks(content).strip()
    return str(content or "").strip()


def _build_conversation(messages: list[Message]) -> list[dict[str, Any]]:
    """Build a conversation that preserves per-iteration assistant/tool ordering.

    Each user message starts a new round. Within a round, every assistant message
    (which may contain text and/or tool_calls) and the tool_results that follow it
    form one ``iteration``. This mirrors the actual ReAct loop instead of flattening
    all tool_calls and tool_results into a single pseudo-assistant step.
    """
    rounds: list[dict[str, Any]] = []
    current_iteration: dict[str, Any] | None = None

    def _flush_iteration() -> None:
        nonlocal current_iteration
        if current_iteration is not None:
            if not rounds:
                rounds.append({"round": 1})
            rounds[-1].setdefault("iterations", []).append(current_iteration)
            current_iteration = None

    for message in messages:
        text = _message_text(message)
        if message.role == MessageRole.USER:
            _flush_iteration()
            rounds.append(
                {
                    "round": len(rounds) + 1,
                    "user": text,
                    "context": _is_context_user_message(text),
                }
            )
        elif message.role == MessageRole.ASSISTANT:
            assistant = _assistant_from_message(message)
            if assistant is not None:
                _flush_iteration()
                current_iteration = {"assistant": assistant}
        elif message.role in {MessageRole.TOOL_RESULT, MessageRole.TOOL}:
            if not rounds:
                continue
            content, truncated = _truncate(text, MAX_TOOL_RESULT_CHARS)
            if current_iteration is None:
                # Tool result without a preceding assistant message: create an empty
                # iteration so the result is still visible in the dump.
                current_iteration = {}
            tool_results = current_iteration.setdefault("tool_results", [])
            tool_results.append(
                {
                    "tool": message.name or "",
                    "content": content,
                    "truncated": truncated or None,
                }
            )

    _flush_iteration()
    return rounds


def _assistant_from_message(message: Message) -> dict[str, Any] | None:
    record: dict[str, Any] = {}
    content = _message_text(message)
    if content:
        record["content"] = content
    if message.reasoning_content:
        record["reasoning_content"] = message.reasoning_content.strip()
    if message.provider_wire_blocks:
        record["provider_wire_blocks"] = message.provider_wire_blocks
    if message.tool_calls:
        record["tool_calls"] = [_normalize_tool_call_for_log(tool_call) for tool_call in message.tool_calls]
    return record or None


_MAX_TOOL_DESCRIPTION_CHARS = 500
_MAX_TOOL_PARAMETERS_CHARS = 2000


def _compact_tool_definitions(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """精简工具定义为前端 schema 展示：name 必填，description/parameters 截断。"""
    compact: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if not name:
            continue
        entry: dict[str, Any] = {"name": str(name)}
        description = tool.get("description")
        if isinstance(description, str) and description:
            entry["description"] = _truncate(description, _MAX_TOOL_DESCRIPTION_CHARS)[0]
        parameters = tool.get("parameters")
        if parameters is not None:
            text = json.dumps(parameters, ensure_ascii=False)
            entry["parameters"] = _truncate(text, _MAX_TOOL_PARAMETERS_CHARS)[0]
        compact.append(entry)
    return compact


_APPROVAL_META_TOOL_NAMES = frozenset({"write", "edit", "delete", "shell"})


def _normalize_tool_call_for_log(tool_call: Any) -> dict[str, Any]:
    """Normalize tool call for logging, ensuring approval meta-params are explicit."""
    record = {"id": tool_call.id, "name": tool_call.name, "arguments": dict(tool_call.arguments)}
    if tool_call.name in _APPROVAL_META_TOOL_NAMES:
        args = record["arguments"]
        if "require_approval" not in args:
            args["require_approval"] = False
        if "approval_reason" not in args:
            args["approval_reason"] = ""
    return record


def _response_record(response: LLMResponse | None) -> dict[str, Any] | None:
    if response is None:
        return None

    content, reasoning_from_storage = response.assistant_storage_fields()
    content_text = content if isinstance(content, str) else str(content or "")
    reasoning_text = _reasoning_text(response) or str(reasoning_from_storage or "").strip()

    record: dict[str, Any] = {}
    if content_text.strip():
        record["content"] = content_text
    if reasoning_text:
        record["reasoning_content"] = reasoning_text

    wire_blocks = _provider_blocks_for_dump(response, reasoning_text=reasoning_text, content_text=content_text)
    if wire_blocks:
        record["provider_content_blocks"] = wire_blocks

    if response.tool_calls:
        record["tool_calls"] = [_normalize_tool_call_for_log(tool_call) for tool_call in response.tool_calls]
    if response.finish_reason:
        record["finish_reason"] = response.finish_reason

    usage_record = _usage_record(response.usage)
    if usage_record is not None:
        record["usage"] = usage_record

    if not record:
        return None
    return record


def _reasoning_text(response: LLMResponse) -> str:
    if response.reasoning_content:
        return response.reasoning_content.strip()
    parts = [
        str(block.get("thinking") or "")
        for block in response.provider_content_blocks or []
        if isinstance(block, dict) and block.get("type") == "thinking"
    ]
    return "\n".join(part for part in parts if part).strip()


def _provider_blocks_for_dump(
    response: LLMResponse,
    *,
    reasoning_text: str,
    content_text: str,
) -> list[dict[str, Any]] | None:
    """Drop wire blocks already represented in content / reasoning_content."""
    blocks = response.provider_content_blocks
    if not blocks:
        return None
    normalized_content = content_text.strip()
    filtered: list[dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict):
            filtered.append(block)
            continue
        kind = block.get("type")
        if kind == "thinking":
            thinking = str(block.get("thinking") or "").strip()
            if not thinking or (reasoning_text and thinking == reasoning_text):
                continue
        elif kind == "text":
            text = str(block.get("text") or "").strip()
            if normalized_content and text == normalized_content:
                continue
        filtered.append(block)
    return filtered or None


def _usage_record(usage: dict[str, int] | None) -> dict[str, Any] | None:
    if not usage:
        return None
    prompt_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or 0)
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_creation = int(usage.get("cache_creation_input_tokens") or 0)
    cached_tokens = int(usage.get("cached_tokens") or 0)

    if not any((prompt_tokens, completion_tokens, total_tokens, cache_read, cache_creation, cached_tokens)):
        return None

    record: dict[str, Any] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens or prompt_tokens + completion_tokens,
    }
    if cache_read:
        record["cache_read_input_tokens"] = cache_read
    if cache_creation:
        record["cache_creation_input_tokens"] = cache_creation
    if cached_tokens:
        record["cached_tokens"] = cached_tokens

    cache_hit_ratio = prompt_cache_hit_ratio(usage)
    if cache_hit_ratio is not None:
        record["cache_hit_ratio"] = cache_hit_ratio

    if cache_read:
        total_input = total_prompt_tokens(usage)
        if total_input:
            record["anthropic_cache_hit_ratio"] = cache_read / total_input

    return record


def _is_context_user_message(text: str) -> bool:
    if not text:
        return False
    if text.startswith(
        (
            "<系统消息>",
            "<系统提醒>",
            "<工作空间消息>",
        )
    ):
        return True
    return text.startswith("[mobile]")


def _truncate(text: str, max_chars: int) -> tuple[str, bool]:
    if max_chars <= 0 or len(text) <= max_chars:
        return text, False
    return text[:max_chars] + "…", True
