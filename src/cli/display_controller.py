"""CLI display routing hub."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markup import escape as _rich_escape

from src.cli.scrollback import CliScrollback
from src.cli.spinner import BackgroundSpinner, SubagentSpinnerManager
from src.cli.subagent_fold import FoldEntry, SubagentFoldStore
from src.coara.event_bus import EventBus, Subscription

_SYSTEM_BODY_RE = re.compile(r"<(系统提醒|系统消息|后台结果)>\s*(.*?)\s*</\1>", re.DOTALL)


def _extract_system_body(text: str) -> str | None:
    """提取系统注入包裹的消息正文（剥掉仅模型的约束段）；非系统注入返回 None。"""
    match = _SYSTEM_BODY_RE.search(text or "")
    if not match:
        return None
    from src.core.message_tags import strip_llm_only

    return strip_llm_only(match.group(2)).strip()


# Channel B/C: activity tree + subagent tool flush rules 工作流（引擎子进程）不在 CLI 显示——它是 WebUI 独立子系统，
# workflow_* 事件不进活动树（FlowRoot 的 subject="flow" 另有 guard 兜底）。
_ACTIVITY_TREE_TOPICS: tuple[str, ...] = (
    "subagent_start",
    "subagent_complete",
    "subagent_failed",
    "flow_started",
    "flow_finished",
    "background_agent_start",
    "background_agent_complete",
    "tool_start",
    "tool_complete",
    "process_spawned",
    "coara_created",
    "coara_terminated",
    "thinking_progress",
    "llm_turn_complete",
)

# Prompt chrome refresh (dynamic area + toolbar badges); includes inbound event turns
_SPINNER_REFRESH_TOPICS: tuple[str, ...] = (
    "event_turn_start",
    "event_turn_complete",
    "subagent_start",
    "subagent_complete",
    "subagent_failed",
    "background_agent_start",
    "background_agent_complete",
    "background_task_complete",
    "tool_start",
    "tool_complete",
    "thinking_progress",
    "llm_switched",
    "plan_mode_changed",
    "llm_request_start",
    "llm_turn_complete",
    # Echo queued follow-ups as soon as mid-turn input lands / drains
    "continuation_input_received",
    "continuation_input_injected",
)

# 工作空间状态区登记表：_ACTIVITY_TREE_TOPICS 之外还吃这些（动作摘要/回合收尾）
_WORKSPACE_REGISTRY_TOPICS: tuple[str, ...] = (
    "llm_request_start",
    "completed",
    "turn_failed",
    "turn_interrupted",
)

# Foreground session / workspace identity changed — rebind activity filter + reload chrome caches
_RUNTIME_CHROME_RESYNC_TOPICS: tuple[str, ...] = (
    "workspace_switched",
    "session_started",
)

# 不在这里重复收，避免同一份收尾打印两遍。
_FOLD_TOPICS: tuple[str, ...] = (
    "user_message",
    "tool_start",
    "subagent_start",
    "background_agent_start",
)
# 只有本端发起、且属于前台空间的 delegate 运行才建块（各端显示独立）。
_FOLD_DELEGATE_RUN_ACTIONS = frozenset({"", "spawn", "resume"})


def _fold_silent_subagent(subagent_type: str) -> bool:
    """janitor/daily 等系统维护子智能体：完全静默，折叠块也不建（静默策略不变）。"""
    from src.coara.builtin_agents import CLI_SILENT_SUBAGENT_TYPES

    return str(subagent_type or "").strip() in CLI_SILENT_SUBAGENT_TYPES


def _session_matches_root(event, root) -> bool:
    """Match events belonging to the foreground workspace session"""
    fg = root.foreground_coara
    payload = event.payload or {}
    session_id = payload.get("session_id")
    if session_id:
        return session_id == fg.session_id
    return event.coara_id in {root.identity.coara_id, fg.identity.coara_id}


def _event_in_foreground_workspace(event, root) -> bool:
    """Match events belonging to the foreground *workspace* (main session + its subagents)."""
    payload = event.payload or {}
    ws = str(payload.get("workspace_dir") or "").strip()
    if ws:
        try:
            fg = root.foreground_coara
            return Path(ws).resolve() == Path(fg.workspace_dir).resolve()
        except Exception:
            return True
    try:
        return _session_matches_root(event, root)
    except Exception:
        return True


def _is_cancelled_terminal(event: Any, payload: dict[str, Any]) -> bool:
    """终态事件是不是「被取消」（而非真失败）"""
    if str(getattr(event, "event_type", "") or "") != "subagent_failed":
        return False
    haystack = f"{payload.get('error') or ''} {getattr(event, 'message', '') or ''}".lower()
    return any(hint in haystack for hint in ("cancelled", "canceled", "取消"))


def _cli_shows_activity_event(event: Any) -> bool:
    """活动树是否应消费该事件（客户端兜底）"""
    from src.coara.turn_source import cli_shows_source

    payload = getattr(event, "payload", None) or {}
    source = str(payload.get("source") or payload.get("origin_source") or "").strip().lower()
    if source:
        return cli_shows_source(source, unknown=False)
    return False


@dataclass(slots=True)
class _PendingFgDiff:
    """前台暂存的 tool_complete diff：等 ✓ chunk 到达后按序 flush（diff 紧跟 ✓）。"""

    display_blocks: Any
    title_prefix: str = ""
    head_style: str | None = None
    compact: bool = False


@dataclass(slots=True)
class CliDisplayController:
    """Wire runtime signals to CLI presentation (scrollback + dynamic prompt)."""

    root: Any
    console: Console
    subagent_spinner: SubagentSpinnerManager
    background_spinner: BackgroundSpinner
    # 后台工作空间工具行通道（wire 时创建并绑到 background_spinner 的 flush 点）
    background_tool_history: Any | None = field(default=None)
    # 前台回合 diff 暂存（tool_complete 事件可能先于 ✓ chunk 到）：✓ 到达时 flush。
    _pending_fg_diffs: list[_PendingFgDiff] = field(default_factory=list)
    # 子智能体折叠块仓库：一套 CLI，delegate 运行的用途全在这（有界环形缓冲）。
    fold: SubagentFoldStore = field(default_factory=SubagentFoldStore)

    def flush_pending_fg_diffs(self) -> bool:
        """✓ 工具行上屏后，把暂存的前台 diff 依次渲染（保持 diff 紧跟 ✓）。"""
        if not self._pending_fg_diffs:
            return False
        queued = self._pending_fg_diffs
        self._pending_fg_diffs = []
        from src.coara.tool_output.pipeline import render_terminal_from_event

        for item in queued:
            render_terminal_from_event(
                is_error=False,
                display_blocks=item.display_blocks,
                title_prefix=item.title_prefix,
                head_style=item.head_style,
                compact=item.compact,
            )
        self.background_spinner.request_redraw()
        return True

    def ingest_tool_block(self, block: Any, *, owner_key: str) -> bool:
        """活动树完成块 → 折叠块的过程行。owner_key 空＝主会话工具行（返回 False 不出折叠）。"""
        owner = str(owner_key or "").strip()
        if not owner:
            return False
        key = self.fold.resolve(owner) or owner
        self.fold.add_tool(
            key,
            FoldEntry(
                kind="tool",
                tool_call_id=str(getattr(block, "tool_call_id", "") or ""),
                label=str(getattr(block, "label", "") or ""),
                is_error=bool(getattr(block, "is_error", False)),
                depth=int(getattr(block, "depth", 0) or 0),
                tool_name=str(getattr(block, "tool_name", "") or ""),
            ),
        )
        return True

    def ingest_subagent_frame(self, frame: dict) -> bool:
        """attach 子智能体正文/结果帧 → 折叠块（子智能体的正文绝不进前台滚动区）。"""
        frame_type = str(frame.get("type") or "")
        if frame_type not in ("subagent_chunk", "subagent_result"):
            return False
        text = str(frame.get("text") or "")
        key = self.fold.resolve(
            str(frame.get("tool_call_id") or ""),
            str(frame.get("subagent_id") or ""),
            str(frame.get("coara_id") or ""),
        )
        if not key:
            # 归集不到块（非本端发起 / 断线补帧）：宁可不落，也不当前台正文。
            return True
        if frame_type == "subagent_chunk":
            self.fold.append_body(key, text)
            return True
        # 最终答复帧即「这一跑收尾」的信号：收尾 + 补一次摘要（终态事件可能丢帧）。
        block = self.fold.set_result(key, text, source="frame")
        if block is not None:
            self.fold.finish(key)
            self.print_fold_summary(block)
        return True

    def toggle_latest(self) -> bool:
        """Ctrl+O：在输入区上方展开/收起最近折叠块（可重绘区，不是滚动区）"""
        expanded = self.fold.expanded_blocks()
        if expanded:
            for block in expanded:
                block.expanded = False
            self.background_spinner.request_redraw()
            return False
        latest_block = self.fold.latest()
        if latest_block is None:
            self.background_spinner.request_redraw()
            return False
        latest_block.expanded = True
        self.background_spinner.request_redraw()
        return True

    def expanded_overlay_lines(
        self,
        *,
        max_lines: int = 28,
        max_width: int = 100,
    ) -> list[tuple[str, str]]:
        """Ctrl+O 展开态：输入区上方可重绘明细行 ``(style, text)``；无展开则空。"""
        from src.cli.terminal_width import truncate_to_width
        from src.cli.theme import fg as _fg

        blocks = self.fold.expanded_blocks()
        if not blocks:
            return []
        colors = {
            "title": _fg("text.tool"),
            "group": _fg("text.system_label"),
            "entry": _fg("text.tool"),
            "body": _fg("text.assistant"),
        }
        rows: list[tuple[str, str]] = []
        width = max(20, int(max_width or 100))
        limit = max(4, int(max_lines or 28))
        for block in blocks:
            for item in self.fold.detail_items(block):
                if item.display_blocks:
                    rows.append((colors["entry"], "  [diff]"))
                    continue
                style = colors.get(item.style, colors["body"])
                for line in item.text.splitlines() or [""]:
                    rows.append((style, truncate_to_width(line, width)))
                    if len(rows) >= limit:
                        rows.append((colors["group"], "  …（更多用 /detail）"))
                        return rows
        return rows

    def print_fold_summary(self, block: Any) -> None:
        """摘要行——一次 delegate 运行在滚动区唯一默认可见的产出（只打一次）。"""
        if getattr(block, "summary_printed", False):
            return
        block.summary_printed = True
        from src.cli.theme import fg as _fg

        line = f"[{_fg('text.tool')}]{_rich_escape(self.fold.summary_line(block))}[/{_fg('text.tool')}]"
        self.background_spinner.write_echo_lines([line])
        self.background_spinner.request_redraw()

    def detail_candidates(self) -> list[Any]:
        """`/detail` 补全候选（最近在前）——端上命令的候选源，供 picker 取。"""
        return self.fold.detail_candidates()

    def print_fold_detail(self, keyword: str = "") -> bool:
        """``/detail [关键词]``：按类型/任务摘要定位一块，明细**落进滚动区**永久回看"""
        block = self.fold.find_block(keyword)
        if block is None:
            return False
        self._write_fold_detail(block)
        return True

    def _write_fold_detail(self, block: Any) -> None:
        """把一块的三组明细补打到滚动区（仅 ``/detail`` 用；Ctrl+O 走可重绘层）。"""
        from src.cli.theme import fg as _fg
        from src.coara.tool_output.pipeline import deserialize_display_blocks, render_terminal_blocks

        colors = {
            "title": _fg("text.tool"),
            "group": _fg("text.system_label"),
            "entry": _fg("text.tool"),
            "body": _fg("text.assistant"),
        }
        CliScrollback.write("")
        for item in self.fold.detail_items(block):
            if item.display_blocks:
                render_terminal_blocks(deserialize_display_blocks(item.display_blocks))
                continue
            color = colors.get(item.style, colors["body"])
            for line in item.text.splitlines() or [""]:
                CliScrollback.write_markup(f"[{color}]{_rich_escape(line)}[/{color}]")
        CliScrollback.write("")
        block.detail_printed = True
        self.background_spinner.request_redraw()

    def begin_turn(self) -> None:
        """Start channel A streaming for a user turn."""
        self.background_spinner.mark_turn_started()
        self.background_spinner.start_streaming()

    def finish_turn(self) -> None:
        """同步兜底入口：无事件循环时退化为旧序（先撤状态、后落内容）。"""
        force_needed = self.background_spinner.retire_turn_display()
        flushed = self._flush_turn_content()
        self.background_spinner.finish_turn_settle(force=force_needed and not flushed)
        self.background_spinner.clear_turn_timer()

    async def finish_turn_async(self) -> None:
        """有序收尾：撤活动树与 Thinking 状态 → 正文经 proxy 落屏 → 终帧静止"""
        # Ordered finish: retire tree/thinking → flush body → settle final frame
        self.subagent_spinner.clear_root_status()
        force_needed = self.background_spinner.retire_turn_display()
        flushed = await self.background_spinner.run_in_terminal_flush(self._flush_turn_content)
        self.background_spinner.finish_turn_settle(force=force_needed and not flushed)
        self.background_spinner.clear_turn_timer()

    def _flush_turn_content(self) -> bool:
        """收尾内容统一出口（``run_in_terminal_flush`` 的 write 回调）。"""
        flushed = self.flush_pending_fg_diffs()
        flushed = self.background_spinner.flush_finished_tool_history() or flushed
        flushed = self.background_spinner.flush_streaming_tail() or flushed
        return flushed

    def wire(self, event_bus: EventBus) -> list[Subscription]:
        """Register all CLI display EventBus subscribers."""
        self.subagent_spinner.bind_session(self.root.foreground_coara.session_id)
        if self.background_tool_history is None:
            from src.cli.background_tool_history import BackgroundToolHistory

            self.background_tool_history = BackgroundToolHistory(self.root)
        self.background_spinner.bind_background_tool_history(self.background_tool_history)
        # 子智能体折叠块：过程行的去向（spinner flush 时转交）与 Ctrl+O 展开入口。
        self.background_spinner.bind_subagent_fold(self)
        subs: list[Subscription] = []

        def sub(callback, topic: str | None = None) -> None:
            # 模块会话（FlowRoot 构建对话、配置助手等）是独立主体：只在 WebUI 显示，CLI 展示层统一跳过其全部 trace
            # 事件（subject 非主会话取值）。
            def _guarded(event: Any) -> None:
                payload = getattr(event, "payload", None) or {}
                if str(payload.get("subject") or "") not in ("", "root"):
                    return
                callback(event)

            subs.append(event_bus.subscribe(_guarded, topic=topic))

        sub(self._on_truncation_recovery, topic="output_truncation_recovery")

        for topic in _ACTIVITY_TREE_TOPICS:
            sub(self._on_activity_event, topic=topic)

        for topic in _WORKSPACE_REGISTRY_TOPICS:
            sub(self._on_workspace_registry_event, topic=topic)

        sub(self._on_background_agent_complete, topic="background_agent_complete")
        # event_turn_*: spinner status only (scrollback text = remote_sync)
        sub(self._on_event_turn_complete, topic="event_turn_complete")
        sub(self._on_continuation_input_received, topic="continuation_input_received")
        sub(self._on_continuation_input_injected, topic="continuation_input_injected")
        sub(self._on_session_auto_new, topic="session_auto_new")
        sub(self._on_session_started_notice, topic="session_started")
        # llm_switched：chrome/状态栏由 root_shim 刷新；CLI 滚动区不上屏（与 session_started 同策）
        sub(self._on_llm_switched_notice, topic="llm_switched")

        for topic in _SPINNER_REFRESH_TOPICS:
            sub(self.background_spinner.handle_event, topic=topic)

        # 子智能体折叠块：开场/任务指令（终态在各自的收尾处理器里收，见 _on_subagent_terminal）。
        for topic in _FOLD_TOPICS:
            sub(self._on_fold_event, topic=topic)
        sub(self._on_subagent_terminal, topic="subagent_complete")
        sub(self._on_subagent_terminal, topic="subagent_failed")

        # diff 统一走通道帧（queue_diff_frame）：不再订阅 tool_complete 事件。
        for topic in _RUNTIME_CHROME_RESYNC_TOPICS:
            sub(self._on_runtime_chrome_resync, topic=topic)

        return subs

    def _resync_runtime_chrome(self) -> None:
        """Rebind activity session filter and reload foreground chrome caches."""
        fg = self.root.foreground_coara
        # 活动树节点属于旧前台会话：先 flush 已完成工具行再清树重建。
        self.background_spinner.flush_finished_tool_history()
        self.subagent_spinner.clear_status()
        self.subagent_spinner.bind_session(fg.session_id)
        # Workspace switch /new: drop stuck turn chrome so the idle prompt accepts input.
        self.background_spinner.reset_after_runtime_change()
        # 新会话/新空间顺手换一批 Thinking 轮换短语
        from src.records import loading_phrases

        loading_phrases.reshuffle()
        # @ 已改为服务台补全，不再随工作空间切换重绑路径补全器

    def _resync_activity_tree(self) -> None:
        """断连重连/丢帧兜底：清活动树（不清 _finished_blocks），。"""
        self.subagent_spinner._tracker.resync()
        self.background_spinner.request_redraw()

    def _on_runtime_chrome_resync(self, event) -> None:
        if event.event_type not in _RUNTIME_CHROME_RESYNC_TOPICS:
            return
        self._resync_runtime_chrome()

    def _on_activity_event(self, event) -> None:
        """活动事件三路：登记表全量归集；前台空间进活动树；后台空间进工具行通道"""
        registry = self.background_spinner.workspace_registry
        registry.ingest(event)
        if _event_in_foreground_workspace(event, self.root):
            # 已是本端才 ingest（兼容旧事件；attach 未镜像它端活跃态时宁可不显示）。
            payload = event.payload or {}
            src = str(payload.get("source") or payload.get("origin_source") or "").strip().lower()
            if src:
                if _cli_shows_activity_event(event):
                    self.subagent_spinner.handle_event(event)
            else:
                from src.coara.turn_source import cli_shows_foreground_spinner

                fg = getattr(self.root, "foreground_coara", None)
                local_src = getattr(fg, "_active_turn_source", None) if fg is not None else None
                if cli_shows_foreground_spinner(local_src):
                    self.subagent_spinner.handle_event(event)
        elif self.background_tool_history is not None:
            self.background_tool_history.ingest(event)

    def _fold_event_is_local(self, event) -> bool:
        """该事件是不是本端（cli/cli-attached）发起的前台空间活动"""
        if not _event_in_foreground_workspace(event, self.root):
            return False
        payload = getattr(event, "payload", None) or {}
        src = str(payload.get("source") or payload.get("origin_source") or "").strip().lower()
        if src:
            return _cli_shows_activity_event(event)
        from src.coara.turn_source import cli_shows_foreground_spinner

        fg = getattr(self.root, "foreground_coara", None)
        return cli_shows_foreground_spinner(getattr(fg, "_active_turn_source", None) if fg else None)

    def _on_fold_event(self, event) -> None:
        """折叠块的开场：建块、登记归属、收任务指令"""
        topic = event.event_type
        payload = event.payload or {}
        if topic == "user_message":
            # 只认 delegate 任务指令帧（`delegate_brief` / 老标记 `delegate_task`）； 普通用户消息与其它注入帧一律不碰。
            if not (payload.get("delegate_brief") or payload.get("delegate_task")):
                return
            if not self._fold_event_is_local(event):
                return
            key = str(payload.get("parent_tool_call_id") or "")
            if not key:
                return
            self.fold.note_brief(key, str(payload.get("content") or ""))
            return
        if not self._fold_event_is_local(event):
            return
        if topic == "tool_start":
            if str(payload.get("tool_name") or "") != "delegate":
                return
            _raw_args = payload.get("arguments")
            args: dict[str, Any] = _raw_args if isinstance(_raw_args, dict) else {}
            action = str(args.get("action") or "").strip().lower()
            if action not in _FOLD_DELEGATE_RUN_ACTIONS:
                return  # wait / message / stop 不是一次运行，不建块
            if _fold_silent_subagent(str(args.get("subagent_type") or "")):
                return
            self.fold.note_start(
                tool_call_id=str(payload.get("tool_call_id") or ""),
                agent_type=str(args.get("subagent_type") or ""),
                task=str(args.get("description") or ""),
                background=bool(args.get("background")),
            )
            return
        if _fold_silent_subagent(str(payload.get("subagent_type") or "")):
            return
        if topic == "subagent_start":
            self.fold.note_start(
                tool_call_id=str(payload.get("parent_tool_call_id") or ""),
                subagent_id=str(payload.get("subagent_id") or ""),
                coara_id=str(payload.get("child_coara_id") or ""),
                agent_type=str(payload.get("subagent_type") or ""),
                task=str(payload.get("description") or ""),
            )
            return
        if topic == "background_agent_start":
            self.fold.note_start(
                tool_call_id=str(payload.get("parent_tool_call_id") or ""),
                subagent_id=str(payload.get("task_id") or ""),
                coara_id=str(payload.get("child_coara_id") or ""),
                agent_type=str(payload.get("subagent_type") or ""),
                task=str(payload.get("description") or ""),
                background=True,
            )

    def _on_subagent_terminal(self, event) -> None:
        """子智能体终态（subagent_complete / subagent_failed）：收尾 + 打摘要行。"""
        payload = event.payload or {}
        key = self.fold.resolve(
            str(payload.get("subagent_id") or ""),
            str(payload.get("parent_tool_call_id") or ""),
        )
        if not key:
            return
        block = self.fold.finish(
            key,
            failed=event.event_type == "subagent_failed",
            cancelled=_is_cancelled_terminal(event, payload),
        )
        if block is not None:
            self.print_fold_summary(block)

    def _on_workspace_registry_event(self, event) -> None:
        """动作摘要/回合收尾事件：只喂登记表，并标脏让状态行及时出现/消失。"""
        registry = self.background_spinner.workspace_registry
        registry.ingest(event)
        self.background_spinner.request_redraw()

    def _on_truncation_recovery(self, event) -> None:
        if event.event_type != "output_truncation_recovery":
            return
        if not _session_matches_root(event, self.root):
            return
        notice = str((event.payload or {}).get("user_notice") or event.message or "").strip()
        if not notice:
            return
        CliScrollback.write("")
        CliScrollback.write(notice, style="yellow")
        CliScrollback.write("")

    def queue_diff_frame(self, frame: dict) -> None:
        """通道 diff 帧 → 暂存前台 diff（✓ 工具行到达时 flush 渲染）"""
        display_blocks = frame.get("display_blocks")
        if not display_blocks:
            return
        parent_call = str(frame.get("parent_tool_call_id") or "").strip()
        if parent_call:
            key = self.fold.resolve(parent_call) or parent_call
            self.fold.add_diff(
                key,
                tool_call_id=str(frame.get("tool_call_id") or ""),
                display_blocks=display_blocks,
                tool_name=str(frame.get("tool_name") or ""),
            )
            self.background_spinner.request_redraw()
            return
        frame_turn_id = str(frame.get("turn_id") or "").strip()
        if frame_turn_id:
            fg = getattr(self.root, "foreground_coara", None)
            streams = getattr(fg, "_turn_streams", None)
            has_active_local_turn = (
                any(not s.get("done") for s in streams.values()) if isinstance(streams, dict) else False
            )
            if not has_active_local_turn:
                # 迟到帧（回合已结束）：立即渲染，不押队列（不押到下一回合）。
                from src.coara.tool_output.pipeline import render_terminal_from_event

                render_terminal_from_event(is_error=False, display_blocks=display_blocks)
                self.background_spinner.request_redraw()
                return
        self._pending_fg_diffs.append(_PendingFgDiff(display_blocks=display_blocks))

    def _on_background_agent_complete(self, event) -> None:
        if event.event_type != "background_agent_complete":
            return
        if not _session_matches_root(event, self.root):
            return
        payload = event.payload or {}
        # Quiet system agents: no scrollback completion banner (janitor/daily 完全静默).
        if str(payload.get("subagent_type") or "") in {"janitor", "daily"}:
            return
        # 无 origin_source 的旧事件（兼容期）保留显示，宁多勿丢。
        from src.coara.turn_source import cli_shows_source

        origin = str(payload.get("origin_source") or "").strip().lower()
        if not cli_shows_source(origin):
            return
        # 折叠块接管：这次子智能体运行的产出都进折叠块，横幅不再单独打 （摘要行已代表它；横幅留着就是同一件事两行）。
        fold_key = self.fold.resolve(
            str(payload.get("task_id") or ""),
            str(payload.get("parent_tool_call_id") or ""),
        )
        if fold_key:
            fold_block = self.fold.finish(fold_key, failed=bool(payload.get("has_error", False)))
            if fold_block is not None:
                self.print_fold_summary(fold_block)
                return
        subagent_type = CliScrollback.esc(str(payload.get("subagent_type") or "unknown"))
        description = CliScrollback.esc(str(payload.get("description") or ""))
        has_error = payload.get("has_error", False)
        result_preview = CliScrollback.esc(str(payload.get("result_preview") or ""))
        if has_error:
            msg = f"☰ [来自 {subagent_type}] 后台任务失败：{description}"
        else:
            msg = (
                f"☰ [来自 {subagent_type}] {result_preview}"
                if result_preview
                else f"☰ [来自 {subagent_type}] {description}"
            )
        CliScrollback.write_html(f"<bold><cyan>\n{msg}\n</cyan></bold>")

    def _on_event_turn_complete(self, event) -> None:
        if event.event_type != "event_turn_complete":
            return
        if event.coara_id != self.root.identity.coara_id:
            return
        # Inbound auto_run turns bypass chat_runner begin_turn/finish_turn — drain leftovers here.
        self.background_spinner.clear_turn_timer()
        self.background_spinner.flush_finished_tool_history()
        self.subagent_spinner.clear_root_status()

    def _on_continuation_input_received(self, event) -> None:
        if event.event_type != "continuation_input_received":
            return
        if not _session_matches_root(event, self.root):
            return
        # 各端显示独立：后台任务完成横幅只在本端（cli/cli-attached）发起的任务上 显示——web/matrix 发起的 aide/coaras
        # 完成结果回投它们各自端，CLI 不镜像。
        from src.coara.turn_source import cli_shows_source

        payload = getattr(event, "payload", None) or {}
        origin = str(payload.get("source") or "").strip().lower()
        if not cli_shows_source(origin):
            return
        self.background_spinner.request_redraw()

    def _on_continuation_input_injected(self, event) -> None:
        """Echo drained user follow-ups (bold) + subagent results into scrollback"""
        if event.event_type != "continuation_input_injected":
            return
        if not _session_matches_root(event, self.root):
            return
        payload = event.payload or {}
        # 回显块经流式块间距语义写屏（Rich markup 单通道 FIFO 与正文同序）。
        from rich.markup import escape as _escape_markup

        lines: list[str] = []
        from src.cli.theme import fg as _fg

        user_label_c = _fg("text.user")
        user_content_c = _fg("text.user_content")
        user_texts = payload.get("user_texts") or []
        user_sources = payload.get("user_sources") or []
        if isinstance(user_texts, list):
            for index, raw in enumerate(user_texts):
                text = str(raw or "").strip()
                if not text:
                    continue
                # 跟话自身来源优先；旧 payload 无 user_sources 时回退到当前回合来源。
                item_source = ""
                if isinstance(user_sources, list) and index < len(user_sources):
                    item_source = str(user_sources[index] or "").strip()
                if not item_source:
                    item_source = str(getattr(self.root.foreground_coara, "_active_turn_source", "") or "")
                # 各端显示独立：后台任务完成横幅只在本端（cli/cli-attached）发起的任务上 显示——web/matrix 发起的
                # aide/coaras 完成结果回投它们各自端，CLI 不镜像。
                from src.coara.turn_source import cli_shows_source

                if not cli_shows_source(item_source):
                    continue
                sys_body = _extract_system_body(text)
                if sys_body is not None:
                    # 后台结果 / 系统消息注入：只进模型，不走「系统：」气泡行； 前台子智能体结果有折叠块时走下方
                    # subagent_texts。
                    continue
                else:
                    lines.append(
                        f"[{user_label_c}]你：[/{user_label_c}]"
                        f"[{user_content_c}]{_escape_markup(text)}[/{user_content_c}]"
                    )
        # Foreground delegate final + report() → model queue and scrollback
        sub_texts = payload.get("subagent_texts") or []
        sub_sources = payload.get("subagent_sources") or []
        if isinstance(sub_texts, list):
            from src.core.message_tags import subagent_scrollback_label

            for index, raw in enumerate(sub_texts):
                text = str(raw or "").strip()
                if not text:
                    continue
                # 跟话自身来源优先；旧 payload 无 user_sources 时回退到当前回合来源。
                item_source = ""
                if isinstance(sub_sources, list) and index < len(sub_sources):
                    item_source = str(sub_sources[index] or "").strip()
                if not item_source:
                    item_source = str(getattr(self.root.foreground_coara, "_active_turn_source", "") or "")
                from src.coara.turn_source import cli_shows_source

                if not cli_shows_source(item_source):
                    continue
                # 最终结果是折叠块的一组，不再单打一行（「这一行是唯一默认产出」）。
                from src.core.message_tags import subagent_task_id

                fold_key = self.fold.resolve(subagent_task_id(text))
                if fold_key:
                    block = self.fold.finish(fold_key)
                    if block is not None:
                        self.fold.set_result(fold_key, text, source="echo")
                        self.print_fold_summary(block)
                    continue
                label = subagent_scrollback_label(text)
                label_c = _fg("text.system_label")
                content_c = _fg("text.system")
                lines.append(
                    f"[{label_c}]{_escape_markup(label)}：[/{label_c}]"
                    f" [{content_c}]{_escape_markup(text)}[/{content_c}]"
                )
        if lines:
            # 回显经流式块间距语义写屏（与正文 yield 同一套规则 顺序不会错位）
            self.background_spinner.write_echo_lines(lines)
        self.background_spinner.request_redraw()

    def _on_session_auto_new(self, event) -> None:
        # CLI 不显示空闲自动新会话提示：刷新等下次对话自然发生，滚动区不打断
        return

    def _on_session_started_notice(self, event) -> None:
        # CLI 不显示他端新会话提示：与对话内容无关的元事件不上屏
        return

    def _on_llm_switched_notice(self, event) -> None:
        # CLI 不显示他端切模型提示：chrome 由 root_shim 刷新，滚动区不上屏
        return
