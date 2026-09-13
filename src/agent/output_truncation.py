"""Detect LLM output truncation and recover via tool materialization or limited continuation."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.core.message_tags import system_reminder
from src.core.types import Message, MessageRole
from src.llm.provider import LLMResponse

if TYPE_CHECKING:
    from src.coara.base import CoaraBase


TRUNCATED_FINISH_REASONS = frozenset({"max_tokens", "length", "model_length"})

MATERIALIZE_TOOL_NAMES = frozenset({"write", "edit", "shell"})


class OutputTruncationPolicy(StrEnum):
    FORCE_TOOL = "force_tool"
    AUTO_CONTINUE = "auto_continue"
    WARN_ONLY = "warn_only"


@dataclass(slots=True)
class OutputTruncationSettings:
    enabled: bool = True
    default_policy: OutputTruncationPolicy = OutputTruncationPolicy.FORCE_TOOL
    max_continuations: int = 3
    long_text_threshold_chars: int = 4000
    output_token_ratio: float = 0.98


@dataclass(slots=True)
class TruncationRecoveryState:
    continuation_count: int = 0
    recovery_attempts: int = field(default=0)


@dataclass(slots=True)
class TruncationRecoveryResult:
    handled: bool
    action: str  # none | continue | warn_finish
    policy: OutputTruncationPolicy | None = None
    warning: str = ""


def load_output_truncation_settings(raw: dict[str, Any] | None) -> OutputTruncationSettings:
    data = raw or {}
    policy_raw = str(data.get("default_policy") or OutputTruncationPolicy.FORCE_TOOL).strip().lower()
    try:
        policy = OutputTruncationPolicy(policy_raw)
    except ValueError:
        policy = OutputTruncationPolicy.FORCE_TOOL
    max_continuations = int(data.get("max_continuations", 3))
    threshold = int(data.get("long_text_threshold_chars", 4000))
    ratio = float(data.get("output_token_ratio", 0.98))
    return OutputTruncationSettings(
        enabled=bool(data.get("enabled", True)),
        default_policy=policy,
        max_continuations=max(0, max_continuations),
        long_text_threshold_chars=max(0, threshold),
        output_token_ratio=min(1.0, max(0.5, ratio)),
    )


def is_output_truncated(
    response: LLMResponse,
    requested_max_tokens: int,
    *,
    output_token_ratio: float = 0.98,
) -> bool:
    reason = (response.finish_reason or "").strip().lower()
    if response.has_tool_calls:
        # When tool_calls are present, only trust explicit finish_reason signals.
        # Token-ratio heuristics are unreliable here (output_tokens includes
        # tool_call arguments tokens), and truncated arguments JSON must not be
        # silently dispatched to tools.
        return reason in TRUNCATED_FINISH_REASONS
    if reason in TRUNCATED_FINISH_REASONS:
        return True
    if requested_max_tokens <= 0:
        return False
    output_tokens = int((response.usage or {}).get("output_tokens") or 0)
    if output_tokens <= 0:
        return False
    return output_tokens >= int(requested_max_tokens * output_token_ratio)


def agent_can_materialize_to_disk(coara: CoaraBase) -> bool:
    visible = coara._get_visible_tool_definitions()
    names = {str(item.get("name") or "") for item in visible}
    return bool(names & MATERIALIZE_TOOL_NAMES)


def resolve_effective_policy(
    settings: OutputTruncationSettings,
    *,
    coara: CoaraBase,
    content_length: int,
) -> OutputTruncationPolicy:
    policy = settings.default_policy
    if policy == OutputTruncationPolicy.FORCE_TOOL and not agent_can_materialize_to_disk(coara):
        return OutputTruncationPolicy.AUTO_CONTINUE
    if content_length < settings.long_text_threshold_chars and policy == OutputTruncationPolicy.FORCE_TOOL:
        # Short truncated replies (e.g. planning blurbs) still benefit from continuation.
        return OutputTruncationPolicy.AUTO_CONTINUE
    return policy


def suggest_draft_path(coara: CoaraBase, *, iteration: int) -> Path:
    session = getattr(coara, "session_id", "session") or "session"
    safe_session = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(session))[:48]
    draft_dir = Path(coara.workspace_dir) / ".coara" / "drafts"
    return draft_dir / f"truncated_{safe_session}_iter{iteration}.md"


def build_force_tool_reminder(
    *,
    coara: CoaraBase,
    response: LLMResponse,
    requested_max_tokens: int,
    iteration: int,
) -> str:
    draft_path = suggest_draft_path(coara, iteration=iteration)
    output_tokens = (response.usage or {}).get("output_tokens")
    lines = [
        "上一轮模型输出因达到 max_tokens 上限被截断。",
        f"- finish_reason: {response.finish_reason or 'unknown'}",
        f"- requested_max_tokens: {requested_max_tokens}",
    ]
    if output_tokens is not None:
        lines.append(f"- output_tokens: {output_tokens}")
    lines.extend(
        [
            "",
            "禁止在对话回复中继续输出大段正文。",
            "请把已有内容视为草稿，改用 write 或 shell 分段写入文件，例如：",
            f"  {draft_path}",
            "然后按 todo 继续下一部分。",
            "本轮回复只允许：文件路径列表 + 下一步计划（不超过 300 字）。",
        ]
    )
    return system_reminder("\n".join(lines))


def settings_from_config(config: OutputTruncationSettings | Any) -> OutputTruncationSettings:
    """Build runtime settings from ``OutputTruncationConfig`` or dict."""
    if isinstance(config, OutputTruncationSettings):
        return config
    if hasattr(config, "model_dump"):
        return load_output_truncation_settings(config.model_dump())
    if isinstance(config, dict):
        return load_output_truncation_settings(config)
    return OutputTruncationSettings()


def build_auto_continue_message(*, continuation_index: int, max_continuations: int) -> str:
    return system_reminder(
        "\n".join(
            [
                f"上一轮输出因 max_tokens 被截断（续写 {continuation_index}/{max_continuations}）。",
                "请从断点精确继续，不要重复已输出内容。",
                "若正文很长，优先 write/shell 写入文件而不是继续在回复里输出。",
            ]
        )
    )


def build_tool_call_truncation_reminder() -> str:
    """Reminder injected when a tool_call's arguments JSON was truncated.

    The truncated tool_call is skipped this round; the LLM must reduce overall
    output volume (thinking + content + all tool_call arguments share the
    max_tokens budget) and re-issue the call. No concrete numbers are mentioned
    because the budget is shared and config-dependent.
    """
    return system_reminder(
        "\n".join(
            [
                "上一轮输出达到 max_tokens 上限被截断，导致其中一个工具调用的参数 JSON 不完整，"
                "相关工具调用本轮未被执行。",
                "max_tokens 由思考+正文+工具参数共享，超限截尾部。",
                "",
                "请降低本轮输出总量后重新发起工具调用：",
                "- 若要写入较大文件，请拆分为多次 write 调用，每次只写入文件的一部分内容；",
                "- 不要原样重试上一次被截断的工具调用，否则会再次被截断。",
            ]
        )
    )


def format_truncation_recovery_notice(
    *,
    policy: OutputTruncationPolicy,
    finish_reason: str | None,
    requested_max_tokens: int,
    output_tokens: int | None = None,
    draft_path: Path | str | None = None,
) -> str:
    """User-facing one-line notice for CLI/Dashboard when recovery kicks in."""
    reason = finish_reason or "unknown"
    token_bit = f"output_tokens={output_tokens}" if output_tokens is not None else f"max_tokens={requested_max_tokens}"
    if policy == OutputTruncationPolicy.FORCE_TOOL:
        draft = f" → {draft_path}" if draft_path else ""
        return f"模型输出已截断（{reason}, {token_bit}），已切换落盘模式{draft}"
    if policy == OutputTruncationPolicy.AUTO_CONTINUE:
        return f"模型输出已截断（{reason}, {token_bit}），正在自动续写…"
    return f"模型输出已截断（{reason}, {token_bit}），内容可能不完整"


def build_warn_only_banner(response: LLMResponse, requested_max_tokens: int) -> str:
    output_tokens = (response.usage or {}).get("output_tokens")
    parts = [
        "模型输出因 max_tokens 上限被截断，以下内容可能不完整。",
        f"finish_reason={response.finish_reason or 'unknown'}",
        f"requested_max_tokens={requested_max_tokens}",
    ]
    if output_tokens is not None:
        parts.append(f"output_tokens={output_tokens}")
    return "\n".join(parts) + "\n\n"


def try_output_truncation_recovery(
    *,
    coara: CoaraBase,
    response: LLMResponse,
    settings: OutputTruncationSettings,
    state: TruncationRecoveryState,
    requested_max_tokens: int,
    iteration: int,
) -> TruncationRecoveryResult:
    if not settings.enabled:
        return TruncationRecoveryResult(handled=False, action="none")
    if not is_output_truncated(response, requested_max_tokens, output_token_ratio=settings.output_token_ratio):
        return TruncationRecoveryResult(handled=False, action="none")

    if response.has_tool_calls:
        # Truncated tool_call arguments: skip dispatch and inject a clear reminder.
        # Never fall back to warn_finish here — truncated tool_calls must not be
        # dispatched (would trigger misleading "missing param" errors). The
        # stagnation guard and turn iteration cap terminate the turn if the LLM
        # keeps producing truncated output.
        #
        # Critical: the orchestrator already appended the assistant row with
        # tool_calls. Close them with synthetic tool_results BEFORE the user
        # reminder, or the next provider call gets HTTP 400 (orphan tool_call_ids).
        from src.coara.workspace_switch_history import close_unmatched_tool_calls

        state.recovery_attempts += 1
        close_unmatched_tool_calls(
            coara.message_history,
            content=(
                "[未执行] 工具调用参数因 max_tokens 截断而不完整，已跳过。"
                "请缩小本轮输出后重试，不要原样重试上一次的内容。"
            ),
        )
        coara.message_history.append(
            Message(
                role=MessageRole.USER,
                content=build_tool_call_truncation_reminder(),
            )
        )
        if state.recovery_attempts > settings.max_continuations:
            warning = build_warn_only_banner(response, requested_max_tokens)
            warning += "(已达到输出截断恢复上限，工具调用仍未恢复，请人工介入。)\n\n"
            return TruncationRecoveryResult(
                handled=True,
                action="continue",
                policy=OutputTruncationPolicy.WARN_ONLY,
                warning=warning,
            )
        return TruncationRecoveryResult(
            handled=True,
            action="continue",
            policy=OutputTruncationPolicy.WARN_ONLY,
        )

    content_len = len(response.content or "")
    policy = resolve_effective_policy(settings, coara=coara, content_length=content_len)
    state.recovery_attempts += 1

    if settings.max_continuations > 0 and state.recovery_attempts > settings.max_continuations:
        warning = build_warn_only_banner(response, requested_max_tokens)
        warning += f"(已达到输出截断恢复上限 {settings.max_continuations} 次。)\n\n"
        return TruncationRecoveryResult(
            handled=True,
            action="warn_finish",
            policy=policy,
            warning=warning,
        )

    if policy == OutputTruncationPolicy.WARN_ONLY:
        warning = build_warn_only_banner(response, requested_max_tokens)
        return TruncationRecoveryResult(
            handled=True,
            action="warn_finish",
            policy=policy,
            warning=warning,
        )

    if policy == OutputTruncationPolicy.AUTO_CONTINUE:
        state.continuation_count += 1
        coara.message_history.append(
            Message(
                role=MessageRole.USER,
                content=build_auto_continue_message(
                    continuation_index=state.continuation_count,
                    max_continuations=settings.max_continuations,
                ),
            )
        )
        return TruncationRecoveryResult(handled=True, action="continue", policy=policy)

    # force_tool
    coara.message_history.append(
        Message(
            role=MessageRole.USER,
            content=build_force_tool_reminder(
                coara=coara,
                response=response,
                requested_max_tokens=requested_max_tokens,
                iteration=iteration,
            ),
        )
    )
    return TruncationRecoveryResult(handled=True, action="continue", policy=policy)
