"""Agent turn loop: LLM iterations, tool execution, stagnation guards."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from src.agent.loop import TurnStagnationGuard
from src.agent.output_truncation import (
    OutputTruncationPolicy,
    TruncationRecoveryState,
    format_truncation_recovery_notice,
    suggest_draft_path,
    try_output_truncation_recovery,
)
from src.coara.content_policy_recovery import complete_turn_with_content_policy_recovery
from src.coara.stagnation import build_error_signature
from src.coara.turn_completion import CoaraRunCancelledError, PlanSubmittedError, _await_interruptible
from src.coara.turn_loop import apply_tool_results_to_history, begin_user_turn, prepare_messages_for_llm_turn
from src.coara.turn_timing import TurnTimingRecorder
from src.context.window import context_window_manager
from src.core.errors import ContextWindowExceededError, LLMError
from src.core.logger import log_agent_error, logger
from src.core.message_tags import system_info, system_reminder
from src.core.types import CoaraStatus, Message, MessageRole, SubagentStatus
from src.todos.loop import read_todo_loop_state
from src.todos.turn_control import TodoLoopState, TurnAction, TurnState

# todo_incomplete (etc.) may CONTINUE after a text-only reply. If the model
# returns empty content with no tool_calls, continuing without a nudge stacks
# blank assistant rows (serialized as "(empty)") until Ctrl+C / iteration cap.
_MAX_EMPTY_TEXT_CONTINUES = 2

# _max_tool_iterations 置 None 时的兜底硬上限：默认 1200 有上限，
# 子智能体/工作空间会话传 None 即裸奔，LLM 每轮产不同参数的工具调用会无限循环。
_UNBOUNDED_TOOL_ITERATION_CAP = 5000

# 整轮回滚只删 message_history，磁盘副作用不回滚——这些写盘工具的目标文件
# 在回滚时汇总成副作用注记，让下一轮模型知道磁盘已被本回合改过的工具动过
_DISK_MUTATING_TOOL_NAMES = frozenset({"write", "edit", "delete"})


@dataclass(slots=True)
class TurnRunContext:
    turn_runtime: Any
    turn_history_start: int
    timing: TurnTimingRecorder
    show_tool_summary: bool


async def force_compress_history(coara: Any, *, signal: Any | None = None) -> dict[str, Any] | None:
    """压缩当前会话历史（手动 /compact 与上下文溢出兜底共用）。

    流程：LLM 压缩（失败回退截断）→ 被替代历史归档进事件日志 → 替换历史 →
    追加 todo 快照 → 清 usage 快照（记录已与历史不符）。
    压缩 LLM 启动的同时异步派 janitor（压缩前完整历史快照；单飞/冷却合并，
    不连触发）。沉淀在后台做，不阻塞压缩与回合。
    返回压缩 info；无可压内容或被打断返回 None。
    单飞：已有压缩进行中时直接返回 None，防并发覆盖历史。
    """
    if getattr(coara, "_compress_inflight", False):
        logger.info("Compression already in flight, skipping duplicate request")
        return None
    coara._compress_inflight = True
    try:
        info, original = await _force_compress_history_impl(coara, signal=signal)
    finally:
        coara._compress_inflight = False
    return info


async def _force_compress_history_impl(
    coara: Any, *, signal: Any | None = None
) -> tuple[dict[str, Any] | None, list[Any] | None]:
    """force_compress_history 的实际实现（单飞锁保护内层）。"""
    from src.coara.injections.snapshot_injector import SnapshotInjector
    from src.llm.profiles import Profile
    from src.llm.service import llm_service
    from src.todos.loop import load_session_todos

    compression = llm_service.resolve(Profile.CONTEXT_COMPRESSION)
    context_window = compression.provider.get_context_window(compression.model)
    original = list(coara.message_history)
    compress_coro = context_window_manager.maybe_compress_messages(
        original,
        max_tokens=context_window,
        compact_hook_runner=coara.compact_hook_runner,
        force=True,
        janitor_coara=coara,
    )
    if signal is not None:
        compressed, info = await _await_interruptible(compress_coro, signal)
    else:
        compressed, info = await compress_coro
    if not info.get("compressed"):
        return None, None
    # 会话事件溯源：被替代的历史归档进事件日志（compaction/*），压缩不再物理丢失
    from src.session_log.archive import archive_compressed_history_for

    archive_compressed_history_for(coara, original=original, compressed=compressed, info=info)
    coara.message_history = [context_window_manager._coerce_message(m) for m in compressed]
    snapshot_messages = SnapshotInjector.build_snapshots(
        read_todo_loop_state(coara.workspace_dir, coara.session_id),
        load_session_todos(coara.workspace_dir, coara.session_id),
        coara=coara,
    )
    coara.message_history.extend(snapshot_messages)
    # Absolute turn_history_start is stale after rewrite; rollback keeps this prefix.
    note = getattr(coara, "note_history_rewrite", None)
    if callable(note):
        note()
    # The recorded snapshot no longer matches history after compression.
    coara._llm_usage_snapshot.clear()
    return info, original


async def _force_compress_after_context_overflow(coara, *, signal: Any) -> bool:
    """Reactive fallback: provider rejected the payload as too long.

    The proactive guard works off provider-reported usage plus cheap deltas,
    so an overflow rejection is rare (non-reporting endpoints, tool-result
    bursts, silently smaller windows). Force-compress history once and let
    the caller retry the model turn. Returns False when nothing could be
    compressed away.
    """
    info = await force_compress_history(coara, signal=signal)
    if info is None:
        return False
    coara._emit_trace(
        "context_compressed_after_overflow",
        f"Provider rejected payload as too long; compressed: "
        f"{info.get('original_count')} -> {info.get('compressed_count')} messages",
        level="warning",
        payload=info,
    )
    logger.warning(
        f"Context window exceeded at provider; force-compressed "
        f"{info.get('original_count')} -> {info.get('compressed_count')} messages and retrying"
    )
    # 用户可见提示：溢出兜底压缩是自动触发的，用户需要知道历史已变
    method = str(info.get("method") or "llm")
    method_label = "LLM 摘要" if method == "llm" else "截断兜底"
    coara.message_history.append(
        Message(
            role=MessageRole.USER,
            content=(
                f"<系统消息>对话过长，已自动压缩（{method_label}）："
                f"{info.get('original_count')} → {info.get('compressed_count')} 条消息。"
                f"被替代的历史已归档进事件日志，未物理丢失。"
            ),
        )
    )
    return True


def _sanitize_error_brief(exc: BaseException) -> str:
    """从异常提取一行可读摘要：剥堆栈/errno/URL，保留类型名+前段文本，120 字截短。"""
    raw = " ".join(str(exc).split())
    # 去掉 [Errno 10061] / (winerror 10061) 这类平台错误码段
    import re as _re

    raw = _re.sub(r"\[(errno|winerror)[^\]]*\]", "", raw, flags=_re.IGNORECASE)
    raw = _re.sub(r"https?://\S+", "", raw)
    raw = " ".join(raw.split()).strip(" :")
    type_name = type(exc).__name__
    if not raw:
        return type_name
    if len(raw) > 120:
        raw = raw[:120].rstrip() + "…"
    # 类型名未体现在文本里时补上，便于辨认是哪类错
    if type_name.lower() not in raw.lower():
        raw = f"{type_name}: {raw}"
    return raw


def _user_facing_llm_error(exc: BaseException) -> str:
    """把原始 LLM 异常映射为用户可理解的中文指引（保持 Error: 前缀兼容前端识别）。"""
    text = str(exc).lower()
    # brief 只保留可读的异常摘要：剥离内部英文技术串（堆栈/errno/URL），
    # 超 120 字符截短，避免把 ConnectionError: [Errno 10061] ... 这类原文甩给用户
    brief = _sanitize_error_brief(exc)

    def _f(guidance: str) -> str:
        return f"Error: {guidance}"

    if "apikeyerror" in type(exc).__name__.lower() or "api key" in text or "apikey" in text:
        return _f("API 密钥无效或未配置。输入 /model --add 重新添加密钥，或 /model 切换到其他可用模型。")
    if "401" in text or "unauthorized" in text:
        return _f(
            "API 密钥鉴权失败（401）。密钥可能已过期或填错。输入 /model --add 重新添加，或 /model 切换到其他模型。"
        )
    # 额度/配额优先于笼统 403：厂商常用 403 + usage limit / access_terminated
    if (
        any(
            k in text
            for k in (
                "usage limit",
                "quota",
                "access_terminated",
                "5-hour",
                "rate allotment",
                "余额不足",
                "欠费",
                "额度",
            )
        )
        or "402" in text
        or "insufficient" in text
    ):
        return _f("模型额度已用尽。请等待配额重置、升级套餐，或输入 /model 切换到其他模型。")
    if "403" in text or "forbidden" in text:
        return _f("访问被拒绝（403）。当前 API 密钥可能没有该模型的访问权限。请检查密钥权限，或输入 /model 切换模型。")
    if "429" in text or "rate limit" in text or "too many" in text:
        return _f("请求频率受限（429）。模型服务当前限流，请稍后重试。")
    if "超时" in text or "timeout" in text or "timed out" in text:
        if "total_timeout" in text or "总" in text:
            return _f(f"模型响应超时：{brief}\n可通过 config.yaml 的 llm.total_timeout_seconds 调整总超时。")
        return _f(f"网络请求超时：{brief}\n请检查网络连接后重试。")
    if "connect" in text and ("refused" in text or "error" in text or "failed" in text):
        return _f(
            f"网络连接失败：{brief}\n请检查网络连接。若本机开着代理（系统代理或 HTTPS_PROXY），"
            "确认代理仍在运行；已关闭代理工具的话，重启内核让连接按直连重建。"
        )
    if "404" in text or "not found" in text:
        return _f(f"模型或端点不存在（404）：{brief}\n请检查模型名称是否正确，或输入 /model 查看可用模型。")
    if "500" in text or "502" in text or "503" in text or "504" in text or "internal" in text:
        return _f("模型服务暂时不可用（服务端错误）。请稍后重试。如果持续失败，可能是服务商故障。")
    if "tool_call" in text or "orphan" in text or "tool_calls" in text:
        return _f(f"会话上下文异常：{brief}\n建议执行 /compact 压缩会话，或 /new 开新会话后继续。")
    if "emptyresponse" in type(exc).__name__.lower():
        return _f("模型返回了空内容。请重试，如果持续出现请尝试切换模型。")
    # 通用兜底：未命中任何已知类别时也要返回可读指引——隐式 None 会让
    # 上游 yield None 炸 chunk.lstrip()（test_llm_failure_turn_records_failed）
    return _f(f"模型调用失败：{brief}")


def _user_facing_turn_error(exc: BaseException) -> str:
    """回合级异常（含非 LLMError）→ 用户可见一行，避免裸 exception 文案。"""
    from src.core.errors import LLMError

    if isinstance(exc, LLMError) or any(
        k in str(exc).lower() for k in ("error code:", "status code", "apistatus", "usage limit", "quota")
    ):
        return _user_facing_llm_error(exc)
    brief = _sanitize_error_brief(exc)
    if not brief or brief.lower() in {"error", "failed", "exception"}:
        return "Error: 回合失败。请重试，或输入 /model 切换模型。"
    return f"Error: 回合异常中断：{brief}"


async def run_turn_loop(
    coara,
    *,
    content: str,
    image_blocks: list[dict[str, Any]] | None,
    ctx: TurnRunContext,
) -> AsyncIterator[str]:
    """Main native function-calling tool loop for one user turn (called under process lock)."""
    turn_runtime = ctx.turn_runtime
    turn_history_start = ctx.turn_history_start
    timing = ctx.timing
    show_tool_summary = ctx.show_tool_summary

    coara._raise_if_interrupted(turn_runtime)
    coara._llm_debug_user_input = content
    session_log = getattr(coara, "_session_log", None)
    if session_log is not None:
        # source 原样落盘（web-flow / web-<subject> 等模块来源不能归一化塌缩）
        session_log.record_turn_start(
            turn_runtime.turn_id,
            source=str(getattr(coara, "_active_turn_source", "") or "").strip().lower(),
        )
    # 回合 in-flight 标记落盘：进程被杀（无 finally 机会）时标记保留，
    # 下次恢复会话据此注入「上次回合未完成」注记；正常收尾在 finally 清除
    try:
        await asyncio.to_thread(coara._mark_turn_in_flight, turn_runtime.turn_id)
    except Exception:
        logger.debug("Failed to write turn in-flight marker", exc_info=True)
    content = await begin_user_turn(coara, content, image_blocks=image_blocks)
    timing.record_input_prep()
    coara._raise_if_interrupted(turn_runtime)

    stagnation_guard = TurnStagnationGuard()
    truncation_recovery_state = TruncationRecoveryState()
    empty_text_continues = 0
    coara.turn_controller.reset_turn()
    # 上回合若中断在 park 工具执行与批次收尾之间，结束语标记可能残留，防带进本回合
    coara._todo_park_message = None
    # deliver 残留不在此重置：flow 节点在 _run_node 激活前自清（flow_coordinator），
    # 且 subagent.process_message 底层 run_turn_loop 可能跑多次（队列/continuation），
    # 此处全局重置会把上一批 deliver 的结果抹掉。残留风险仅限主会话复用，主会话
    # deliver 已被 deliver 工具限定 flow 上下文（不可达）。
    coara.loop_detector.clear()
    coara.shell_failure_streak_guard.clear()
    # 本回合已成功执行的工具副作用记录：写盘工具记 (工具名, 目标文件)，
    # 其余工具只记名字；未预期异常整轮回滚时据此注入磁盘副作用注记（#331）
    turn_file_effects: list[tuple[str, str]] = []
    turn_other_effects: list[str] = []
    # 本回合 delegate 结果（含失败）：(task_id, description, outcome)
    # 回滚会抹掉 tool 结果，须靠此清单把可 resume 的断点 id 留给下一轮。
    turn_delegate_effects: list[tuple[str, str, str]] = []
    # 显式失败标记：finally 判定回合成败读此变量，不依赖 sys.exc_info()
    # （async generator 关闭路径下 exc_info 不可靠，失败回合会误记 completed）
    turn_failure: BaseException | None = None
    # 收尾尾窗计数：正常收尾 return 前的 yield 会让出事件循环，跟话恰在此刻
    # 入队则绕过 :879 的消化检查；return 前再查一次队列续循环消化，到顶强制
    # 收尾走 leftover 兜底——防持续跟话把回合拖成无限。
    _final_drain_continues = 0
    _final_drain_max = 10
    # 兜底硬上限：调用方若把 _max_tool_iterations 置 None（默认 1200 有上限，
    # 子智能体/工作空间会话传 None 即裸奔），LLM 每轮产不同参数的工具调用
    # （绕开 LoopDetector）且每次有进展（绕开 StagnationGuard）时会无限循环。
    _iteration_cap = (
        coara._max_tool_iterations if coara._max_tool_iterations is not None else _UNBOUNDED_TOOL_ITERATION_CAP
    )
    try:
        iteration_stream = range(1, _iteration_cap + 1)
        for iteration in iteration_stream:
            coara._raise_if_interrupted(turn_runtime)
            timing.begin_iteration(iteration)
            _pending_continuation = coara.drain_continuation_inputs()
            if _pending_continuation:
                # 注入点开新段：以本批注入顺序的最后一条交互端为本段归属端（靠后的赢）。
                # 这是输出路由的分界点——此后产生的输出归属最新注入端。
                _seg_source = ""
                _seg_channel = ""
                for _ci in reversed(_pending_continuation):
                    _s = str(getattr(_ci, "source", "") or "").strip()
                    if _s in ("cli", "web", "matrix", "cli-attached"):
                        _seg_source = _s
                        _seg_channel = str(getattr(_ci, "channel_id", "") or "").strip()
                        break
                _tid = str(getattr(getattr(coara, "_active_turn", None), "turn_id", "") or "")
                # 段只在「用户端注入」时开：接续队列里的系统回投（子智能体结果等）
                # 不换段、不绑 launch source。接续标签与段归属是两套逻辑。
                if _seg_source:
                    _new_seg = coara._segments.open(
                        _seg_source,
                        turn_id=_tid,
                        mid_turn=True,
                        channel_id=_seg_channel,
                    )
                    coara._stamp_segment_model(_new_seg)
                    _rec_seg = getattr(getattr(coara, "_session_log", None), "record_segment_open", None)
                    if _rec_seg is not None:
                        _rec_seg(seq=_new_seg.seq, source=_new_seg.source, turn_id=_tid, mid_turn=True)
                    # 真正注入上下文这一刻才更新「最近一次输入端」。入队时不更新（入队≠注入）。
                    coara._last_user_input_source = _seg_source
                # mid-turn 远端接续输入（Matrix/Web ingress defer 路径）没有 turn
                # 上下文：按队列项自带的 deferred_remote_ctx 恢复——同回合多端跟话
                # 各跟各的审批通道，禁止单槽后写覆盖先写。
                from src.coara.turn_context import set_turn_context
                from src.core.message_tags import (
                    continuation_followup_display_line,
                    continuation_subagent_display_text,
                    format_continuation_for_history,
                )

                for _ci in _pending_continuation:
                    _deferred = getattr(_ci, "deferred_remote_ctx", None)
                    _ci_src = str(getattr(_ci, "source", "") or "").strip().lower()
                    if not _deferred or _ci_src not in ("matrix", "web"):
                        continue
                    _room_id, _send_text, _interaction_channel, _source = _deferred[:4]
                    _actor = str(_deferred[4] if len(_deferred) > 4 else "") or ""
                    coara._turn_context_tokens.append(
                        set_turn_context(
                            _room_id,
                            send_text=_send_text,
                            interaction_channel=_interaction_channel,
                            source=_source or str(getattr(coara, "_active_turn_source", "") or "cli"),
                            actor=_actor,
                        )
                    )
                # Compat: legacy single-slot stamp if an item arrived without per-item ctx
                if coara._deferred_remote_ctx and any(
                    str(getattr(t, "source", "") or "").strip().lower() in ("matrix", "web")
                    and not getattr(t, "deferred_remote_ctx", None)
                    for t in _pending_continuation
                ):
                    _deferred = coara._deferred_remote_ctx
                    _room_id, _send_text, _interaction_channel, _source = _deferred[:4]
                    _actor = str(_deferred[4] if len(_deferred) > 4 else "") or ""
                    coara._turn_context_tokens.append(
                        set_turn_context(
                            _room_id,
                            send_text=_send_text,
                            interaction_channel=_interaction_channel,
                            source=_source or str(getattr(coara, "_active_turn_source", "") or "cli"),
                            actor=_actor,
                        )
                    )
                # Slot consumed for this drain batch (items already carry their own stamp).
                coara.clear_deferred_remote_ctx()

                from src.coara.turn_context import get_turn_channel, get_turn_channel_id

                # 审批/回投 origin：优先本批开段的用户端（已恢复 deferred ctx 后的通道）。
                _origin_src = _seg_source or str(getattr(coara, "_active_turn_source", "") or "").strip()
                if _origin_src:
                    coara.session_origin = {
                        "source": _origin_src,
                        "channel_id": get_turn_channel_id(),
                    }
                    coara._origin_remote_channel = get_turn_channel()

                _user_texts: list[str] = []
                _user_sources: list[str] = []
                _subagent_texts: list[str] = []
                _subagent_sources: list[str] = []
                _turn_id = str(getattr(getattr(coara, "_active_turn", None), "turn_id", "") or "")
                _active_source = str(getattr(coara, "_active_turn_source", "") or "")
                for _ci in _pending_continuation:
                    # 接续正文裸文本套 <接续输入>（不再现包来源标签；落带裸文本）。
                    _ci_text = _ci.text
                    # 本批是在「回合尚未结束」的迭代头 drain 进历史的 → 接续。
                    # 若 LLM 思考完直接退出、队列项没被本轮消化，那是 leftover，
                    # 走收尾后的新回合路径，不经此处、也不打 <接续输入>。
                    _wrapped = format_continuation_for_history(
                        _ci_text,
                        is_mid_turn=True,
                    )
                    if _ci.image_blocks:
                        from src.utils.multimodal_content import build_user_content

                        # 图片接续：文本包 <接续输入>（纯图时省略文本块），图片块跟随组多模态。
                        _content = build_user_content(
                            _wrapped if _ci_text.strip() else "",
                            _ci.image_blocks,
                        )
                    else:
                        _content = _wrapped
                    coara.message_history.append(Message(role=MessageRole.USER, content=_content))
                    if _ci.image_blocks:
                        # 与新建图片回合一致的提示：让模型直接据图作答。
                        coara.message_history.append(
                            Message(
                                role=MessageRole.USER,
                                content=system_reminder(
                                    "本条消息附带图片，请直接据图作答；图片已在上下文中，不要再用工具获取或截图。"
                                ),
                            )
                        )
                    # 跟话来源 ≠ 当前回合来源：手机/Web 注入 background 唤醒回合时，
                    # 绝不能继承 source=background，否则 CLI 会标成 [后台] 并双显。
                    _emit_source = str(_ci.source or "").strip()
                    if not _emit_source:
                        _emit_source = (
                            "cli-attached" if _active_source == "background" else (_active_source or "cli-attached")
                        )
                    _sub_display = continuation_subagent_display_text(_ci_text)
                    if _sub_display:
                        # 子智能体结果回显归位：子智能体活动（diff + 最终结果）全部
                        # 固定归属 delegate 调用时父会话当前注入段——锚点由注入端
                        # 携带的 agent_origin（段快照）给出，不随运行中用户换端漂移。
                        _agent_origin = str(getattr(_ci, "agent_origin", "") or "").strip()
                        _sub_emit_source = _agent_origin or _emit_source
                        _subagent_texts.append(_sub_display)
                        _subagent_sources.append(_sub_emit_source)
                        # 显示面投递不在这里：子智能体结果由 delegate 的帧路由统一
                        # 投给发起端（含未命中时按端兜底）。注入路径只负责把结果写进
                        # history 供主模型继续推理——在注入里再直推一次端，就是同一条
                        # 结果的第二次上屏（手机端会把它当主会话正文铺出来）。
                    _display = continuation_followup_display_line(
                        _ci_text,
                        image_blocks=_ci.image_blocks,
                    )
                    if not _display:
                        continue
                    _user_texts.append(_display)
                    _user_sources.append(_emit_source)
                    # 正式用户行：与开局 user_message 同形（CLI/Web/历史同款样式）。
                    # continuation=True 只标记注入时机，不是旁路通道。
                    coara._emit_trace(
                        "conversation_message",
                        _display,
                        payload={
                            "role": "user",
                            "content": _display,
                            "turn_id": _turn_id,
                            "source": _emit_source,
                            "continuation": True,
                        },
                    )
                    coara._emit_trace(
                        "user_message",
                        _display[:200],
                        payload={
                            "content": _display,
                            "source": _emit_source,
                            "turn_id": _turn_id,
                            "continuation": True,
                        },
                    )
                coara._emit_trace(
                    "continuation_input_injected",
                    f"Injected {len(_pending_continuation)} continuation input(s) mid-turn",
                    payload={
                        "count": len(_pending_continuation),
                        "session_id": str(getattr(coara, "session_id", "") or ""),
                        "user_texts": _user_texts,
                        "user_sources": _user_sources,
                        "subagent_texts": _subagent_texts,
                        "subagent_sources": _subagent_sources,
                    },
                )
            prep = await prepare_messages_for_llm_turn(
                coara,
                iteration=iteration,
                signal=turn_runtime.signal,
                compact_hook_runner=coara.compact_hook_runner,
            )
            timing.record_context_prep()
            turn_messages = prep.turn_messages
            system_prompt = prep.system_prompt
            llm_input_summary = prep.llm_input_summary
            guard = prep.guard
            if guard.should_block:
                coara.status = CoaraStatus.FAILED
                coara._emit_trace(
                    "context_blocked", guard.reason, level="error", payload={"llm_input": llm_input_summary}
                )
                # 上下文压缩阻断：确定性失败，不可续跑；记失败标记使
                # session_log 终态与 CoaraStatus.FAILED / error 级 trace 同口径
                turn_failure = LLMError(f"context blocked: {guard.reason}")
                coara._turn_failure = turn_failure
                yield f"Context limit reached. {guard.reason}"
                return

            # 流式续传循环：mid-stream 断开且已收到内容时 保留已聚合内容自动补全
            _resume_attempted = False
            while True:
                try:
                    timing.begin_llm()
                    coara._llm_debug_turn_meta = {
                        "turn_iteration": iteration,
                        "messages_in_llm_window": len(turn_messages),
                        "user_input": getattr(coara, "_llm_debug_user_input", ""),
                        "turn_id": turn_runtime.turn_id,
                        "session_id": coara.session_id,
                    }
                    try:
                        response = await complete_turn_with_content_policy_recovery(
                            coara,
                            system_prompt,
                            turn_messages,
                            turn_runtime.signal,
                            llm_input_summary=llm_input_summary,
                        )
                    except ContextWindowExceededError:
                        # Provider says the payload is too long despite the guard —
                        # force-compress history and retry this model turn once.
                        if not await _force_compress_after_context_overflow(coara, signal=turn_runtime.signal):
                            raise
                        turn_messages = list(coara.message_history)
                        coara._llm_debug_turn_meta["messages_in_llm_window"] = len(turn_messages)
                        response = await complete_turn_with_content_policy_recovery(
                            coara,
                            system_prompt,
                            turn_messages,
                            turn_runtime.signal,
                            llm_input_summary=llm_input_summary,
                        )
                    timing.record_llm()
                    break  # 成功 跳出续传循环
                except LLMError as exc:
                    # 流式续传：mid-stream 断开且已收到部分内容时 保留已聚合内容
                    # 追加「请继续」自动补全 而非直接判死。只续传一次防循环。
                    stream_delta = bool(getattr(exc, "stream_received_delta", False))
                    partial_content = str(getattr(exc, "partial_content", "") or "").strip()
                    if stream_delta and partial_content and not _resume_attempted:
                        _resume_attempted = True
                        logger.info(f"Stream interrupted with {len(partial_content)} chars received; attempting resume")
                        coara._emit_trace(
                            "llm_stream_resume",
                            f"Stream interrupted, resuming from {len(partial_content)} chars",
                            level="warning",
                            payload={"partial_chars": len(partial_content)},
                        )
                        coara.message_history.append(Message(role=MessageRole.ASSISTANT, content=partial_content))
                        coara.message_history.append(
                            Message(
                                role=MessageRole.USER,
                                content=system_info(
                                    "上一轮回复因网络中断而不完整。请从上次中断的位置继续，不要重复已输出的内容。"
                                ),
                            )
                        )
                        turn_messages = list(coara.message_history)
                        continue  # 回到 while 重试
                    # 不可续传或续传也失败：走原始判死路径
                    coara._emit_trace("llm_error", str(exc), level="error", payload={"llm_input": llm_input_summary})
                    log_agent_error(
                        f"LLM error: {exc}",
                        exc_info=not str(exc).startswith("Provider content policy"),
                        coara_id=coara.identity.coara_id,
                        coara_name=coara.identity.name,
                        session_id=coara.session_id,
                        workspace_dir=coara.workspace_dir,
                        event="llm_error",
                    )
                    partial_usage = getattr(exc, "partial_usage", None)
                    if isinstance(partial_usage, dict) and partial_usage:
                        from src.runtime.usage_attribution import attribution_from_coara

                        coara._emit_trace(
                            "llm_turn_partial",
                            "LLM turn failed after partial response; usage accounted",
                            level="warning",
                            payload={
                                "session_id": coara.session_id,
                                "turn_id": turn_runtime.turn_id,
                                "model": getattr(coara, "model_name", None) or "",
                                "provider": getattr(getattr(coara, "provider", None), "name", None) or "",
                                "iteration": iteration,
                                "usage": partial_usage,
                                **attribution_from_coara(coara),
                            },
                        )
                    brief = " ".join(str(exc).split())[:160]
                    coara.message_history.append(
                        Message(
                            role=MessageRole.USER,
                            content=system_info(
                                f"上一轮模型请求失败（{type(exc).__name__}）：{brief}\n"
                                "该轮未产出回复，上面的用户消息尚未被处理。"
                                "若用户继续发问，请基于上下文正常作答，不要假设上一轮已成功。"
                            ),
                        )
                    )
                    coara.status = CoaraStatus.FAILED
                    turn_failure = exc
                    # 记录原始异常供 delegate 失败分类（瞬时 vs 确定）判定用；
                    # 仅子智能体（delegate 上下文）会读取，主会话不消费此字段。
                    coara._turn_failure = exc
                    yield _user_facing_llm_error(exc)
                    return

            coara._raise_if_interrupted(turn_runtime)

            # New LLM response — reset streamed-delta counter so
            # _unsent_assistant_text slices against THIS response, not the
            # cumulative total from prior iterations.
            coara._streamed_assistant_chars = 0

            # Emit llm_turn_complete with full output
            llm_output_summary = {
                "content": response.content,
                "tool_calls": [{"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in response.tool_calls],
                "finish_reason": response.finish_reason,
                "usage": response.usage,
            }
            from src.runtime.usage_attribution import attribution_from_coara

            attribution = attribution_from_coara(coara)
            coara._emit_trace(
                "llm_turn_complete",
                "Model turn completed",
                payload={
                    "iteration": iteration,
                    "has_tool_calls": response.has_tool_calls,
                    "session_id": coara.session_id,
                    "turn_id": turn_runtime.turn_id,
                    "model": getattr(coara, "model_name", None) or "",
                    "provider": getattr(getattr(coara, "provider", None), "name", None) or "",
                    "llm_output": llm_output_summary,
                    **attribution,
                },
            )

            # 会话事件溯源：消息事件统一走 persist 边界 sync_history（防双写）；
            # 此处不记 assistant 事件

            assistant_message_index = len(coara.message_history)
            assistant_content, reasoning_content = response.assistant_storage_fields()
            provider_wire_blocks = None
            from src.llm.endpoints import preserves_anthropic_thinking_wire
            from src.llm.message_content import extract_thinking_wire_blocks

            if preserves_anthropic_thinking_wire(coara.provider.base_url):
                provider_wire_blocks = extract_thinking_wire_blocks(response)
                reasoning_content = None
            assistant_message = Message(
                role=MessageRole.ASSISTANT,
                content=assistant_content,
                tool_calls=response.tool_calls or None,
                reasoning_content=reasoning_content,
                provider_wire_blocks=provider_wire_blocks,
            )
            coara.message_history.append(assistant_message)

            truncation_settings = coara._get_output_truncation_settings()
            truncation_recovery = try_output_truncation_recovery(
                coara=coara,
                response=response,
                settings=truncation_settings,
                state=truncation_recovery_state,
                requested_max_tokens=coara._resolved_output_max_tokens(),
                iteration=iteration,
            )
            if truncation_recovery.handled and truncation_recovery.action == "continue":
                stagnation_guard.reset()
                policy = truncation_recovery.policy or OutputTruncationPolicy.FORCE_TOOL
                draft_path = suggest_draft_path(coara, iteration=iteration)
                output_tokens = (response.usage or {}).get("output_tokens")
                user_notice = format_truncation_recovery_notice(
                    policy=policy,
                    finish_reason=response.finish_reason,
                    requested_max_tokens=coara._resolved_output_max_tokens(),
                    output_tokens=int(output_tokens) if output_tokens is not None else None,
                    draft_path=draft_path if policy == OutputTruncationPolicy.FORCE_TOOL else None,
                )
                coara._emit_trace(
                    "output_truncation_recovery",
                    user_notice,
                    payload={
                        "iteration": iteration,
                        "policy": policy.value,
                        "finish_reason": response.finish_reason,
                        "continuation_count": truncation_recovery_state.continuation_count,
                        "recovery_attempts": truncation_recovery_state.recovery_attempts,
                        "requested_max_tokens": coara._resolved_output_max_tokens(),
                        "output_tokens": output_tokens,
                        "draft_path": str(draft_path),
                        "user_notice": user_notice,
                    },
                    level="warning",
                )
                timing.finish_iteration()
                continue

            # 计划模式下豁免待办续循环：submit 提交方案后回合应立刻收尾等用户审，
            # 不因计划前列的未完成待办而再走一遍 LLM（空转/park 都浪费）。喂空 todo
            # 状态让决策走「无待办」分支直接退出。
            todo_state = (
                TodoLoopState() if coara.is_plan_mode else read_todo_loop_state(coara.workspace_dir, coara.session_id)
            )
            turn_decision = coara.turn_controller.decide_after_llm(
                TurnState(
                    has_tool_calls=response.has_tool_calls,
                    todo=todo_state,
                    assistant_text_chars=len((response.content or "").strip()),
                    assistant_text=(response.content or "").strip(),
                )
            )

            if turn_decision.action == TurnAction.CONTINUE and not response.has_tool_calls:
                visible = (response.content or "").strip()
                if not visible:
                    stored = assistant_content
                    if isinstance(stored, str):
                        visible = stored.strip()
                    elif isinstance(stored, list):
                        from src.llm.message_content import visible_text_from_blocks

                        visible = (visible_text_from_blocks(stored) or "").strip()

                if not visible:
                    # Empty text-only continue: drop the blank assistant (it becomes
                    # wire "(empty)" spam), nudge once, then hard-stop if it repeats.
                    if coara.message_history and coara.message_history[-1] is assistant_message:
                        coara.message_history.pop()
                    empty_text_continues += 1
                    coara._emit_trace(
                        "empty_reply_continue",
                        f"Empty model reply while continuing ({turn_decision.reason})",
                        level="warning",
                        payload={
                            "iteration": iteration,
                            "reason": turn_decision.reason,
                            "empty_streak": empty_text_continues,
                        },
                    )
                    if empty_text_continues >= _MAX_EMPTY_TEXT_CONTINUES:
                        stop_message = "模型连续返回空回复，已停止本轮。待办若未完成，请继续发指令推进。"
                        coara.message_history.append(Message(role=MessageRole.ASSISTANT, content=stop_message))
                        coara._emit_trace(
                            "empty_reply_loop_stop",
                            stop_message,
                            level="error",
                            payload={
                                "iteration": iteration,
                                "reason": turn_decision.reason,
                                "empty_streak": empty_text_continues,
                            },
                        )
                        coara._emit_final_turn_traces(
                            stop_message,
                            completed_message="Stopped: consecutive empty model replies",
                        )
                        timing.finish_iteration()
                        yield stop_message
                        return

                    coara.message_history.append(
                        Message(
                            role=MessageRole.USER,
                            content=system_info(
                                "上一条模型回复为空。请继续推进未完成待办："
                                "立刻调用工具，或输出实质性进展（不要再返回空内容）。"
                            ),
                        )
                    )
                    timing.finish_iteration()
                    continue

                empty_text_continues = 0
                # Stagnation guard only fires on repeated tool errors; a text-only
                # turn has no error signature, so just reset the error streak.
                stagnation_guard.reset()
                # Flush this iteration's text before continuing: without this the
                # reply is stored in history but never streamed, so remote channels
                # (Matrix) silently lose it while the CLI still shows it via traces.
                _continue_preview = coara._unsent_assistant_text(response.content or "").strip()
                if _continue_preview:
                    yield _continue_preview if _continue_preview.endswith("\n") else f"{_continue_preview}\n"
                # Todo incomplete: inject the still-open todo list. The model
                # sees what is left and decides — push forward, update status,
                # or close out with a longer explanation.
                if turn_decision.reason == "todo_incomplete":
                    from src.todos.loop import (
                        format_todo_incomplete_reminder,
                        load_session_todos,
                    )

                    coara.message_history.append(
                        Message(
                            role=MessageRole.USER,
                            content=format_todo_incomplete_reminder(
                                load_session_todos(coara.workspace_dir, coara.session_id)
                            ),
                        )
                    )
                coara._emit_trace(
                    "turn_continue",
                    f"Continuing internal loop: {turn_decision.reason}",
                    payload={
                        "iteration": iteration,
                        "reason": turn_decision.reason,
                        "todo_stall_streak": coara.turn_controller.todo_stall_streak,
                    },
                )
                timing.finish_iteration()
                continue

            if not response.has_tool_calls:
                # Foreground delegate rendezvous — agentic wait + soft reminder.
                # When the LLM has no more tool calls but async foreground
                # subagents are still running, the turn does NOT hard-block:
                # the first arrival injects a one-shot <系统提醒> so the model
                # can choose delegate(action="wait") or explicitly release;
                # the second arrival releases the delegates (they keep running,
                # late results route via on_foreground_delegate_done).
                #
                # The `or coara._continuation_inputs` clause covers the race
                # where a delegate completes *during* the LLM call (after drain
                # at iteration start, before this check): its result is queued
                # but has_pending_foreground_delegates() already cleaned it up
                # → without this clause the turn would exit and defer the
                # result to the next turn.
                has_pending_fg = coara.has_pending_foreground_delegates()
                if has_pending_fg or coara._continuation_inputs:
                    # Flush before continuing: the model already wrote text this
                    # iteration (it thought the turn was done). Without this the
                    # text stays in history but is never streamed — remote
                    # channels lose it while the CLI still shows it via traces.
                    _fg_preview = coara._unsent_assistant_text(response.content or "").strip()
                    if _fg_preview:
                        yield _fg_preview if _fg_preview.endswith("\n") else f"{_fg_preview}\n"

                    if coara._continuation_inputs:
                        # 有排队的接续输入（LLM 调用期间到达的子智能体结果或
                        # 用户新消息）→ 先消化，下一迭代 drain，不与提醒/放行抢跑
                        timing.finish_iteration()
                        continue

                    # 前台子智能体的软提醒/放行只属于 has_pending_fg：仅有分离工具时
                    # 走下方独立 wait 分支，不注入空提醒、不发空 release trace
                    if has_pending_fg:
                        if not turn_runtime.fg_release_reminded:
                            turn_runtime.fg_release_reminded = True
                            coara.message_history.append(
                                Message(
                                    role=MessageRole.USER,
                                    content=system_reminder(
                                        "还有前台子智能体在跑：\n"
                                        f"{coara.pending_foreground_delegate_descriptions()}\n"
                                        '等结果用 delegate(action="wait")；不需要就明确放弃，结果稍后到达。'
                                        "汇合前不要宣称任务完成。"
                                    ),
                                )
                            )
                            coara._emit_trace(
                                "turn_continue",
                                "Foreground delegates pending; soft reminder injected",
                                payload={
                                    "pending_delegates": coara.pending_foreground_delegate_descriptions(),
                                },
                            )
                            timing.finish_iteration()
                            continue

                        released = coara.release_pending_foreground_delegates()
                        coara._emit_trace(
                            "turn_continue",
                            "Foreground delegates released at turn exit",
                            payload={"released_delegates": released},
                        )
                        # fall through to normal turn end

                if turn_decision.reason in {
                    "todo_incomplete_stalled",
                    "todo_incomplete_duplicate_reply",
                }:
                    stalled_visible = (response.content or "").strip()
                    coara._emit_trace(
                        "todo_stall_stop",
                        (
                            "Stopped: duplicate continue text"
                            if turn_decision.reason == "todo_incomplete_duplicate_reply"
                            else "Stopped todo_incomplete spin: no todo progress across short replies"
                        ),
                        level="warning",
                        payload={
                            "iteration": iteration,
                            "todo_stall_streak": coara.turn_controller.todo_stall_streak,
                            "had_visible_text": bool(stalled_visible),
                            "reason": turn_decision.reason,
                        },
                    )
                    if not stalled_visible:
                        # Drop the blank assistant row (same as empty_reply continue path).
                        if coara.message_history and coara.message_history[-1] is assistant_message:
                            coara.message_history.pop()
                        stop_message = "待办仍有未完成项，但连续回复无进展，已停止本轮空转。请继续发指令推进。"
                        coara.message_history.append(Message(role=MessageRole.ASSISTANT, content=stop_message))
                        coara.status = CoaraStatus.IDLE
                        timing.finish_iteration()
                        coara._emit_final_turn_traces(
                            stop_message,
                            completed_message="Stopped: todo incomplete stall",
                        )
                        yield stop_message
                        return

                final_content = response.content or ""
                warning_prefix = ""
                if truncation_recovery.handled and truncation_recovery.action == "warn_finish":
                    policy = truncation_recovery.policy or OutputTruncationPolicy.WARN_ONLY
                    output_tokens = (response.usage or {}).get("output_tokens")
                    user_notice = format_truncation_recovery_notice(
                        policy=policy,
                        finish_reason=response.finish_reason,
                        requested_max_tokens=coara._resolved_output_max_tokens(),
                        output_tokens=int(output_tokens) if output_tokens is not None else None,
                    )
                    coara._emit_trace(
                        "output_truncation_recovery",
                        user_notice,
                        payload={
                            "iteration": iteration,
                            "policy": policy.value,
                            "finish_reason": response.finish_reason,
                            "recovery_attempts": truncation_recovery_state.recovery_attempts,
                            "requested_max_tokens": coara._resolved_output_max_tokens(),
                            "output_tokens": output_tokens,
                            "user_notice": user_notice,
                            "action": "warn_finish",
                        },
                        level="warning",
                    )
                    warning_prefix = truncation_recovery.warning
                    final_content = warning_prefix + final_content
                remainder = coara._unsent_assistant_text(response.content or "")
                if warning_prefix:
                    yield warning_prefix + remainder if remainder else warning_prefix
                elif remainder:
                    yield remainder
                # 尾窗跟话（:859 检查之后、return 之前入队）：不结束回合，续循环消化
                if coara._continuation_inputs and _final_drain_continues < _final_drain_max:
                    _final_drain_continues += 1
                    coara._emit_trace(
                        "turn_continue",
                        "Late continuation at final turn boundary; continuing",
                        payload={"iteration": iteration, "final_drain_continues": _final_drain_continues},
                    )
                    timing.finish_iteration()
                    continue
                coara.status = CoaraStatus.IDLE
                timing.finish_iteration()
                coara._emit_final_turn_traces(final_content)
                logger.info(f"coara completed: {coara.identity.name}")
                return

            if response.content:
                coara._emit_trace(
                    "thinking_progress",
                    "LLM thinking",
                    payload={"content_preview": response.content[:200]},
                )
                # Channel A: show interleaved assistant text before tools run (Phase B / D3).
                preview = coara._unsent_assistant_text(response.content or "").strip()
                if preview:
                    yield preview if preview.endswith("\n") else f"{preview}\n"

            # Execute tools
            # 治理逻辑元数据化：使用 is_owner_context 元数据替代外部传入的 is_owner 参数
            timing.begin_tools()
            tool_names = ", ".join(tc.name for tc in response.tool_calls[:3])
            if len(response.tool_calls) > 3:
                tool_names += f" 等{len(response.tool_calls)}个"
            coara._turn_phase.set("tool_running", tool_names)
            # 中断收场时执行器把已有结果（已完成/已取消）写入该列表，
            # 由下方 except 如实入史，避免整批被统一闭合为「已取消」
            interrupted_executions: list[Any] = []
            try:
                executions = await _await_interruptible(
                    coara.tool_executor.execute(
                        coara,
                        response.tool_calls,
                        coara.identity.is_owner_context,
                        signal=turn_runtime.signal,
                        interrupt_sink=interrupted_executions,
                    ),
                    turn_runtime.signal,
                    join_on_cancel=True,
                )
            except (asyncio.CancelledError, CoaraRunCancelledError):
                if interrupted_executions:
                    # 防护：入史失败不得顶替原本的 CancelledError /
                    # CoaraRunCancelledError，否则正常中断会被降级成整轮回滚
                    try:
                        _batch_effects, coara._recent_progress_signatures = apply_tool_results_to_history(
                            coara,
                            executions=interrupted_executions,
                            assistant_message_index=assistant_message_index,
                            assistant_message=assistant_message,
                            recent_progress_signatures=set(coara._recent_progress_signatures),
                        )
                        # 与正常完成路径同口径登记本批已执行工具的副作用，
                        # 供中断收尾注记如实列出（磁盘改动不随打断撤销）
                        _collect_turn_effects(interrupted_executions, turn_file_effects, turn_other_effects)
                        _collect_delegate_effects(interrupted_executions, turn_delegate_effects, coara=coara)
                    except Exception:
                        logger.warning(
                            "Failed to record interrupted tool results to history; "
                            "falling through to interrupted-turn closure",
                            exc_info=True,
                        )
                raise
            timing.record_tools(executions)
            coara._turn_phase.set("processing")

            # 在入史（可能抛错走整轮回滚）之前先登记本批成功工具的副作用
            _collect_turn_effects(executions, turn_file_effects, turn_other_effects)
            _collect_delegate_effects(executions, turn_delegate_effects, coara=coara)

            batch_effects, coara._recent_progress_signatures = apply_tool_results_to_history(
                coara,
                executions=executions,
                assistant_message_index=assistant_message_index,
                assistant_message=assistant_message,
                recent_progress_signatures=set(coara._recent_progress_signatures),
            )
            made_progress = batch_effects.made_progress

            for execution in executions:
                result_metadata = execution.result.metadata or {}

                if execution.result.is_error:
                    logger.debug(f"Tool error: {execution.tool_call.name} -> {execution.result.content}")
                    if show_tool_summary:
                        # 上屏只取错误首行（一句人话）；多行细节（如 edit 附带的原文摘录）
                        # 属模型通道，留在 ToolResult.content 里给 LLM 自纠，不进端上输出流
                        error_line = str(execution.result.content or "").split("\n", 1)[0].strip()
                        if len(error_line) > 200:
                            error_line = error_line[:200] + "…"
                        yield f"✗ {execution.tool_call.name} 报错: `{error_line}`\n"

                # 用户主动取消（如按 ESC）—— 优雅终止当前 turn
                if getattr(execution.result, "is_cancelled", False):
                    cancel_msg = "当前操作已被用户取消。"
                    should_continue_after_cancel = result_metadata.get("continue_after_cancel")
                    suppress_cancel_feedback = bool(result_metadata.get("suppress_cancel_feedback"))

                    if should_continue_after_cancel:
                        coara._emit_trace(
                            "user_cancelled",
                            "User cancelled the operation, continuing for cleanup",
                            payload={"tool": execution.tool_call.name, "continue_after_cancel": True},
                        )
                        if not suppress_cancel_feedback:
                            coara.message_history.append(Message(role=MessageRole.ASSISTANT, content=cancel_msg))
                            yield cancel_msg
                    else:
                        coara.status = CoaraStatus.IDLE
                        coara._emit_trace(
                            "user_cancelled",
                            "User cancelled the operation",
                            payload={
                                "tool": execution.tool_call.name,
                                "suppress_cancel_feedback": suppress_cancel_feedback,
                            },
                        )
                        if not suppress_cancel_feedback:
                            coara.message_history.append(Message(role=MessageRole.ASSISTANT, content=cancel_msg))
                            yield cancel_msg
                        return

                # kimi-cli style: flush finished tool to terminal history
                if not execution.result.is_error and not getattr(execution.result, "is_cancelled", False):
                    is_background_delegate = (
                        execution.tool_call.name == "delegate"
                        and (execution.result.metadata or {}).get("mode") == "background"
                    )
                    if not is_background_delegate and show_tool_summary:
                        summary = coara._format_tool_summary(execution.tool_call.name, execution.tool_call.arguments)
                        yield f"✓ {summary}\n"
                        # Terminal diff after the tool line：进程内嵌 CLI 前端（daily
                        # curator 等 daemon 内嵌会话）经前端钩子直渲；daemon 内核为
                        # no-op 前端时这是 no-op，attach CLI 的 diff 走输出帧通道
                        # （EndRegistry.deliver → cli-attached sender → queue_diff_frame）。
                        display_blocks = execution.result.display
                        if display_blocks:
                            from src.coara.tool_output.pipeline import render_terminal_blocks

                            render_terminal_blocks(display_blocks)
                if show_tool_summary:
                    from src.coara.frontend import get_frontend

                    echo_line = get_frontend().flush_interaction_echo()
                    if echo_line:
                        yield echo_line

            # 后台 delegate 已通过 BackgroundAgentManager 异步启动并立即返回。
            # 这里从 message_history 中剥离其 tool_call（见 tool_results.py），
            # 并让父进程继续推进——不结束 turn，父 LLM 可继续分析或启动更多任务。
            background_results = [ex for ex in executions if ex.tool_call.id in batch_effects.background_delegate_ids]
            execution_summary = [f"{e.tool_call.name}({'err' if e.result.is_error else 'ok'})" for e in executions]
            logger.info(
                f"Background check: {len(background_results)} background delegate(s) found, "
                f"executions=[{', '.join(execution_summary)}]"
            )
            logger.debug(f"Execution metadata: {[(e.tool_call.name, e.result.metadata) for e in executions]}")
            if background_results:
                # tool_results.py already stripped background delegate tool_calls
                # from assistant_message (same object in message_history).
                if assistant_message_index < len(coara.message_history):
                    assistant_history_message = coara.message_history[assistant_message_index]
                    if not assistant_history_message.tool_calls and not assistant_history_message.content:
                        del coara.message_history[assistant_message_index]

                non_background_executions = [
                    ex for ex in executions if ex.tool_call.id not in batch_effects.background_delegate_ids
                ]
                if not non_background_executions:
                    # 所有 tool_calls 都是后台 delegate：不结束 turn，而是让父进程继续运行。
                    # 向 message_history 注入一条轻量提醒，使下一轮 LLM 知道已有后台任务在跑，
                    # 避免重复委托；父进程可以继续分析、继续对话或启动更多后台任务。
                    reminder = _build_background_delegate_reminder(background_results)
                    if reminder:
                        coara.message_history.append(Message(role=MessageRole.USER, content=system_info(reminder)))
                    coara._emit_trace(
                        "turn_continue",
                        "Background delegates launched; parent continues",
                        payload={"background_tasks": [ex.result.metadata for ex in background_results]},
                    )
                    timing.finish_iteration()
                    continue

                # 仍有非后台执行需要继续本轮处理
                coara._emit_trace(
                    "turn_continue",
                    "Background delegates launched alongside foreground work",
                    payload={"background_tasks": [ex.result.metadata for ex in background_results]},
                )

            # deliver 已交付最终结果（flow 节点）：立即正常结束迭代循环。
            # 本批工具结果已入史；不再进入下一轮 LLM，物理上不可能再有后续工具调用
            if getattr(coara, "_final_deliver_message", None) is not None:
                # 不在此清空：flow 节点在 process_message 返回后、经
                # flow_coordinator._run_node 读 subagent._final_deliver_message 提取
                # 结果——此处清空会让节点结果退化成最后一个 chunk（✓ 工具行）。
                # 残留风险仅限主会话复用，而 deliver 工具限定 flow 上下文（主会话不可达）。
                coara.status = CoaraStatus.IDLE
                timing.finish_iteration()
                coara._emit_final_turn_traces(
                    coara._final_deliver_message,
                    completed_message="Completed: result delivered",
                )
                return

            # todo(action="park") 一步收尾：结束语在 park 的 message 参数里，
            # 待办也已在工具内更新完——回合立即结束，不再进入下一轮 LLM
            # （仿 deliver 的即时收尾；message 未经流式通道，此处直接交付）
            park_message = getattr(coara, "_todo_park_message", None)
            if park_message is not None:
                coara._todo_park_message = None
                coara.message_history.append(Message(role=MessageRole.ASSISTANT, content=park_message))
                coara.status = CoaraStatus.IDLE
                timing.finish_iteration()
                coara._emit_final_turn_traces(
                    park_message,
                    completed_message="Completed: todos parked",
                )
                logger.info(f"coara completed (todos parked): {coara.identity.name}")
                yield park_message if park_message.endswith("\n") else f"{park_message}\n"
                return

            alert = stagnation_guard.record(
                made_progress=made_progress,
                error_signature=build_error_signature(executions),
            )
            if alert is not None:
                stop_message = coara._stop_for_stagnation(iteration, alert.feedback, alert.reason)
                # 停滞停止是失控保护（FAILED + error trace），终态同口径记 failed
                turn_failure = LLMError(f"stagnation stop: {alert.reason}")
                coara._turn_failure = turn_failure
                yield stop_message
                return
        stop_message = f"Stopped after reaching the maximum tool iterations ({_iteration_cap})."
        coara.message_history.append(Message(role=MessageRole.ASSISTANT, content=stop_message))
        coara.status = CoaraStatus.FAILED
        coara._emit_trace("iteration_limit", stop_message, level="error")
        # 迭代上限同属失控保护：终态记 failed，与状态机/trace 口径一致
        turn_failure = LLMError(stop_message)
        coara._turn_failure = turn_failure
        coara._emit_final_turn_traces(
            stop_message,
            completed_message="Iteration limit reached",
        )
        yield stop_message
    except PlanSubmittedError:
        # plan_mode(submit) 成功收尾（非打断）：submit 的 tool_call 已在 assistant
        # 消息里但无匹配结果。先给 plan_mode 补一条成功结果，再用统一闭合器把
        # 同批其余未闭合的 tool_call（若 LLM 同轮并发其它工具）标为「未执行」——
        # 不留悬空（provider 会因 orphan tool_call 拒掉下一轮请求）。不回滚历史、
        # 不注入打断文案，再让 finally 走正常回合收尾。
        from src.coara.workspace_switch_history import close_unmatched_tool_calls

        for _msg in reversed(coara.message_history):
            if getattr(_msg, "role", None) == MessageRole.ASSISTANT and getattr(_msg, "tool_calls", None):
                _plan_tcs = [tc for tc in _msg.tool_calls if tc.name == "plan_mode"]
                if _plan_tcs:
                    coara.message_history.append(
                        Message(
                            role=MessageRole.TOOL_RESULT,
                            tool_call_id=_plan_tcs[-1].id,
                            name="plan_mode",
                            content="计划已提交并展示给用户，回合结束。",
                        )
                    )
                break
        close_unmatched_tool_calls(
            coara.message_history,
            content="[未执行] 本回合因提交计划而结束，该工具未运行。",
        )
        # 回合结束信号与正常收尾一致：发 "completed"（attach/web 镜像与 workspace
        # registry 都以它为回合终点；旧 "turn_completed" 不在任何端 topic 白名单，
        # attach 收到不了，spinner/状态行会一直挂着）。计划正文已由
        # _publish_plan_review 经 EndRegistry chunk 通路展示，此处无需再带全文。
        coara._emit_final_turn_traces("", completed_message="Plan submitted for approval")
        return
    except CoaraRunCancelledError as exc:
        # Cancel any pending foreground delegates — the turn is gone.
        coara.cancel_all_pending_foreground_delegates()
        coara._emit_trace(
            "turn_interrupted",
            "Turn interrupted",
            level="warning",
            payload={"turn_id": turn_runtime.turn_id, "reason": exc.reason},
        )
        if exc.reason == "new_session":
            return
        if exc.reason and exc.reason.startswith("switch_workspace"):
            from src.coara.workspace_switch_history import strip_ws_switch_tail

            # Idempotent: ws(switch) already stripped the source tail; sanitize again
            # if the executor path skipped cleanup.
            strip_ws_switch_tail(coara.message_history)
            # 与 Ctrl+C / 异常回滚两条中断路径对齐：整轮抹除后本回合已执行的
            # 磁盘改动对模型彻底隐形，会误判磁盘未被改动。副作用注记 append 在
            # strip 边界之后（strip 只上溯到 ws 工具链），不会被二次 strip。
            side_effects_detail = _build_interrupt_side_effects_note(
                file_effects=turn_file_effects,
                other_tools=turn_other_effects,
                delegates=turn_delegate_effects,
            )
            if side_effects_detail:
                coara.message_history.append(Message(role=MessageRole.USER, content=system_info(side_effects_detail)))
            workspace_name = exc.reason.split(":", 1)[1] if ":" in exc.reason else "?"
            from src.coara.workspace_state import format_workspace_switch_message

            notification = format_workspace_switch_message(
                workspace_name,
                session_renewed=bool(getattr(exc, "session_renewed", False)),
                last_active=getattr(exc, "last_active", None),
            )
            yield f"[系统] {notification}"
            return
        # Ctrl+C / /stop: end the turn but keep this turn's messages so context continues.
        from src.coara.workspace_switch_history import finalize_interrupted_turn_history
        from src.tools.builtin.delegate.delegate import format_turn_interrupt_note

        finalize_interrupted_turn_history(coara.message_history)
        cancelled = getattr(coara, "_interrupt_cancelled_delegates", None) or []
        interrupt_note = format_turn_interrupt_note(cancelled)
        coara._interrupt_cancelled_delegates = []
        # 附上本回合已执行工具的副作用清单：打断不回滚磁盘改动，
        # 避免下一轮模型误判磁盘未被改动
        side_effects_detail = _build_interrupt_side_effects_note(
            file_effects=turn_file_effects,
            other_tools=turn_other_effects,
            delegates=turn_delegate_effects,
        )
        if side_effects_detail:
            interrupt_note = f"{interrupt_note}\n{side_effects_detail}"
        coara.message_history.append(Message(role=MessageRole.USER, content=system_info(interrupt_note)))
        # UI line stays short; full resume tip is in history for the next LLM turn.
        yield "[系统] 当前会话已打断。"
        return
    except (KeyboardInterrupt, SystemExit) as exc:
        turn_failure = exc
        raise
    except Exception as exc:
        turn_failure = exc
        coara._emit_trace(
            "turn_failed",
            "Turn failed before completion",
            level="error",
            payload={"turn_id": turn_runtime.turn_id, "error": str(exc)},
        )
        log_agent_error(
            f"Turn failed: {exc}",
            exc_info=True,
            coara_id=coara.identity.coara_id,
            coara_name=coara.identity.name,
            session_id=coara.session_id,
            workspace_dir=coara.workspace_dir,
            event="turn_failed",
            turn_id=turn_runtime.turn_id,
        )
        coara._rollback_partial_turn_history(turn_history_start)
        # 整轮回滚只删历史，磁盘副作用不回滚：留一条副作用摘要注记，
        # 让下一轮模型知道本回合已执行的工具改动仍然生效（与中断闭合注记同源）
        side_effects_note = _build_rollback_side_effects_note(
            exc,
            file_effects=turn_file_effects,
            other_tools=turn_other_effects,
            delegates=turn_delegate_effects,
        )
        if side_effects_note:
            coara.message_history.append(Message(role=MessageRole.USER, content=system_info(side_effects_note)))
        raise
    finally:
        # 成败判定用显式捕获的 turn_failure（async generator 关闭路径下
        # sys.exc_info() 不可靠，失败回合会误记 completed）
        #
        # GeneratorExit / asyncio.CancelledError（生成器被外部关闭/取消，如
        # detach 的 pending.cancel、进程关停、端断开）不经任何 except 分支，
        # turn_failure 保持 None → 记 completed。这是有意的：detach 取消是
        # 切空间的正常生命周期，记 failed 会系统性污染失败率；两者都不是
        # 「回合未达成且需关注」的失败。
        # 清理 mid-turn 远端接续输入恢复的 turn ContextVar：逆序 reset
        # 回到初始值，避免远端上下文泄漏到回合外（如下一回合或后台任务）。
        # 正文回投统一走 EndRegistry 流式路由，无收尾补发镜像。
        if coara._turn_context_tokens:
            from src.coara.turn_context import reset_turn_context

            for _tokens in reversed(coara._turn_context_tokens):
                reset_turn_context(_tokens)
            coara._turn_context_tokens = []
        coara.clear_deferred_remote_ctx()
        coara.clear_rollback_floor()
        timing = getattr(coara, "_turn_timing", None)
        if timing is not None:
            coara._emit_turn_timing(timing)
            coara._turn_timing = None
        # Cancel pending foreground delegates — covers KeyboardInterrupt,
        # generic exceptions, and normal turn exit. Released delegates
        # (explicitly let go at the exit soft-reminder) keep running: their
        # late results park into history via the done callback.
        # CoaraRunCancelledError already cancels above (before yield) to
        # prevent delegates from running during the yield window.
        coara.cancel_all_pending_foreground_delegates(include_released=False)
        coara._active_turn = None
        if coara.status == CoaraStatus.RUNNING:
            coara.status = CoaraStatus.IDLE
        # Persist message_history for cross-process session recovery.
        try:
            await asyncio.to_thread(coara.persist_session_to_disk)
        except Exception:
            logger.exception("Failed to persist session history after turn")
        # 先落盘再清标记：保证「标记在 ⇒ 本轮历史未落盘」的恢复推断成立
        try:
            await asyncio.to_thread(coara._clear_turn_in_flight)
        except Exception:
            logger.debug("Failed to clear turn in-flight marker", exc_info=True)
        session_log = getattr(coara, "_session_log", None)
        if session_log is not None:
            # abort 优先归类打断；失败判定用显式捕获的 turn_failure
            if turn_runtime.signal.aborted:
                reason = "interrupted"
            elif turn_failure is not None:
                reason = "failed"
            else:
                reason = "completed"
            session_log.record_turn_end(turn_runtime.turn_id, reason=reason)


def _build_background_delegate_reminder(executions: list[Any]) -> str:
    """Build a concise reminder listing launched background delegates.

    The reminder is injected into message_history as a USER-role system-info
    message so the next LLM iteration knows which tasks are already running
    and can avoid re-delegating the same work.
    """
    lines: list[str] = []
    for ex in executions:
        metadata = ex.result.metadata or {}
        subagent_type = metadata.get("subagent_type") or ex.tool_call.arguments.get("subagent_type") or "?"
        description = metadata.get("description") or ex.tool_call.arguments.get("description") or "后台任务"
        task_id = metadata.get("task_id") or "?"
        lines.append(f"- [{subagent_type}] {description} ({task_id})")
    if not lines:
        return ""
    return "以下后台子代理任务已启动并在运行中：\n" + "\n".join(lines)


def _collect_turn_effects(
    executions: list[Any],
    file_effects: list[tuple[str, str]],
    other_effects: list[str],
) -> None:
    """登记本批成功工具的副作用：写盘工具记 (工具名, 目标文件)，其余只记名字。

    正常完成与中断收场共用同一口径，供回滚/中断注记如实列出磁盘改动。
    """
    for execution in executions:
        if execution.result.is_error or getattr(execution.result, "is_cancelled", False):
            continue
        name = execution.tool_call.name
        if name in _DISK_MUTATING_TOOL_NAMES:
            args = execution.tool_call.arguments
            path = args.get("path") if isinstance(args, dict) else None
            file_effects.append((name, str(path) if path else "?"))
        else:
            other_effects.append(name)


def _subagent_store_for_coara(coara: Any) -> tuple[Any, bool]:
    """Build the SubagentStore backing ``coara``'s delegates.

    Delegate breakpoints live under the workspace's coara Home; when the node
    carries a workspace manager its ``coara_home`` wins (mirrors
    DelegateTool._subagent_store_for_dir). Returns (store_or_None, failed):
    when unavailable (no workspace_dir / resolution error) callers keep the
    tool-receipt default, so contexts without a coara behave exactly as before.
    """
    workspace_dir = getattr(coara, "workspace_dir", None) if coara is not None else None
    if not workspace_dir:
        return None, True
    wm = getattr(coara, "workspace_manager", None)
    coara_home = getattr(wm, "coara_home", None) if wm is not None else None
    try:
        from src.coara.subagent_store import subagent_store_for_workspace

        return subagent_store_for_workspace(workspace_dir, coara_home=coara_home), False
    except Exception:
        logger.warning("Failed to open subagent store for delegate effects", exc_info=True)
        return None, True


def _delegate_outcome_from_store(store: Any, task_id: str) -> str:
    """Map a SubagentStore record status onto the note outcome vocabulary.

    A delegate tool receipt only proves a spawn was accepted; the subagent
    itself runs asynchronously and may still be running or later fail (e.g.
    provider 403). When a store record exists it is the source of truth for
    the outcome; a missing record (cleaned/pruned) falls back to the receipt
    default "ok".
    """
    try:
        record = store.load(task_id)
    except Exception:
        logger.debug(f"SubagentStore load failed for {task_id}", exc_info=True)
        return "ok"
    if record is None:
        return "ok"
    status = record.status
    if status in (SubagentStatus.RUNNING_FOREGROUND.value, SubagentStatus.RUNNING_BACKGROUND.value):
        return "running"
    if status == SubagentStatus.IDLE.value:
        # 成功完成后记录落 idle（已完成并闲置），等价 ok
        return "ok"
    if status == SubagentStatus.FAILED.value:
        return "failed"
    if status == SubagentStatus.CANCELLED.value:
        return "cancelled"
    return "ok"


def _collect_delegate_effects(
    executions: list[Any],
    delegates: list[tuple[str, str, str]],
    coara: Any = None,
) -> None:
    """登记本批 delegate 结果（含失败/取消）：回滚后仍需知道可 resume 的 task_id。"""
    seen = {tid for tid, _, _ in delegates}
    store: Any = None
    store_failed = False
    for execution in executions:
        if execution.tool_call.name != "delegate":
            continue
        meta = execution.result.metadata or {}
        task_id = str(meta.get("task_id") or meta.get("subagent_id") or "").strip()
        if not task_id:
            # 兼容旧错误文案里嵌的 task_id="sa-…"
            import re as _re

            m = _re.search(r'task_id="(sa-[^"]+)"', str(execution.result.content or ""))
            if not m:
                m = _re.search(r"\[(sa-[a-z0-9-]+)\]", str(execution.result.content or ""), _re.I)
            task_id = m.group(1) if m else ""
        if not task_id or task_id in seen:
            continue
        seen.add(task_id)
        args = execution.tool_call.arguments if isinstance(execution.tool_call.arguments, dict) else {}
        desc = str(meta.get("description") or args.get("description") or "").strip()
        if getattr(execution.result, "is_cancelled", False):
            outcome = "cancelled"
        elif execution.result.is_error:
            outcome = "failed"
        elif str(meta.get("mode") or "") == "background":
            outcome = "background"
        elif meta.get("outcome"):
            outcome = str(meta.get("outcome"))
        else:
            # spawn 成功回执 ≠ 子智能体完成：metadata 无 outcome 只说明派发成功，
            # 子智能体异步运行可能仍在跑或已失败（403 等）。真实状态以
            # SubagentStore 为准，不再一律兜底 ok（否则会把运行/失败者误标成已完成）
            if store is None and not store_failed:
                store, store_failed = _subagent_store_for_coara(coara)
            outcome = _delegate_outcome_from_store(store, task_id) if store is not None else "ok"
        delegates.append((task_id, desc, outcome))


def _format_delegate_effects_detail(delegates: list[tuple[str, str, str]]) -> str:
    """回滚/打断注记中的子智能体断点清单（含可 resume 的 task_id）。"""
    if not delegates:
        return ""
    outcome_label = {
        "ok": "已完成",
        "failed": "已失败（断点仍在）",
        "cancelled": "已取消（断点仍在）",
        "background": "后台运行中",
        "running": "运行中（断点可 resume）",
    }
    lines = [
        "本回合已派发的子智能体（对话记录若被回滚，断点仍在磁盘；"
        '换模型或额度恢复后可用 delegate(action="resume", task_id=…) 接着跑，不必整批重派摸底）：'
    ]
    for task_id, desc, outcome in delegates:
        label = outcome_label.get(outcome, outcome)
        if desc:
            lines.append(f"- {task_id}：{desc}（{label}）")
        else:
            lines.append(f"- {task_id}（{label}）")
    return "\n" + "\n".join(lines)


def _format_side_effects_detail(
    file_effects: list[tuple[str, str]],
    other_tools: list[str],
) -> str:
    """副作用清单正文：写盘文件逐条列出（去重），其余工具按次数汇总。"""
    detail = ""
    seen: set[tuple[str, str]] = set()
    file_lines: list[str] = []
    for name, path in file_effects:
        if (name, path) in seen:
            continue
        seen.add((name, path))
        file_lines.append(f"- {name}: {path}")
    if file_lines:
        detail += "\n" + "\n".join(file_lines)
    if other_tools:
        counts: dict[str, int] = {}
        for name in other_tools:
            counts[name] = counts.get(name, 0) + 1
        summary = "、".join(f"{name}×{count}" if count > 1 else name for name, count in counts.items())
        detail += f"\n（此外已执行：{summary}）"
    return detail


def _build_interrupt_side_effects_note(
    *,
    file_effects: list[tuple[str, str]],
    other_tools: list[str],
    delegates: list[tuple[str, str, str]] | None = None,
) -> str:
    """中断收尾注记的副作用段：打断只停回合，已完成的工具改动不回滚。"""
    delegates = delegates or []
    if not file_effects and not other_tools and not delegates:
        return ""
    note = ""
    if file_effects or other_tools:
        note = "打断前已完成的工具操作不会撤销，磁盘上的改动仍然生效："
        note += _format_side_effects_detail(file_effects, other_tools)
        note += "\n请基于磁盘当前实际状态继续，不要假设上述改动未发生。"
    note += _format_delegate_effects_detail(delegates)
    return note.lstrip("\n")


def _build_rollback_side_effects_note(
    exc: BaseException,
    *,
    file_effects: list[tuple[str, str]],
    other_tools: list[str],
    delegates: list[tuple[str, str, str]] | None = None,
) -> str:
    """Build the disk side-effect note injected after a full-turn rollback.

    回滚只删 message_history，本回合已完成的工具改动（写盘文件等）不会撤销；
    如实列出已执行的工具与目标文件，以及可 resume 的子智能体 task_id。
    """
    delegates = delegates or []
    if not file_effects and not other_tools and not delegates:
        return ""
    note = (
        f"上一轮因未预期错误（{type(exc).__name__}）中断，该轮对话记录已回滚，"
        "但回滚前已完成的工具操作不会撤销，磁盘上的改动仍然生效："
    )
    if file_effects or other_tools:
        note += _format_side_effects_detail(file_effects, other_tools)
        note += "\n请基于磁盘当前实际状态继续，不要假设上述改动未发生。"
    else:
        # 仅有子智能体断点、没有写盘工具时，开头那句「磁盘改动」略空，收束成回滚说明
        note = f"上一轮因未预期错误（{type(exc).__name__}）中断，该轮对话记录已回滚；子智能体断点仍保留在磁盘："
    note += _format_delegate_effects_detail(delegates)
    return note


__all__ = ["TurnRunContext", "run_turn_loop"]
