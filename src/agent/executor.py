"""Tool execution helpers with concurrent-safe partitioning."""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from src.agent.tool_policy import ToolExecutionPolicy
from src.coara.turn_completion import CoaraRunCancelledError
from src.core.abort import OperationAborted
from src.core.config import config_manager
from src.core.error_log import log_tool_error_event
from src.core.logger import logger
from src.core.tool_base import ToolKind, ToolResult
from src.core.types import ToolCall
from src.todos.trace_payload import build_todo_update_trace_payload
from src.tools.cache import tool_cache, tool_execution_cache_scope
from src.tools.sandbox import get_sandbox

# 工具全文随 tool_complete 事件下发（Web 查看区渲染全文用）。封顶避免超大输出把
# trace 事件撑爆；超限标截断，前端提示可读 spill ref。
_MAX_TOOL_OUTPUT_CHARS = 256_000

# 手机端（Matrix）慢工具行：执行超过该秒数仍未完成时先发一条 running 预览，
# 完成后用同 tool_call_id 的完成帧覆盖为 ✓/✗。快工具不发预览，仍只在结束时一条。
SLOW_TOOL_LINE_DELAY_S = 3.0


def _is_ptc_sub_call(tool_call: Any) -> bool:
    """ptc 程序内的子调用（``code:`` 前缀 id）"""
    return str(getattr(tool_call, "id", "") or "").startswith("code:")


def _event_source(coara: Any) -> str:
    """事件端归属的唯一取法：当前段优先，回合发起端兜底

    同一次工具调用的全部事件（开始 / 完成 / 慢工具预览 / diff 记账）必须同源——
    两处各自计算会在跟话切段后漂移，端侧按 source 过滤时把不属于本端的活动收下
    """
    return str(
        getattr(getattr(coara, "_segments", None), "source", "")
        or getattr(coara, "_active_turn_source", "")
        or ""
    )


def _build_canonical_diff_lines(display: list[Any]) -> dict[str, Any] | None:
    """coara 进程内一次性算好 diff 的最终展示行（含对称上下文），作为各端渲染的单一事实源。

    CLI / 手机 / Web 都渲染这份结果；Web 不再各自用 TS 重算一遍（避免两端不一致）。
    """
    from src.coara.diff_render import collect_diff_hunks

    hunks, added, removed = collect_diff_hunks(display)
    if not hunks:
        return None
    return {
        "path": display[0].path,
        "added": added,
        "removed": removed,
        "hunks": [
            [
                {
                    "kind": dl.kind.name.lower(),
                    "oldNum": dl.old_num,
                    "newNum": dl.new_num,
                    "code": dl.code,
                }
                for dl in hunk
            ]
            for hunk in hunks
        ],
    }


# READ is deliberately excluded: read.py maintains its own mtime-aware cache
_CACHEABLE_TOOL_KINDS = frozenset({ToolKind.SEARCH, ToolKind.FETCH})

# 已更名的工具：旧名 → 新名。模型从旧会话历史学到旧名时给出明确指引
_RENAMED_TOOLS: dict[str, str] = {"tool_search": "tool"}

# 批次中断时等待仍在跑的工具组收尾的上限：超时后仍未完成的调用按未执行处理
_INTERRUPT_DRAIN_TIMEOUT_SECONDS = 5.0

if TYPE_CHECKING:
    from src.coara.base import CoaraBase
    from src.core.abort import AbortSignal


@dataclass(slots=True)
class ToolExecution:
    """Executed tool call and its result."""

    index: int
    tool_call: ToolCall
    result: ToolResult


