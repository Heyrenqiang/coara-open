"""Context window helpers."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

from src.core.logger import logger
from src.core.types import Message, MessageRole
from src.llm.tokenizer import estimate_tokens as _estimate_tokens_raw
from src.llm.usage import total_prompt_tokens

DEFAULT_CONTEXT_TOKENS = 200_000
WARN_USAGE_RATIO = 0.75
BLOCK_USAGE_RATIO = 0.90

# ============================================================================
# Context Compression Configuration
# ============================================================================
# qwen-code uses LLM-based compression with structured prompts:
# - COMPRESSION_TOKEN_THRESHOLD = 0.8 (80% of context window)
# - COMPRESSION_PRESERVE_THRESHOLD = 0.3 by default
# - MIN_COMPRESSION_FRACTION = 0.05 (skip if compressible < 5%)
# - Uses getCompressionPrompt() to generate structured XML summary


class CompressionConfig:
    """Configuration for intelligent context compression.

    Based on qwen-code's ChatCompressionService:
    - Threshold: When to trigger compression (80% of context)
    - Preserve ratio: How much recent history to keep (default 30%, configurable)
    - Min fraction: Skip compression if too little to compress (5%)
    - LLM-based: Uses structured prompts for semantic compression
    """

    # Trigger compression at 80% of context window
    TOKEN_THRESHOLD = 0.80

    # Default recent-history preserve ratio; runtime config may override it.
    PRESERVE_RATIO = 0.30

    # Minimum fraction that must be compressible to proceed
    MIN_COMPRESSIBLE_FRACTION = 0.05

    # Minimum messages to consider compression worthwhile
    MIN_MESSAGES_FOR_COMPRESSION = 6

    # Kimi-cli style: preserve the last N messages uncompressed, compress the rest.
    # When set, this takes precedence over PRESERVE_RATIO.
    PRESERVE_LAST_N = 2


@dataclass(slots=True)
class ContextWindowInfo:
    """Resolved context window metadata."""

    tokens: int
    source: Literal["model", "config", "default"] = "default"


@dataclass(slots=True)
class ContextGuardResult:
    """Outcome of a context usage check."""

    should_warn: bool = False
    should_block: bool = False
    reason: str = ""
    usage_ratio: float = 0.0
    remaining_tokens: int = 0


def payload_fingerprint(system_prompt: str, tool_definitions: list[dict[str, Any]]) -> str:
    """system prompt + 工具模式的内容指纹：等长替换也能检出变更。"""
    digest = hashlib.sha1(system_prompt.encode("utf-8", "ignore"))
    digest.update(b"\x00")
    digest.update(json.dumps(tool_definitions, ensure_ascii=False, sort_keys=True).encode("utf-8", "ignore"))
    return digest.hexdigest()


# 工具定义内容指纹缓存：tool_definitions 由 tool_manager 缓存复用（同一 list 对象
# 在回合内不变），指纹按对象身份缓存，避免每迭代重复 json.dumps+sha1 大对象
_fingerprint_cache: dict[int, tuple[int, str]] = {}


def payload_fingerprint_cached(system_prompt: str, tool_definitions: list[dict[str, Any]]) -> str:
    """按 tool_definitions 对象身份缓存的指纹：同一对象（且 system_prompt 同 hash）直接复用。"""
    key = id(tool_definitions)
    sys_hash = hash(system_prompt)
    hit = _fingerprint_cache.get(key)
    if hit is not None and hit[0] == sys_hash:
        return hit[1]
    fp = payload_fingerprint(system_prompt, tool_definitions)
    _fingerprint_cache[key] = (sys_hash, fp)
    # 防无限增长：对象被回收后 id 可能复用，定期清理（简单封顶）
    if len(_fingerprint_cache) > 64:
        _fingerprint_cache.clear()
        _fingerprint_cache[key] = (sys_hash, fp)
    return fp


@dataclass(slots=True)
class LlmUsageSnapshot:
    """Last provider-reported turn usage plus cumulative cache accounting.

    ``usage`` is the **latest** turn (context-fill % on the toolbar).
    Cumulative fields drive session cache hit rate:
    ``Σ cache_read / Σ prompt`` across turns in this session.
    """

    usage: dict[str, int] | None = None
    history_len: int | None = None
    system_len: int | None = None
    tool_count: int | None = None
    payload_hash: str | None = None
    cumulative_prompt_tokens: int = 0
    cumulative_cache_read_tokens: int = 0
    # 最近一次回合是否以 finish_reason=partial 收尾（该回合 usage 可能残缺或缺失）
    last_turn_partial: bool = False

    def clear(self) -> None:
        self.usage = None
        self.history_len = None
        self.system_len = None
        self.tool_count = None
        self.payload_hash = None
        self.cumulative_prompt_tokens = 0
        self.cumulative_cache_read_tokens = 0
        self.last_turn_partial = False

    @property
    def has_reported_input(self) -> bool:
        return total_prompt_tokens(self.usage) > 0

    @property
    def cache_hit_ratio(self) -> float | None:
        from src.llm.usage import cumulative_prompt_cache_hit_ratio

        return cumulative_prompt_cache_hit_ratio(
            cumulative_cache_read_tokens=self.cumulative_cache_read_tokens,
            cumulative_prompt_tokens=self.cumulative_prompt_tokens,
        )

    def record_turn(
        self,
        *,
        usage: dict[str, int],
        history_len: int,
        system_len: int,
        tool_count: int,
        payload_hash: str | None = None,
        partial: bool = False,
    ) -> None:
        from src.llm.usage import cache_read_tokens

        prompt = total_prompt_tokens(usage)
        if prompt <= 0:
            # partial 回合可能完全没有 usage 上报（如 MiMo 仅最终 chunk 给
            # usage）：只更新标记，保留上一轮完整记账不被覆盖
            self.last_turn_partial = partial
            return
        self.usage = dict(usage)
        self.history_len = history_len
        self.system_len = system_len
        self.tool_count = tool_count
        self.payload_hash = payload_hash
        self.cumulative_prompt_tokens += prompt
        self.cumulative_cache_read_tokens += cache_read_tokens(usage)
        self.last_turn_partial = partial

    def to_dict(self) -> dict[str, Any] | None:
        """Serialize for session persistence; None when nothing was reported."""
        if not self.has_reported_input and self.cumulative_prompt_tokens <= 0 and not self.last_turn_partial:
            return None
        return {
            "usage": dict(self.usage or {}),
            "history_len": self.history_len,
            "system_len": self.system_len,
            "tool_count": self.tool_count,
            "payload_hash": self.payload_hash,
            "cumulative_prompt_tokens": self.cumulative_prompt_tokens,
            "cumulative_cache_read_tokens": self.cumulative_cache_read_tokens,
            "last_turn_partial": self.last_turn_partial,
        }

    def restore(self, data: dict[str, Any] | None) -> None:
        """Restore from a persisted ``to_dict`` payload (tolerates bad input)."""
        self.clear()
        if not isinstance(data, dict):
            return
        usage = data.get("usage")
        if isinstance(usage, dict) and total_prompt_tokens(usage) > 0:
            self.usage = {str(k): int(v) for k, v in usage.items() if isinstance(v, (int, float))}
        for field in ("history_len", "system_len", "tool_count"):
            value = data.get(field)
            if isinstance(value, (int, float)):
                setattr(self, field, int(value))
        payload_hash = data.get("payload_hash")
        if isinstance(payload_hash, str) and payload_hash:
            self.payload_hash = payload_hash
        for field in ("cumulative_prompt_tokens", "cumulative_cache_read_tokens"):
            value = data.get(field)
            if isinstance(value, (int, float)) and int(value) >= 0:
                setattr(self, field, int(value))
        self.last_turn_partial = bool(data.get("last_turn_partial"))


class ContextWindowManager:
    """Resolve model context window sizes and estimate token usage."""

    def __init__(
        self,
        model_context_window: int | None = None,
        configured_context_window: int | None = None,
        compression_threshold: float = CompressionConfig.TOKEN_THRESHOLD,
        compression_preserve_ratio: float = CompressionConfig.PRESERVE_RATIO,
        min_compressible_fraction: float = CompressionConfig.MIN_COMPRESSIBLE_FRACTION,
        compression_preserve_last_n: int | None = CompressionConfig.PRESERVE_LAST_N,
    ):
        self.model_context_window = model_context_window
        self.configured_context_window = configured_context_window
        self.compression_threshold = self._normalize_ratio(compression_threshold, CompressionConfig.TOKEN_THRESHOLD)
        self.compression_preserve_ratio = self._normalize_ratio(
            compression_preserve_ratio,
            CompressionConfig.PRESERVE_RATIO,
        )
        self.min_compressible_fraction = self._normalize_ratio(
            min_compressible_fraction,
            CompressionConfig.MIN_COMPRESSIBLE_FRACTION,
        )
        self.compression_preserve_last_n = self._normalize_int(
            compression_preserve_last_n,
            CompressionConfig.PRESERVE_LAST_N,
            min_value=1,
        )

    @staticmethod
    def _normalize_int(value: int | None, default: int, min_value: int = 1) -> int | None:
        if value is None:
            return None  # Explicitly disabled
        try:
            numeric = int(value)
        except (TypeError, ValueError):
            return default
        if numeric < min_value:
            return None  # Disable count-based preservation if too small
        return numeric

    def configure(
        self,
        *,
        model_context_window: int | None = None,
        configured_context_window: int | None = None,
        compression_threshold: float | None = None,
        compression_preserve_ratio: float | None = None,
        min_compressible_fraction: float | None = None,
        compression_preserve_last_n: int | None = None,
    ) -> None:
        if model_context_window is not None:
            self.model_context_window = model_context_window
        if configured_context_window is not None:
            self.configured_context_window = configured_context_window
        if compression_threshold is not None:
            self.compression_threshold = self._normalize_ratio(
                compression_threshold,
                CompressionConfig.TOKEN_THRESHOLD,
            )
        if compression_preserve_ratio is not None:
            self.compression_preserve_ratio = self._normalize_ratio(
                compression_preserve_ratio,
                CompressionConfig.PRESERVE_RATIO,
            )
        if min_compressible_fraction is not None:
            self.min_compressible_fraction = self._normalize_ratio(
                min_compressible_fraction,
                CompressionConfig.MIN_COMPRESSIBLE_FRACTION,
            )
        if compression_preserve_last_n is not None:
            self.compression_preserve_last_n = self._normalize_int(
                compression_preserve_last_n,
                CompressionConfig.PRESERVE_LAST_N,
                min_value=1,
            )

    @staticmethod
    def _normalize_ratio(value: float | None, default: float) -> float:
        if value is None:
            return default
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return default
        if numeric <= 0 or numeric >= 1:
            return default
        return numeric

    def resolve_context_window(self) -> ContextWindowInfo:
        if self.configured_context_window:
            return ContextWindowInfo(tokens=self.configured_context_window, source="config")
        if self.model_context_window:
            return ContextWindowInfo(tokens=self.model_context_window, source="model")
        return ContextWindowInfo(tokens=DEFAULT_CONTEXT_TOKENS, source="default")

    def evaluate_guard(self, used_tokens: int, max_tokens: int | None = None) -> ContextGuardResult:
        window_tokens = max_tokens or self.resolve_context_window().tokens
        if used_tokens <= 0:
            return ContextGuardResult(remaining_tokens=window_tokens)

        usage_ratio = used_tokens / window_tokens
        remaining_tokens = max(window_tokens - used_tokens, 0)

        if usage_ratio >= BLOCK_USAGE_RATIO:
            return ContextGuardResult(
                should_warn=True,
                should_block=True,
                reason=(
                    f"Context usage {usage_ratio:.0%} exceeds the hard limit ({used_tokens}/{window_tokens} tokens)."
                ),
                usage_ratio=usage_ratio,
                remaining_tokens=remaining_tokens,
            )

        if usage_ratio >= WARN_USAGE_RATIO:
            return ContextGuardResult(
                should_warn=True,
                reason=(
                    f"Context usage {usage_ratio:.0%} is above the warning threshold "
                    f"({used_tokens}/{window_tokens} tokens)."
                ),
                usage_ratio=usage_ratio,
                remaining_tokens=remaining_tokens,
            )

        return ContextGuardResult(
            usage_ratio=usage_ratio,
            remaining_tokens=remaining_tokens,
        )

    def estimate_tokens(self, text: str) -> int:
        return _estimate_tokens_raw(text)

    # Vision 块在 guard 里按「每张图固定上限」估算，避免把整段 base64 当文本 token 算爆。
    _IMAGE_BLOCK_TOKEN_CAP = 10_000

    def _estimate_content_block_tokens(self, item: Any) -> int:
        if not isinstance(item, dict):
            return self.estimate_tokens(str(item))
        block_type = item.get("type")
        if block_type == "thinking":
            return self.estimate_tokens(str(item.get("thinking", "")))
        if block_type == "text":
            return self.estimate_tokens(str(item.get("text", "")))
        if block_type == "image":
            source = item.get("source")
            if isinstance(source, dict) and source.get("type") == "base64":
                data = source.get("data")
                if isinstance(data, str) and data:
                    return min(self.estimate_tokens(data), self._IMAGE_BLOCK_TOKEN_CAP)
            return self._IMAGE_BLOCK_TOKEN_CAP
        return self.estimate_tokens(str(item))

    def _estimate_message_content_tokens(self, message: Message | dict[str, Any]) -> int:
        if isinstance(message, Message):
            content = message.content
            tool_calls = message.tool_calls or []
        else:
            content = message.get("content", "")
            tool_calls = message.get("tool_calls") or []

        total = 0
        if isinstance(content, str):
            total += self.estimate_tokens(content)
        elif isinstance(content, list):
            for item in content:
                total += self._estimate_content_block_tokens(item)
        else:
            total += self.estimate_tokens(str(content))

        for tool_call in tool_calls:
            if hasattr(tool_call, "arguments"):
                total += self.estimate_tokens(str(tool_call.arguments))
            elif isinstance(tool_call, dict):
                total += self.estimate_tokens(str(tool_call.get("arguments", "")))

        wire_blocks = None
        reasoning = None
        if isinstance(message, Message):
            wire_blocks = message.provider_wire_blocks
            reasoning = message.reasoning_content
        else:
            wire_blocks = message.get("provider_wire_blocks")
            reasoning = message.get("reasoning_content")

        if wire_blocks:
            for block in wire_blocks:
                total += self._estimate_content_block_tokens(block)
        if reasoning:
            total += self.estimate_tokens(str(reasoning))

        return total

    def _get_message_content(self, message: Message | dict[str, Any]) -> str:
        """Flatten message content for compression heuristics (not token guard)."""
        if isinstance(message, Message):
            content = message.content
            tool_calls = message.tool_calls or []
        else:
            content = message.get("content", "")
            tool_calls = message.get("tool_calls") or []

        parts: list[str] = []
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "image":
                    parts.append("[image]")
                elif isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                elif isinstance(item, dict) and item.get("type") == "thinking":
                    continue
                else:
                    parts.append(str(item))

        for tool_call in tool_calls:
            if hasattr(tool_call, "arguments"):
                parts.append(str(tool_call.arguments))
            elif isinstance(tool_call, dict):
                parts.append(str(tool_call.get("arguments", "")))

        return "\n".join(parts)

    # 系统级消息标签前缀：压缩时头部这些消息原样保留，绝不压进摘要。
    # 环境上下文种子（<系统消息> 包裹）等都在此列——
    # 压掉它们会让会话像「新建会话」，且下一回合注入逻辑检测不到已有种子
    # 会把环境上下文追加到历史尾部，顺序错乱。<情境> 理论上可被配置挪到
    # conversation 之前成为 seed，一并保护。
    _HEAD_PROTECTED_TAGS = ("<系统消息>", "<系统提醒>", "<情境>", "<后台结果>")
    # 环境种子标识：无论位于历史何处都必须保留（注入逻辑曾把种子追加到
    # 历史尾部，压缩按「开头连续保护」扫描不到，种子被压进摘要——恶性循环）。
    # 含拆分后的概况 / AGENTS.md / 用户规则等前缀模块。
    _ENV_SEED_MARKERS = (
        "环境上下文：",
        "工作空间概况：",
        "AGENTS.md：",
        "用户规则：",
    )

    def _is_head_protected(self, message: Message | dict[str, Any]) -> bool:
        """True when the message is a head-level system message that compression must keep."""
        role = self._get_role(message)
        if role in (MessageRole.SYSTEM, "system"):
            return True
        if role in (MessageRole.USER, "user"):
            text = self._get_message_content(message).strip()
            return text.startswith(self._HEAD_PROTECTED_TAGS)
        return False

    def _is_env_seed(self, message: Message | dict[str, Any]) -> bool:
        """True for prefix context-module seed messages (env / ws / AGENT / rules)."""
        text = self._get_message_content(message)
        return any(marker in text for marker in self._ENV_SEED_MARKERS)

    def _find_head_boundary(self, messages: list[Message | dict[str, Any]]) -> int:
        """Index of the first compressible message (head system messages stay put)."""
        boundary = 0
        for message in messages:
            if self._is_head_protected(message):
                boundary += 1
            else:
                break
        return boundary

    def estimate_messages_tokens(self, messages: list[Message | dict[str, Any]]) -> int:
        total = 0
        for message in messages:
            total += self._estimate_message_content_tokens(message)
        return total

    def estimate_tool_definitions_tokens(self, tool_definitions: list[dict[str, Any]] | None) -> int:
        """Heuristic token cost of tool schemas sent alongside each LLM request."""
        if not tool_definitions:
            return 0
        return self.estimate_tokens(json.dumps(tool_definitions, ensure_ascii=False))

    def estimate_llm_input_tokens(
        self,
        *,
        system_prompt: str = "",
        messages: list[Message | dict[str, Any]] | None = None,
        tool_definitions: list[dict[str, Any]] | None = None,
    ) -> int:
        """Estimate total input tokens: system + messages + tool schemas."""
        total = self.estimate_tokens(system_prompt)
        if messages:
            total += self.estimate_messages_tokens(messages)
        total += self.estimate_tool_definitions_tokens(tool_definitions)
        return total

    def resolve_input_tokens(
        self,
        *,
        system_prompt: str = "",
        messages: list[Message | dict[str, Any]] | None = None,
        tool_definitions: list[dict[str, Any]] | None = None,
        snapshot: LlmUsageSnapshot | None = None,
    ) -> int:
        """Prefer provider-reported prompt tokens (incl. cache); cheap heuristic fallback.

        Budget decisions must not run the offline tokenizer on the full history —
        APIs return real usage, proactive compress uses that, and provider
        overflow force-compresses once (see turn_orchestrator).
        """
        snap = snapshot or LlmUsageSnapshot()
        reported = total_prompt_tokens(snap.usage)
        baseline_len = snap.history_len
        message_list = messages or []
        tool_list = tool_definitions or []
        system_len = len(system_prompt)
        tool_count = len(tool_list)

        if snap.payload_hash is not None:
            # 内容指纹比较：等长替换（静态 prompt 重渲染、工具 schema 调整）也能检出
            payload_changed = payload_fingerprint_cached(system_prompt, tool_list) != snap.payload_hash
        else:
            # 旧快照无内容指纹，退化为长度比较
            payload_changed = (
                snap.system_len is not None
                and snap.tool_count is not None
                and (system_len != snap.system_len or tool_count != snap.tool_count)
            )

        if reported > 0 and baseline_len is not None and not payload_changed:
            if len(message_list) > baseline_len:
                delta_messages = message_list[baseline_len:]
                return reported + self.estimate_messages_tokens(delta_messages)
            if len(message_list) < baseline_len:
                # History shrank (compression); re-budget the full payload cheaply.
                return self.estimate_llm_input_tokens(
                    system_prompt=system_prompt,
                    messages=message_list,
                    tool_definitions=tool_list,
                )
            return reported

        return self.estimate_llm_input_tokens(
            system_prompt=system_prompt,
            messages=message_list,
            tool_definitions=tool_list,
        )

    def _get_role(self, message: Message | dict[str, Any]) -> str | None:
        if isinstance(message, Message):
            return message.role.value if hasattr(message.role, "value") else str(message.role)
        return message.get("role")

    def find_compression_split_point(
        self,
        messages: list[Message | dict[str, Any]],
        preserve_ratio: float | None = None,
        preserve_last_n: int | None = None,
    ) -> int:
        """Find the index where we should split for compression.

        Two modes (mutually exclusive):
        - preserve_last_n: keep the last N messages, compress everything before.
          This is the kimi-cli style and is tried first when given.
        - preserve_ratio: keep the most recent 'preserve_ratio' fraction by
          character count (qwen-code style).

        Tool-call safety: while walking backward, track tool_call_ids whose
        matching assistant.tool_calls has NOT yet been seen in the candidate
        preserve slice. A USER index is only a valid split when this set is
        empty — otherwise compressing the prefix would leave orphan tool
        responses in the preserved tail and the LLM provider would reject the
        request.

        Returns: Index of first message to KEEP (0 = keep all, len = compress all)
        """
        if not messages:
            return 0

        head_boundary = self._find_head_boundary(messages)

        # --- kimi-cli style: preserve a fixed number of recent messages ---
        if preserve_last_n is not None and preserve_last_n > 0:
            split = self._find_split_for_preserve_last_n(messages, preserve_last_n)
            # 头部系统消息（环境种子等）不可压缩：split 不能越过它们
            return max(split, head_boundary)

        # --- qwen-code style: preserve by character ratio ---
        preserve = self._normalize_ratio(preserve_ratio, self.compression_preserve_ratio)
        char_counts = [len(self._get_message_content(m)) for m in messages]
        total_chars = sum(char_counts)
        target_chars = total_chars * preserve

        cumulative_chars = 0
        pending_tool_results: set[str] = set()
        last_safe_split = len(messages)  # Default: compress everything

        for i in range(len(messages) - 1, -1, -1):
            msg = messages[i]
            role = self._get_role(msg)

            cumulative_chars += char_counts[i]

            if role in (MessageRole.TOOL, "tool", MessageRole.TOOL_RESULT, "tool_result"):
                tool_call_id = self._get_tool_call_id(msg)
                if tool_call_id:
                    pending_tool_results.add(tool_call_id)
                continue

            if role in (MessageRole.ASSISTANT, "assistant"):
                for tc_id in self._iter_tool_call_ids(msg):
                    pending_tool_results.discard(tc_id)
                continue

            if role in (MessageRole.USER, "user"):
                if pending_tool_results:
                    continue
                if cumulative_chars >= target_chars:
                    return max(i, head_boundary)
                last_safe_split = i

        return max(last_safe_split, head_boundary)

    def _is_tool_safe_split(self, messages: list[Message | dict[str, Any]], start: int) -> bool:
        """Return True when messages[start:] has no orphan tool results."""
        if start <= 0:
            return True
        introduced: set[str] = set()
        for msg in messages[start:]:
            role = self._get_role(msg)
            if role in (MessageRole.ASSISTANT, "assistant"):
                introduced.update(self._iter_tool_call_ids(msg))
                continue
            if role in (MessageRole.TOOL, "tool", MessageRole.TOOL_RESULT, "tool_result"):
                tool_call_id = self._get_tool_call_id(msg)
                if tool_call_id and tool_call_id not in introduced:
                    return False
        return True

    def _find_split_for_preserve_last_n(
        self,
        messages: list[Message | dict[str, Any]],
        preserve_last_n: int,
    ) -> int:
        """Keep the last N messages, expanding backward for tool-call pairing safety."""
        if preserve_last_n <= 0 or len(messages) <= preserve_last_n:
            return 0

        start = len(messages) - preserve_last_n
        while start > 0 and not self._is_tool_safe_split(messages, start):
            start -= 1
        return start

    def _get_tool_call_id(self, message: Message | dict[str, Any]) -> str | None:
        if isinstance(message, Message):
            return message.tool_call_id
        return message.get("tool_call_id")

    def _iter_tool_call_ids(self, message: Message | dict[str, Any]):
        tool_calls = message.tool_calls or [] if isinstance(message, Message) else message.get("tool_calls") or []
        for tc in tool_calls:
            tc_id = getattr(tc, "id", None)
            if tc_id is None and isinstance(tc, dict):
                tc_id = tc.get("id")
            if tc_id:
                yield tc_id

    async def compress_with_llm(
        self,
        messages: list[Message | dict[str, Any]],
        *,
        split_point: int | None = None,
        preserve_ratio: float | None = None,
        preserve_last_n: int | None = None,
        compact_hook_runner: Any | None = None,
        janitor_coara: Any | None = None,
    ) -> tuple[list[Message | dict[str, Any]], dict[str, Any]]:
        """LLM-based context compression.

        Uses ``context.compression`` llm_profile via :class:`LLMService`.
        Falls back to simple truncation if the LLM call fails.
        """
        if not messages:
            return messages, {"compressed": False, "status": "NOOP", "reason": "too_few_messages"}

        head_boundary = self._find_head_boundary(messages)
        split_point = (
            max(split_point, head_boundary)
            if split_point is not None
            else self.find_compression_split_point(
                messages, preserve_ratio=preserve_ratio, preserve_last_n=preserve_last_n
            )
        )
        # 环境种子可能被历史注入逻辑追加到中段/尾部（不在头部），仍必须保留：
        # 从压缩区与保留区剔除种子，统一并入头部，摘要前保持种子原位语义。
        stray_seed = [m for m in messages[head_boundary:] if self._is_env_seed(m)]
        to_compress = [m for m in messages[head_boundary:split_point] if not self._is_env_seed(m)]
        to_preserve = [m for m in messages[split_point:] if not self._is_env_seed(m)]
        head_messages = list(messages[:head_boundary]) + stray_seed

        if not to_compress or len(to_compress) < CompressionConfig.MIN_MESSAGES_FOR_COMPRESSION:
            return messages, {"compressed": False, "status": "NOOP", "reason": "nothing_to_compress"}

        # 压缩前冻结完整历史：成功后才派 janitor（快照仍是压缩前原文）。
        # 失败/取消不派——避免「压缩没缩短上下文、管家却白跑一遍」。
        janitor_history_snapshot = list(messages) if janitor_coara is not None else None

        estimated_tokens = self.estimate_messages_tokens(messages)
        if compact_hook_runner is not None:
            await compact_hook_runner.run_pre(len(messages), estimated_tokens)

        result_messages: list[Message | dict[str, Any]] = messages
        result_info: dict[str, Any] = {"compressed": False, "status": "NOOP"}

        try:
            from src.context.compression_prompt import get_compression_prompt
            from src.llm.profiles import Profile
            from src.llm.request import LLMRequest
            from src.llm.service import llm_service

            response = await llm_service.complete(
                LLMRequest(
                    profile=Profile.CONTEXT_COMPRESSION,
                    messages=[
                        *[self._coerce_message(message) for message in to_compress],
                        Message(
                            role=MessageRole.USER,
                            content=(
                                "First, reason in your scratchpad. Then generate only one "
                                "<state_snapshot>...</state_snapshot> summary for the prior conversation. "
                                "Be incredibly dense with information and omit filler."
                            ),
                        ),
                    ],
                    tools=None,
                    system_prompt=get_compression_prompt(),
                )
            )
            summary = str(response.content or "").strip()
            if not summary:
                result_messages, result_info = self._truncate_messages(messages, split_point)
                result_info.update({"status": "FAILED_EMPTY_SUMMARY", "fallback_from": "llm"})
            else:
                if "<state_snapshot" not in summary:
                    summary = f"<state_snapshot>\n{summary}\n</state_snapshot>"

                summary_user_msg = Message(
                    role=MessageRole.USER,
                    content=summary,
                )
                summary_ack_msg = Message(
                    role=MessageRole.ASSISTANT,
                    content="Got it. Thanks for the additional context!",
                )

                compressed = (
                    head_messages
                    + [summary_user_msg, summary_ack_msg]
                    + to_preserve
                )

                original_tokens = self.estimate_messages_tokens(messages)
                new_tokens = self.estimate_messages_tokens(compressed)
                if new_tokens >= original_tokens:
                    result_messages, result_info = self._truncate_messages(messages, split_point)
                    result_info.update({"status": "FAILED_INFLATED_TOKEN_COUNT", "fallback_from": "llm"})
                else:
                    result_messages = compressed
                    result_info = {
                        "compressed": True,
                        "status": "COMPRESSED",
                        "method": "llm",
                        "original_count": len(messages),
                        "compressed_count": len(compressed),
                        "summarized_count": len(to_compress),
                        "original_tokens": original_tokens,
                        "new_tokens": new_tokens,
                        "token_reduction": original_tokens - new_tokens,
                    }

        except Exception as e:
            logger.warning(f"LLM compression failed: {e}, falling back to truncation", exc_info=True)
            result_messages, result_info = self._truncate_messages(messages, split_point)
            from src.core.errors import EmptyResponseError

            if isinstance(e, EmptyResponseError):
                # 空响应 = 模型没给出摘要，与 provider 故障区分（状态语义供 UI/测试断言）
                result_info.update({"status": "FAILED_EMPTY_SUMMARY", "fallback_from": "llm"})
            else:
                result_info.update({"status": "FAILED_PROVIDER_ERROR", "fallback_from": "llm", "error": str(e)})

        if compact_hook_runner is not None:
            token_reduction = result_info.get("token_reduction", 0)
            await compact_hook_runner.run_post(len(messages), len(result_messages), token_reduction)

        # 截断兜底也算 compressed=True：管家仍应基于压缩前原文沉淀。
        if (
            janitor_coara is not None
            and janitor_history_snapshot is not None
            and result_info.get("compressed")
        ):
            from src.coara.workspace_protocol import schedule_janitor_parallel_with_compress

            schedule_janitor_parallel_with_compress(
                janitor_coara, original_history=janitor_history_snapshot
            )

        return result_messages, result_info

    def _coerce_message(self, message: Message | dict[str, Any]) -> Message:
        if isinstance(message, Message):
            return message
        role = message.get("role", MessageRole.USER)
        if isinstance(role, str):
            try:
                role = MessageRole(role)
            except ValueError:
                role = MessageRole.USER
        return Message(
            role=role,
            content=message.get("content", ""),
            tool_calls=message.get("tool_calls"),
            tool_call_id=message.get("tool_call_id"),
            name=message.get("name"),
            reasoning_content=message.get("reasoning_content"),
            provider_wire_blocks=message.get("provider_wire_blocks"),
        )

    def _truncate_messages(
        self,
        messages: list[Message | dict[str, Any]],
        split_point: int,
    ) -> tuple[list[Message | dict[str, Any]], dict[str, Any]]:
        """Simple truncation fallback when LLM compression fails."""
        head_boundary = self._find_head_boundary(messages)
        # 中间/尾部的环境种子同样保留（与 compress_with_llm 语义一致）
        stray_seed = [m for m in messages[head_boundary:] if self._is_env_seed(m)]
        preserve_tail = [m for m in messages[split_point:] if not self._is_env_seed(m)]
        to_preserve = list(messages[:head_boundary]) + stray_seed + preserve_tail

        original_tokens = self.estimate_messages_tokens(messages)
        new_tokens = self.estimate_messages_tokens(to_preserve)

        return to_preserve, {
            "compressed": True,
            "method": "truncation",
            "original_count": len(messages),
            "compressed_count": len(to_preserve),
            "removed_count": len(messages) - len(to_preserve),
            "original_tokens": original_tokens,
            "new_tokens": new_tokens,
            "token_reduction": original_tokens - new_tokens,
        }

    async def maybe_compress_messages(
        self,
        messages: list[Message | dict[str, Any]],
        *,
        max_tokens: int | None = None,
        input_tokens: int | None = None,
        preserve_ratio: float | None = None,
        preserve_last_n: int | None = None,
        threshold: float | None = None,
        compact_hook_runner: Any | None = None,
        force: bool = False,
        janitor_coara: Any | None = None,
    ) -> tuple[list[Message | dict[str, Any]], dict[str, Any]]:
        """Apply compression when the input nears the context threshold.

        Uses count-based preservation (kimi-cli style, keep last N messages)
        by default; falls back to ratio-based if count is not set.

        ``force=True`` skips the threshold check — used when the provider has
        already rejected the payload as too long (reactive fallback).
        """
        window = self.resolve_context_window()
        budget = max_tokens or window.tokens
        threshold_ratio = self._normalize_ratio(threshold, self.compression_threshold)
        preserve = self._normalize_ratio(preserve_ratio, self.compression_preserve_ratio)
        preserve_n = preserve_last_n if preserve_last_n is not None else self.compression_preserve_last_n

        # Prefer caller-provided input_tokens (usually provider-reported + cheap
        # delta). Only estimate message tokens when we may actually compress —
        # under-threshold turns must not walk the whole history.
        if input_tokens is not None and input_tokens > 0:
            total_tokens = input_tokens
        else:
            total_tokens = self.estimate_messages_tokens(messages)
        usage_ratio = (total_tokens / budget) if budget > 0 else 0.0

        if not force and usage_ratio < threshold_ratio:
            return messages, {
                "compressed": False,
                "status": "NOOP",
                "reason": "under_threshold",
                "usage_ratio": usage_ratio,
                "threshold": threshold_ratio,
                "preserve_ratio": preserve,
                "preserve_last_n": preserve_n,
            }

        message_tokens = self.estimate_messages_tokens(messages)
        head_boundary = self._find_head_boundary(messages)
        split_point = self.find_compression_split_point(messages, preserve_ratio=preserve, preserve_last_n=preserve_n)
        compressible_tokens = self.estimate_messages_tokens(messages[head_boundary:split_point])
        if message_tokens <= 0 or (compressible_tokens / message_tokens) < self.min_compressible_fraction:
            return messages, {
                "compressed": False,
                "status": "NOOP",
                "reason": "below_min_compressible_fraction",
                "usage_ratio": usage_ratio,
                "threshold": threshold_ratio,
                "preserve_ratio": preserve,
                "preserve_last_n": preserve_n,
            }

        compressed, info = await self.compress_with_llm(
            messages,
            split_point=split_point,
            preserve_ratio=preserve,
            preserve_last_n=preserve_n,
            compact_hook_runner=compact_hook_runner,
            janitor_coara=janitor_coara,
        )
        info.setdefault("usage_ratio", usage_ratio)
        info.setdefault("threshold", threshold_ratio)
        info.setdefault("preserve_ratio", preserve)
        info.setdefault("preserve_last_n", preserve_n)
        # 被替代范围（Phase 1 会话事件溯源归档用；仅压缩成功时有效）
        info.setdefault("split_point", split_point)
        return compressed, info


context_window_manager = ContextWindowManager()
