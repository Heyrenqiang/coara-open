"""LLM turn message preparation: compression, context guard."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.coara.injections.snapshot_injector import SnapshotInjector
from src.coara.turn_completion import _await_interruptible
from src.context.window import context_window_manager
from src.core.types import Message, MessageRole
from src.todos.loop import load_session_todos, read_todo_loop_state


@dataclass(slots=True)
class LlmTurnPrepareResult:
    turn_messages: list[Message]
    system_prompt: str
    llm_input_summary: dict[str, Any]
    guard: Any


async def prepare_messages_for_llm_turn(
    coara,
    *,
    iteration: int,
    signal: Any,
    compact_hook_runner: Any,
) -> LlmTurnPrepareResult:
    """Build system prompt, compress history if needed, evaluate context guard."""
    from src.core.logger import logger

    system_prompt = coara._build_system_prompt()
    from src.coara.workspace_switch_history import (
        close_unmatched_tool_calls,
        sanitize_dangling_tool_tail,
    )

    # Drop trailing dangling assistants, then close any mid-history orphans
    # (e.g. truncated tool_calls followed by a recovery reminder / user "继续").
    sanitize_dangling_tool_tail(coara.message_history)
    closed, closed_bg_delegate = close_unmatched_tool_calls(
        coara.message_history,
        content="[未执行] 上一轮工具调用缺少结果，已自动闭合以便继续对话。",
    )
    if closed:
        logger.warning(f"Closed {closed} unmatched tool_call(s) before LLM turn")
    if closed_bg_delegate:
        logger.debug(f"Closed {closed_bg_delegate} background delegate tool_call(s) (design transient)")
    turn_messages = list(coara.message_history)

    llm_input_summary = {
        "system_prompt": system_prompt,
        "messages": coara._serialize_messages_for_trace(turn_messages),
        "tool_count": len(coara._get_tool_definitions_for_llm()),
    }

    active_turn = getattr(coara, "_active_turn", None)
    coara._emit_trace(
        "llm_turn_start",
        f"Starting model turn {iteration}",
        payload={
            "iteration": iteration,
            "history_messages": len(turn_messages),
            "tool_count": llm_input_summary["tool_count"],
            "provider": coara.provider.name,
            "model": coara.model_name,
            "llm_input": llm_input_summary,
            "turn_id": getattr(active_turn, "turn_id", None),
        },
    )

    from src.llm.profiles import Profile
    from src.llm.service import llm_service

    compression = llm_service.resolve(Profile.CONTEXT_COMPRESSION)
    context_window = compression.provider.get_context_window(compression.model)
    input_tokens = coara._resolve_context_input_tokens(system_prompt, turn_messages)
    original_turn_messages = list(turn_messages)
    turn_messages, compression_info = await _await_interruptible(
        context_window_manager.maybe_compress_messages(
            turn_messages,  # type: ignore[arg-type]
            max_tokens=context_window,
            input_tokens=input_tokens,
            compact_hook_runner=compact_hook_runner,
            janitor_coara=coara,
        ),
        signal,
    )

    timing = getattr(coara, "_turn_timing", None)
    if compression_info.get("compressed") and timing is not None:
        timing.record_compression()

    if compression_info.get("compressed"):
        # 会话事件溯源：压缩被替代历史归档为 compaction 事件（底稿保留、影子化）。
        # 与 persist 边界 sync_history 不冲突：归档事件 seq 落在公共前缀之后，
        # 投影时被 shadow 截除，审计视图（skip_shadowed=False）仍完整可重建
        from src.session_log.archive import archive_compressed_history_for

        archive_compressed_history_for(
            coara, original=original_turn_messages, compressed=turn_messages, info=compression_info
        )
        orig_count = compression_info.get("original_count")
        comp_count = compression_info.get("compressed_count")
        orig_tokens = compression_info.get("original_tokens")
        new_tokens = compression_info.get("new_tokens")
        coara._emit_trace(
            "context_compressed",
            f"Context compressed: {orig_count} -> {comp_count} messages",
            payload=compression_info,
        )
        logger.info(f"Context compressed: {orig_count} -> {comp_count} messages ({orig_tokens} -> {new_tokens} tokens)")
        coara.message_history = [context_window_manager._coerce_message(m) for m in turn_messages]
        snapshot_messages = SnapshotInjector.build_snapshots(
            read_todo_loop_state(coara.workspace_dir, coara.session_id),
            load_session_todos(coara.workspace_dir, coara.session_id),
            coara=coara,
        )
        coara.message_history.extend(snapshot_messages)
        turn_messages = list(coara.message_history)
        # 用户可见提示：自动压缩是隐式触发的，用户需要知道历史已变
        method = str(compression_info.get("method") or "llm")
        method_label = "LLM 摘要" if method == "llm" else "截断兜底"
        coara.message_history.append(
            Message(
                role=MessageRole.USER,
                content=(
                    f"<系统消息>对话过长，已自动压缩（{method_label}）："
                    f"{orig_count} → {comp_count} 条消息。"
                    f"被替代的历史已归档进事件日志，未物理丢失。"
                ),
            )
        )
        note = getattr(coara, "note_history_rewrite", None)
        if callable(note):
            note()
        if timing is not None:
            timing.finish_compression()
        # History shrank; re-resolve once for the guard (snapshot cleared by callers on overflow).
        input_tokens = coara._resolve_context_input_tokens(system_prompt, turn_messages)

    guard = coara._evaluate_context_guard(
        system_prompt,
        turn_messages,
        max_tokens=context_window,
        used_tokens=input_tokens,
    )
    if guard.should_warn:
        coara._emit_trace("context_warning", guard.reason, level="warning")
        logger.warning(guard.reason)

    # 情境等 conversation 之后的模块：只挂本轮 LLM 输入末尾，不写回 message_history。
    # 仅面向用户的主会话注入（identity.user_facing）——子智能体与 janitor 等维护
    # 主体不面向任何对话端，注入「当前对话来自 X 端」只会误导（如让它误以为能
    # send_file 给用户）。delegate_depth 判据不够：janitor 是临时 CoaraBase，
    # delegate_depth 恒为 0。
    _identity = getattr(coara, "identity", None)
    _user_facing = bool(getattr(_identity, "user_facing", getattr(coara, "delegate_depth", 0) == 0))
    if getattr(coara, "inject_environment_seed", True) and _user_facing:
        from src.coara.injections.context_modules import build_suffix_messages

        suffix = build_suffix_messages(getattr(coara, "workspace_dir", "") or "")
        if suffix:
            turn_messages = list(turn_messages) + suffix

    return LlmTurnPrepareResult(
        turn_messages=turn_messages,
        system_prompt=system_prompt,
        llm_input_summary=llm_input_summary,
        guard=guard,
    )