class ToolExecutor:
    """Execute tool calls, grouping by write-lock key for safe concurrency."""

    def __init__(self, default_timeout: float = 120.0):
        # （None=自己管，如 delegate/orchestrator 全权管理自己的时长）。
        self.default_timeout = default_timeout
        self._policy = ToolExecutionPolicy(config_manager)

    @staticmethod
    def _check_sandbox(tool_name: str, arguments: dict) -> str | None:
        """Check if a tool call violates sandbox boundaries for untrusted callers.

        Returns None if allowed, or a denial reason string if blocked.
        """
        sandbox = get_sandbox()

        if tool_name == "shell":
            command = arguments.get("command", "")
            return sandbox.check_command(command)

        if tool_name in ("read", "write", "edit", "delete"):
            for key in ("path", "template_path"):
                # read 的 ref 是溢出存档引用而非文件系统路径，不纳入沙箱路径检查
                value = arguments.get(key)
                if isinstance(value, str) and value:
                    denial = sandbox.check_path(value)
                    if denial:
                        return denial
            return None

        if tool_name == "web_fetch":
            url = arguments.get("url", "")
            return sandbox.check_url(url)

        # Other tools are not sandbox-restricted by default
        return None

    @staticmethod
    def _error_log_context(coara: CoaraBase) -> dict[str, object]:
        try:
            coara_home = config_manager.config.coara_home
        except Exception:
            coara_home = None
        return {
            "workspace_dir": coara.workspace_dir,
            "session_id": coara.audit_session_id,
            "coara_id": coara.identity.coara_id,
            "coara_name": coara.identity.name,
            "agent_session_id": coara.session_id,
            "coara_home": coara_home,
        }

    @staticmethod
    def _log_tool_error(
        *,
        event: str,
        tool_call: ToolCall,
        coara: CoaraBase,
        arguments: dict | None = None,
        output: object = None,
        is_cancelled: bool = False,
        duration_ms: float | None = None,
        reason: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        try:
            ctx = ToolExecutor._error_log_context(coara)
            merged_metadata = dict(metadata or {})
            agent_session_id = ctx.get("agent_session_id")
            if agent_session_id and agent_session_id != ctx["session_id"]:
                merged_metadata.setdefault("agent_session_id", agent_session_id)
            message = str(output or reason or "tool error")
            log_tool_error_event(
                workspace_dir=ctx["workspace_dir"],  # type: ignore[arg-type]
                session_id=str(ctx["session_id"]),
                coara_id=str(ctx["coara_id"]),
                coara_name=str(ctx["coara_name"]),
                source_event=event,
                tool_name=tool_call.name,
                tool_call_id=tool_call.id,
                message=message,
                arguments=arguments,
                reason=reason,
                duration_ms=duration_ms,
                is_cancelled=is_cancelled,
                metadata=merged_metadata or None,
                coara_home=ctx.get("coara_home"),  # type: ignore[arg-type]
            )
        except Exception as exc:
            # Error logging must never break tool execution.
            logger.warning("Failed to write tool error log: {}", exc)

    async def execute(
        self,
        coara: CoaraBase,
        tool_calls: list[ToolCall],
        is_owner: bool,
        signal: AbortSignal | None = None,
        interrupt_sink: list[ToolExecution] | None = None,
    ) -> list[ToolExecution]:
        """按写锁分组调度：同一批 tool_calls 默认全并发，仅锁键相同的串行"""
        if not tool_calls:
            return []

        # 按 (lock_key or sentinel) 分组，保留每组内的出现顺序
        # None 键共用一个 sentinel，使所有只读调用合并到同一并发组
        _read_sentinel = "__read__"
        groups: dict[str, list[tuple[int, ToolCall]]] = {}
        for i, tc in enumerate(tool_calls):
            tool = coara._tool_manager.tools.get(tc.name)
            lock: str | None = None
            if tool is not None:
                args = tc.arguments if isinstance(tc.arguments, dict) else {}
                try:
                    lock = tool.get_write_lock(args)
                except Exception:
                    lock = None
            groups.setdefault(lock or _read_sentinel, []).append((i, tc))

        # 中断收尾用收集器：_execute_one 每产出一个结果（含已取消）即追加，
        # 批次异常收场时仍能把真实执行结果带出
        completed: list[ToolExecution] = []

        async def run_group(group_key: str, group: list[tuple[int, ToolCall]]) -> list[ToolExecution]:
            if not group:
                return []
            # 只读组（None 键归并到 _read_sentinel）全并发；有锁组内串行
            if group_key == _read_sentinel:
                child_tasks = [
                    asyncio.create_task(self._execute_one(coara, i, c, is_owner, signal, completed)) for i, c in group
                ]
                try:
                    return list(await asyncio.gather(*child_tasks))
                except BaseException:
                    # 能如实带出，而不是被悬空闭合谎标「未执行」
                    for child in child_tasks:
                        child.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await asyncio.wait(child_tasks, timeout=_INTERRUPT_DRAIN_TIMEOUT_SECONDS)
                    raise

            return [await self._execute_one(coara, i, tc, is_owner, signal, completed) for i, tc in group]

        group_tasks = [asyncio.create_task(run_group(key, group)) for key, group in groups.items()]
        try:
            group_results = await asyncio.gather(*group_tasks)
        except (asyncio.CancelledError, CoaraRunCancelledError):
            # 中断/取消：取消仍在跑的组并等其收尾（各自把已完成/已取消的真实
            # 结果记入 completed），再把全部已有结果带出给上层如实入史
            for task in group_tasks:
                task.cancel()
            await asyncio.wait(group_tasks, timeout=_INTERRUPT_DRAIN_TIMEOUT_SECONDS)
            if interrupt_sink is not None:
                interrupt_sink.extend(sorted(completed, key=lambda item: item.index))
            raise
        executions: list[ToolExecution] = []
        for sub in group_results:
            executions.extend(sub)
        executions.sort(key=lambda item: item.index)
        self._apply_batch_spill_budget(coara, executions)
        return executions

    def _apply_batch_spill_budget(self, coara: CoaraBase, executions: list[ToolExecution]) -> None:
        if not executions:
            return
        from src.runtime.tool_output_store import apply_batch_spill_budget, model_facing_byte_len

        coara_home = None
        if config_manager._config is not None:
            coara_home = config_manager._config.coara_home

        items: list[tuple[str, str, ToolResult, str | None]] = []
        for item in executions:
            tool = coara._tool_manager.tools.get(item.tool_call.name)
            category = getattr(tool, "category", None) if tool is not None else None
            items.append((item.tool_call.name, item.tool_call.id, item.result, category))

        updated = apply_batch_spill_budget(
            workspace_dir=coara.workspace_dir,
            session_id=coara.session_id,
            items=items,
            coara_home=coara_home,
        )
        from src.tools.cache import tool_cache, tool_execution_cache_scope

        cache_scope = tool_execution_cache_scope(coara)
        for idx, new_result in enumerate(updated):
            old_result = executions[idx].result
            if model_facing_byte_len(new_result) == model_facing_byte_len(old_result):
                continue
            executions[idx].result = new_result
            # spill 重写在批后发生：把 spill 后的结果（含 output_spilled 元数据）
            # 同步进缓存——下次缓存命中应返回 spill 后的占位内容，与 model 所见一致。
            tool = coara._tool_manager.tools.get(executions[idx].tool_call.name)
            if (
                tool is not None
                and tool.kind in _CACHEABLE_TOOL_KINDS
                and not new_result.is_error
                and not getattr(new_result, "is_cancelled", False)
            ):
                tool_cache.set(
                    executions[idx].tool_call.name,
                    executions[idx].tool_call.arguments,
                    new_result,
                    scope=cache_scope,
                )

    def _attach_invocation_context(
        self,
        invocation: Any,
        tool_call: ToolCall,
        coara: CoaraBase,
        trust_level: str,
    ) -> None:
        """create_invocation 后向冻实例注入运行时上下文（集中单点，替代散落的 object.__setattr__）。"""
        # Attach runtime context for tools that emit trace/UI linkage (e.g. delegate parent_tool_call_id).
        from src.coara.turn_source import current_turn_source

        invocation.bind_runtime_context(
            tool_call_id=tool_call.id,
            session_id=coara.session_id,
            coara_id=str(getattr(getattr(coara, "identity", None), "coara_id", "") or ""),
            origin_source=current_turn_source(coara),
            # 发起者归属：异步完成的产出（媒体文件/投递卡片）按它寻址落线，不能读
            # 「完成那一刻的端视图」——那是跨空间串内容的病根。
            workspace_dir=str(getattr(coara, "workspace_dir", "") or ""),
            turn_id=str(getattr(getattr(coara, "_active_turn", None), "turn_id", "") or ""),
        )
        # Attach trust_level so individual tools can access it (e.g., shell env sanitization)
        if hasattr(invocation, "_trust_level"):
            invocation._trust_level = trust_level
        else:
            object.__setattr__(invocation, "_trust_level", trust_level)

    def _build_tool_complete_payload(
        self,
        *,
        coara: CoaraBase,
        tool: Any,
        tool_call: ToolCall,
        effective_call: ToolCall,
        result: ToolResult,
        duration_ms: float,
        cache_hit: bool,
        raw_output_text: str,
        source: str,
    ) -> dict[str, Any]:
        """装配 tool_complete trace 事件 payload（含 diff 单源与录像带 diff 落盘）。"""
        from src.coara.display import format_tool_call_label
        from src.coara.tool_output.pipeline import serialize_display_blocks
        from src.runtime.usage_args import compact_tool_usage_args
        from src.runtime.usage_attribution import attribution_from_coara

        # 工具行文本：成功＝调用摘要；失败再拼错误首行（端上人眼排障，模型通道仍看 content）
        tool_label = format_tool_call_label(tool.name, effective_call.arguments, max_len=None)
        if result.is_error:
            error_line = str(result.content or "").split("\n", 1)[0].strip()
            if len(error_line) > 200:
                error_line = error_line[:200] + "…"
            if error_line:
                tool_label = f"{tool_label} 报错: `{error_line}`"
        # 端归属由调用方带入（工具开始时锁定），此处不重算
        tool_complete_payload: dict[str, Any] = {
            "tool_name": tool.name,
            "tool_call_id": tool_call.id,
            "tool_label": tool_label,
            "is_error": result.is_error,
            "duration_ms": duration_ms,
            "session_id": coara.session_id,
            # turn_id 进 payload：web 视图存储的 diff 落盘（sender emit "diff" 帧，
            # 经 TurnStream persist）与消费方按它归属回合。与 record_assistant_diff 同源。
            "turn_id": str(getattr(getattr(coara, "_active_turn", None), "turn_id", "") or ""),
            "coara_id": coara.identity.coara_id,
            # 注入段来源：diff 进聊天区/视图存储按端归属（web 只显示 web 来源回合的 diff，
            # 手机回合归 matrix，不镜像到 web）。与 record_assistant_diff 的 source 同源。
            "source": source,
            "display_blocks": serialize_display_blocks(result.display),
            "tool_output": (raw_output_text[:_MAX_TOOL_OUTPUT_CHARS] if raw_output_text else ""),
            "tool_output_truncated": len(raw_output_text) > _MAX_TOOL_OUTPUT_CHARS,
            "tool_output_ref": ((result.metadata or {}).get("output_ref", "")),
            "usage_args": compact_tool_usage_args(tool.name, effective_call.arguments),
            "cache_hit": cache_hit,
            "cli_silent": bool(getattr(coara, "_cli_silent", False)),
            **attribution_from_coara(coara),
        }
        # 当前注入段（哪次用户注入之后调用 delegate，活动送哪端显示；不随运行中
        if not getattr(coara, "identity", None) or not coara.identity.user_facing:
            _sub_origin = ""
            _origin_snap = getattr(coara, "_subagent_origin", None)
            if isinstance(_origin_snap, (tuple, list)) and len(_origin_snap) >= 2:
                _sub_origin = str(_origin_snap[0] or "")
            # 老版本子智能体无登记：回退父会话当前段（近似）
            if not _sub_origin:
                _parent_ref = getattr(coara, "_subagent_parent", None)
                if _parent_ref is not None:
                    _sub_origin = str(getattr(getattr(_parent_ref, "_segments", None), "source", "") or "").strip()
            if _sub_origin:
                tool_complete_payload["subagent_origin"] = _sub_origin
        # diff 单源：coara 进程内一次性算好最终展示行（含对称上下文），塞进事件。
        # CLI/手机/Web 都渲染这份 -- Web 不再各自用 TS 重算一遍（避免两端不一致）。
        if result.display:
            diff_lines = _build_canonical_diff_lines(result.display)
            if diff_lines is not None:
                tool_complete_payload["diff_lines"] = diff_lines
                # 刷新/切空间后聊天区 diff 块可从录像带重建，不再仅存于实时帧内存。
                _diff_recorder = getattr(coara, "_session_log", None)
                if _diff_recorder is not None and not _is_ptc_sub_call(tool_call):
                    with contextlib.suppress(Exception):
                        _diff_recorder.record_assistant_diff(
                            diff=diff_lines,
                            turn_id=str(getattr(getattr(coara, "_active_turn", None), "turn_id", "") or ""),
                            source=source,
                        )
        result_meta = result.metadata or {}
        if tool.name == "web_fetch" and result_meta.get("fetch_meta"):
            tool_complete_payload["fetch_meta"] = result_meta.get("fetch_meta")
        if tool.name == "web_search" and result_meta.get("search_meta"):
            tool_complete_payload["search_meta"] = result_meta.get("search_meta")
        if tool.name == "delegate":
            # Foreground-async / background spawn returns before the child finishes;
            # CLI live paren needs these so it does not mark the row done early.
            mode = result_meta.get("mode")
            if mode is not None and str(mode).strip():
                tool_complete_payload["delegate_mode"] = str(mode).strip()
            task_id = result_meta.get("task_id")
            if task_id is not None and str(task_id).strip():
                tool_complete_payload["delegate_task_id"] = str(task_id).strip()
        return tool_complete_payload

    def _emit_tool_events(
        self,
        *,
        coara: CoaraBase,
        tool: Any,
        tool_call: ToolCall,
        effective_call: ToolCall,
        result: ToolResult,
        tool_complete_payload: dict[str, Any],
        duration_ms: float,
        cache_hit: bool,
        output_lines: int,
        output_bytes: int,
    ) -> None:
        """工具完成后的事件发射段：tool_complete / 录像带 tool_exec / tool_call / tool_result / todo 特判。"""
        ptc_sub_call = _is_ptc_sub_call(tool_call)
        if not ptc_sub_call:
            coara._emit_trace(
                "tool_complete",
                f"Tool completed: {tool.name}",
                payload=tool_complete_payload,
            )
            # 工具行帧：先于 diff 投递——聊天流里工具行在上、
            # 它产生的 diff 紧随其下（CLI 的 · 行 + ⎿ diff 层级同构）。
            coara._route_tool_line(tool_complete_payload)
            # diff 帧统一路由：与正文 chunk 同走 EndRegistry（按段 source 投递
            # 端通道），端通道 sender 自行渲染。消除各端各自订阅事件的分散消费。
            coara._route_tool_diff(tool_complete_payload)
        elif tool_complete_payload.get("display_blocks"):
            # 里的编辑」在聊天流里凭空发生：文件变了，页面上既没有工具行也没有
            coara._route_tool_line(tool_complete_payload)
            coara._route_tool_diff(tool_complete_payload)

        # ptc 子调用不记——它们是程序内部步骤，不是用户可见的工具执行。
        recorder = getattr(coara, "_session_log", None)
        if recorder is not None and not _is_ptc_sub_call(tool_call):
            # record_tool_exec 内部已尽力而为，suppress 兜底（防录像带
            # 异常反噬工具执行主路径）
            with contextlib.suppress(Exception):
                recorder.record_tool_exec(
                    turn_id=str(getattr(getattr(coara, "_active_turn", None), "turn_id", "") or ""),
                    tool_call_id=tool_call.id,
                    tool_name=tool.name,
                    is_error=result.is_error,
                    duration_ms=duration_ms,
                    lines=output_lines,
                    bytes_=output_bytes,
                    spill_ref=str((result.metadata or {}).get("output_ref", "") or ""),
                    cache_hit=cache_hit,
                )

        # Emit tool_call event for Dashboard and EventBus subscribers
        tool_output_text = result.content if isinstance(result.content, str) else str(result.content)
        if not _is_ptc_sub_call(tool_call):
            coara._emit_trace(
                "tool_call",
                f"Tool executed: {tool.name}",
                payload={
                    "tool_name": tool.name,
                    "tool_call_id": tool_call.id,
                    "call_id": tool_call.id,
                    "arguments": effective_call.arguments,
                    "tool_output": tool_output_text[:500],
                    "is_error": result.is_error,
                    "session_id": coara.session_id,
                    "output_ref": (result.metadata or {}).get("output_ref"),
                    "output_bytes": (result.metadata or {}).get("output_bytes"),
                    "output_spilled": (result.metadata or {}).get("output_spilled"),
                },
            )

        # Emit tool_result so Web UI consumers can mark the tool call as done.
        if not _is_ptc_sub_call(tool_call):
            coara._emit_trace(
                "tool_result",
                f"Tool result: {tool.name}",
                payload={
                    "tool": tool.name,
                    "call_id": tool_call.id,
                    "summary": tool_output_text[:500],
                    "ok": not result.is_error,
                    "session_id": coara.session_id,
                },
            )

        if tool.name == "todo":
            todo_metadata = result.metadata or {}
            todo_payload = build_todo_update_trace_payload(
                todo_metadata,
                getattr(coara, "session_id", ""),
            )
            coara._emit_trace(
                "todo_update",
                todo_metadata.get("summary") or "Todo list updated",
                payload=todo_payload,
            )

    @staticmethod
    def _wants_slow_tool_preview(coara: CoaraBase, tool_call: ToolCall, source: str) -> bool:
        """仅手机发起回合：超过阈值仍未结束时先发 running 工具行"""
        if _is_ptc_sub_call(tool_call):
            return False
        if getattr(coara, "_cli_silent", False):
            return False
        return str(source or "").strip().lower() == "matrix"

    def _schedule_slow_tool_preview(
        self,
        *,
        coara: CoaraBase,
        tool: Any,
        tool_call: ToolCall,
        effective_call: ToolCall,
        finished: asyncio.Event,
        source: str,
    ) -> asyncio.Task[None] | None:
        """执行开始后调度：满 SLOW_TOOL_LINE_DELAY_S 且仍未完成 → 发 running 行。"""
        if not self._wants_slow_tool_preview(coara, tool_call, source):
            return None
        from src.coara.display import format_tool_call_label

        label = format_tool_call_label(tool.name, effective_call.arguments, max_len=None)
        if not label.strip():
            return None
        payload = {
            "tool_label": label,
            "tool_name": tool.name,
            "tool_call_id": tool_call.id,
            "is_error": False,
            "running": True,
            "source": source,
            "turn_id": str(getattr(getattr(coara, "_active_turn", None), "turn_id", "") or ""),
        }

        async def _emit_when_slow() -> None:
            try:
                await asyncio.wait_for(finished.wait(), timeout=SLOW_TOOL_LINE_DELAY_S)
                return
            except TimeoutError:
                pass
            except asyncio.CancelledError:
                raise
            if finished.is_set():
                return
            with contextlib.suppress(Exception):
                coara._route_tool_line(payload)

        try:
            return asyncio.get_running_loop().create_task(_emit_when_slow())
        except RuntimeError:
            return None

    @staticmethod
    async def _stop_slow_tool_preview(
        finished: asyncio.Event,
        task: asyncio.Task[None] | None,
    ) -> None:
        """完成/失败前先关门：避免 running 行晚于完成行落到房间。"""
        finished.set()
        if task is None:
            return
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def _execute_one(
        self,
        coara: CoaraBase,
        index: int,
        tool_call: ToolCall,
        is_owner: bool,
        signal: AbortSignal | None = None,
        completed: list[ToolExecution] | None = None,
    ) -> ToolExecution:
        def _record(execution: ToolExecution) -> ToolExecution:
            # 追加到批次收集中，批次被中断时真实结果仍可带出
            if completed is not None:
                completed.append(execution)
            return execution

        started_at = time.perf_counter()
        result: ToolResult | None = None
        tool = coara._tool_manager.tools.get(tool_call.name)
        if tool is None:
            renamed_to = _RENAMED_TOOLS.get(tool_call.name)
            if renamed_to:
                result = ToolResult.error(
                    f"Tool not found: {tool_call.name}（已更名为 {renamed_to}，请改用新名重新调用）"
                )
            else:
                result = ToolResult.error(f"Tool not found: {tool_call.name}")
            self._log_tool_error(
                event="tool_blocked",
                tool_call=tool_call,
                coara=coara,
                arguments=tool_call.arguments if isinstance(tool_call.arguments, dict) else None,
                reason=result.content if isinstance(result.content, str) else "tool_not_found",
            )
            return _record(ToolExecution(index=index, tool_call=tool_call, result=result))

        # Pre-tool hooks (access policy, loop detection, etc.)
        decision = await coara.tool_hook_runner.run_before(coara, tool, tool_call, is_owner)
        if not decision.allowed:
            result = ToolResult.error(decision.reason or f"Tool call blocked: {tool_call.name}")
            self._log_tool_error(
                event="tool_blocked",
                tool_call=tool_call,
                coara=coara,
                arguments=tool_call.arguments if isinstance(tool_call.arguments, dict) else None,
                reason=decision.reason or "hook_blocked",
            )
            return _record(
                ToolExecution(
                    index=index,
                    tool_call=tool_call,
                    result=result,
                )
            )

        effective_call = tool_call

        # Detect the sentinel here and return a clear error instead of dispatching
        if isinstance(effective_call.arguments, dict) and "_raw" in effective_call.arguments:
            result = ToolResult.error(
                "工具调用参数解析失败：参数 JSON 不完整，可能因本轮 max_tokens 输出上限被截断。"
                "该工具调用未执行。请降低本轮输出总量后重试（例如将大文件拆分为多次写入），"
                "不要原样重试上一次的内容。"
            )
            self._log_tool_error(
                event="tool_blocked",
                tool_call=tool_call,
                coara=coara,
                arguments=None,
                reason="truncated_or_unparseable_arguments",
            )
            return _record(ToolExecution(index=index, tool_call=tool_call, result=result))

        log_arguments = (
            effective_call.arguments
            if isinstance(effective_call.arguments, dict)
            else {"_raw": effective_call.arguments}
        )

        # Emit tool_start before execution so CLI spinner can show activity
        _args = log_arguments
        if isinstance(_args, dict):
            _args = {k: (v if not isinstance(v, str) or len(v) <= 500 else v[:500] + "...") for k, v in _args.items()}
        # 端归属在工具开始这一刻锁定，整个工具生命周期（start / 慢预览 / complete / diff 记账）
        # 沿用同一个值——结束时不重算，避免跨段执行时同一次调用头尾分属两端。
        source = _event_source(coara)
        if not _is_ptc_sub_call(tool_call):
            coara._emit_trace(
                "tool_start",
                f"Calling tool: {tool.name}",
                payload={
                    "tool_name": tool.name,
                    "tool_call_id": tool_call.id,
                    "arguments": _args,
                    "coara_id": coara.identity.coara_id,
                    "source": source,
                    "session_id": str(getattr(coara, "session_id", "") or ""),
                    "turn_id": str(getattr(getattr(coara, "_active_turn", None), "turn_id", "") or ""),
                },
            )

        # Query tool cache for idempotent operations (search/fetch)
        result = None
        cache_hit = False

        # 主会话工具开关：tools.disabled 禁用的工具拒绝执行（置于审批与缓存命中之前，不可绕过）
        if coara._tool_manager.is_disabled(tool.name):
            result = ToolResult.error(
                f"工具 {tool.name} 已被禁用（tools.disabled 配置）。如需使用请运行 /tools on {tool.name}。"
            )

        _cacheable_kinds = _CACHEABLE_TOOL_KINDS
        cache_scope = None
        if result is None and tool.kind in _cacheable_kinds:
            cache_scope = tool_execution_cache_scope(coara)
            cached = tool_cache.get(tool.name, effective_call.arguments, scope=cache_scope)
            if cached is not None:
                result = cached
                cache_hit = True

        # New declarative API: create invocation, run approval gate, then execute.
        # Determine trust level from coara (needed for sandbox and tool-level checks)
        trust_level = getattr(coara, "_current_trust_level", "owner")
        invocation = None
        timeout: float | None = self.default_timeout
        cancelled = False
        slow_preview_finished = asyncio.Event()
        slow_preview_task: asyncio.Task[None] | None = None
        try:
            # so it can read them for the LLM-driven approval gate.
            invocation_args = effective_call.arguments
            if isinstance(invocation_args, dict):
                invocation_args = {
                    k: v for k, v in invocation_args.items() if k not in ("require_approval", "approval_reason")
                }
            invocation = tool.create_invocation(invocation_args)
            self._attach_invocation_context(invocation, tool_call, coara, trust_level)

            # Config call_policy.prompt can also force-prompt.
            call_decision = await self._policy.resolve(
                coara,
                tool.name,
                tool,
                effective_call.arguments,
                invocation,
                signal,
            )
            if not call_decision.allowed:
                result = ToolResult.error(call_decision.reason)

            # Plan mode restriction: write/edit can only target the plan file
            if result is None and coara.is_plan_mode and tool.name in ("write", "edit"):
                plan_path = coara.plan_file_path
                target_path = None
                if hasattr(invocation, "path"):
                    target_path = invocation.path

                if plan_path and target_path:
                    from pathlib import Path

                    try:
                        if Path(target_path).resolve() != Path(plan_path).resolve():
                            result = ToolResult.error(
                                f"计划模式限制：{tool.name} 只能修改计划文件 {plan_path}。"
                                f'要修改工作空间文件，请先提交计划审阅，或调用 plan(action="exit") 退出计划模式。'
                            )
                    except OSError as exc:
                        result = ToolResult.error(
                            f"计划模式限制：无法解析目标路径 {target_path!r}（{exc}）。"
                            '请使用有效路径，或调用 plan(action="exit") 退出计划模式。'
                        )
                elif plan_path and not target_path:
                    result = ToolResult.error("计划模式限制：无法确定写入目标。")

            # ── Sandbox check (execution-time interceptor, only for untrusted) ──
            if result is None:
                sandbox = get_sandbox()
                if sandbox.is_enabled(trust_level):
                    denial = self._check_sandbox(tool.name, effective_call.arguments)
                    if denial:
                        result = ToolResult.error(f"Sandbox denied: {denial}")

            # Execute if not blocked above
            if result is None:
                # 审批/拦截之后才计时：等人确认不算「慢工具」；满阈值未完成才给手机转圈
                slow_preview_task = self._schedule_slow_tool_preview(
                    coara=coara,
                    tool=tool,
                    tool_call=tool_call,
                    effective_call=effective_call,
                    finished=slow_preview_finished,
                    source=source,
                )
                # 传本次调用参数：工具可按 args 声明 per-call 超时（如 shell 的
                # timeout_ms）。签名统一为 (default_timeout, args=None)。
                timeout = tool.get_execution_timeout(
                    self.default_timeout,
                    args=effective_call.arguments if isinstance(effective_call.arguments, dict) else None,
                )
                if timeout is None:
                    result = await invocation.execute(signal=signal)
                else:
                    execute_task = asyncio.ensure_future(invocation.execute(signal=signal))
                    try:
                        result = await asyncio.wait_for(asyncio.shield(execute_task), timeout=timeout)
                    except TimeoutError:
                        # 超时错误，模型据此决策重试或放弃
                        execute_task._coara_timed_out = True  # type: ignore[attr-defined]
                        execute_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError, TimeoutError, Exception):
                            await asyncio.wait_for(execute_task, timeout=5.0)
                        result = ToolResult.error(f"Tool execution timed out after {timeout}s: {tool_call.name}")
                    except asyncio.CancelledError:
                        # 外层取消（用户打断 / 兄弟清理）：传播给工具任务后再重抛，
                        # 保持原有「工具执行可被取消」语义
                        execute_task.cancel()
                        raise
        except TimeoutError:
            result = ToolResult.error(f"Tool execution timed out after {timeout}s: {tool_call.name}")
        except (ValueError, TypeError) as exc:
            result = ToolResult.error(f"Invalid parameters: {exc}")
        except asyncio.CancelledError:
            result = ToolResult.cancelled(f"工具 {tool_call.name} 的执行已被用户取消。")
            cancelled = True
        except OperationAborted as exc:
            # 在执行中（可能有副作用）。如实记录一条「执行中被中断」的真实结果进
            await self._stop_slow_tool_preview(slow_preview_finished, slow_preview_task)
            interrupted_result = ToolResult.cancelled(
                f"工具 {tool_call.name} 执行中被用户打断，未能完成。",
                metadata={"preserve_cancel_content": True},
            )
            _record(ToolExecution(index=index, tool_call=effective_call, result=interrupted_result))
            raise CoaraRunCancelledError(exc.reason or "interrupted") from exc
        except CoaraRunCancelledError as exc:
            # A tool (e.g. ws switch) requested a mid-turn workspace switch.
            await self._stop_slow_tool_preview(slow_preview_finished, slow_preview_task)
            duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
            if not _is_ptc_sub_call(tool_call):
                coara._emit_trace(
                    "tool_complete",
                    f"Tool completed: {tool.name}",
                    payload={
                        "tool_name": tool.name,
                        "tool_call_id": tool_call.id,
                        "is_error": False,
                        "duration_ms": duration_ms,
                        "session_id": coara.session_id,
                        "coara_id": coara.identity.coara_id,
                        "display_blocks": [],
                        "usage_args": {},
                        "cache_hit": False,
                        "control_cancel": True,
                        "cancel_reason": getattr(exc, "reason", "") or "",
                    },
                )
            raise
        except Exception as exc:
            result = ToolResult.error(f"Execution failed: {exc}")

        # 先关慢工具预览门，再发完成行——避免房间里出现「先 ✓ 后转圈」
        await self._stop_slow_tool_preview(slow_preview_finished, slow_preview_task)

        if result is None:
            result = ToolResult.error(f"Tool execution produced no result: {tool_call.name}")

        # 相对工具本身执行耗时可忽略（spill 写盘本身也是 O(n)）。
        _raw_output_text = result.content if isinstance(result.content, str) else str(result.content)
        _output_lines = _raw_output_text.count("\n")
        _output_bytes = len(_raw_output_text.encode("utf-8"))

        from src.runtime.tool_output_store import maybe_spill_tool_result

        coara_home = None
        if config_manager._config is not None:
            coara_home = config_manager._config.coara_home
        tool_category = getattr(tool, "category", None)
        try:
            result = maybe_spill_tool_result(
                workspace_dir=coara.workspace_dir,
                session_id=coara.session_id,
                tool_name=tool_call.name,
                tool_call_id=tool_call.id,
                arguments=log_arguments if isinstance(log_arguments, dict) else None,
                result=result,
                coara_home=coara_home,
                tool_category=tool_category,
            )
        except Exception as exc:
            logger.warning("Failed to spill tool result for {}: {}", tool_call.name, exc)

        duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
        tool_complete_payload = self._build_tool_complete_payload(
            coara=coara,
            tool=tool,
            tool_call=tool_call,
            effective_call=effective_call,
            result=result,
            duration_ms=duration_ms,
            cache_hit=cache_hit,
            raw_output_text=_raw_output_text,
            source=source,
        )
        self._emit_tool_events(
            coara=coara,
            tool=tool,
            tool_call=tool_call,
            effective_call=effective_call,
            result=result,
            tool_complete_payload=tool_complete_payload,
            duration_ms=duration_ms,
            cache_hit=cache_hit,
            output_lines=_output_lines,
            output_bytes=_output_bytes,
        )
        # 工具行已经 EndRegistry → TurnStream → WS ensure_future 发出；让出一拍
        # 让发送落地、端上滚动，避免并行批收尾才整坨刷屏（执行完一个显示一个）。
        await asyncio.sleep(0)

        # （再 set 会刷新 created_at，破坏 TTL 语义）。
        complete_metadata = dict(result.metadata or {})
        complete_metadata["duration_ms"] = duration_ms
        if cache_hit:
            complete_metadata["cache_hit"] = True
        result.metadata = complete_metadata

        # Cache successful idempotent results for future reuse
        if (
            result is not None
            and not cache_hit
            and tool.kind in _cacheable_kinds
            and not result.is_error
            and not getattr(result, "is_cancelled", False)
        ):
            tool_cache.set(tool.name, effective_call.arguments, result, scope=cache_scope or "")

        coara.loop_detector.record(
            effective_call,
            successful=not result.is_error and not getattr(result, "is_cancelled", False),
        )

        # Post-tool hooks (cleanup, logging, etc.)
        await coara.tool_hook_runner.run_post(coara, tool, effective_call, result, is_owner)

        if result.is_error or getattr(result, "is_cancelled", False):
            self._log_tool_error(
                event="tool_complete",
                tool_call=effective_call,
                coara=coara,
                output=result.content,
                is_cancelled=getattr(result, "is_cancelled", False),
                duration_ms=duration_ms,
                metadata=complete_metadata or None,
            )

        if cancelled:
            _record(ToolExecution(index=index, tool_call=effective_call, result=result))
            raise asyncio.CancelledError()

        return _record(ToolExecution(index=index, tool_call=effective_call, result=result))
