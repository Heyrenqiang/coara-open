"""Spinner and status management for the coara CLI."""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import FormattedText, to_formatted_text
from rich.spinner import SPINNERS

from src.cli.activity_live import ActivityLiveTracker
from src.cli.activity_types import ToolCallBlock
from src.cli.background_tasks_display import BackgroundTasksSnapshot, snapshot_running_tasks
from src.cli.input_queue_display import pending_input_hint_lines
from src.cli.scrollback import CliScrollback
from src.cli.streaming import StreamingBlock
from src.cli.terminal_width import (
    display_width,
    fit_three_column,
    format_context_segment,
    truncate_preserving_trailing_paren,
    truncate_to_width,
    wrap_to_width,
)
from src.cli.workspace_activity import WorkspaceActivityRegistry, workspace_color
from src.core.logger import logger
from src.llm.usage import total_prompt_tokens

# Trace events that should mark the prompt/spinner dirty (coalesced in _loop).
# workflow_* 不在列：工作流显示只在 WebUI（CLI 保持安静）。
_SPINNER_REFRESH_EVENT_TYPES = frozenset(
    {
        "tool_start",
        "subagent_start",
        "subagent_complete",
        "subagent_failed",
        "flow_started",
        "flow_finished",
        "background_agent_start",
        "background_agent_complete",
        "background_task_complete",
        "process_spawned",
        "coara_created",
        "coara_terminated",
        "thinking_progress",
        "llm_switched",
        "plan_mode_changed",
        "llm_turn_complete",
        # continuation_input_received 不入列：web/matrix 等其它端的跟话不该
        # 触发 CLI spinner 重绘（各端显示独立）；本端跟话在 submit 时已本地
        # 乐观入队并重绘，无需服务端回发再刷一次。
    }
)
_BACKGROUND_TASK_EVENT_TYPES = frozenset(
    {
        "background_agent_start",
        "background_agent_complete",
        "background_task_complete",
    }
)


class SubagentSpinnerManager:
    """Thin facade over ``ActivityLiveTracker`` (Channel B + C flush rules)."""

    def __init__(self) -> None:
        self._tracker = ActivityLiveTracker()

    def bind_session(self, session_id: str) -> None:
        self._tracker.bind_session(session_id)

    def clear_status(self) -> None:
        self._tracker.clear()

    def clear_root_status(self) -> None:
        self._tracker.clear_root_status()

    def handle_event(self, event) -> None:
        self._tracker.ingest(event)

    def get_status_lines(self) -> list[str]:
        return self._tracker.get_status_lines()

    def get_status_styles(self) -> list[str]:
        return self._tracker.get_status_styles()

    def get_status_rows(self) -> list[tuple[str, str]]:
        return self._tracker.get_status_rows()

    def flush_finished_blocks(self, *, merge: bool = True) -> list[ToolCallBlock]:
        return self._tracker.flush_finished_blocks(merge=merge)

    @staticmethod
    def format_history_line(block: ToolCallBlock, *, subagent_tag: str = "") -> str:
        return ActivityLiveTracker.format_history_line(block, subagent_tag=subagent_tag)

    def has_active_blocks(self) -> bool:
        return self._tracker.has_active_blocks()

    def subagent_tag_for_block(self, block: ToolCallBlock) -> str:
        return self._tracker.subagent_tag_for_block(block)

    def delegate_owner_for_block(self, block: ToolCallBlock) -> str:
        """该完成块属于哪次子智能体运行（delegate 行 call_id）；空串＝主会话工具行。"""
        return self._tracker.delegate_owner_for_block(block)


def _continuous_invalidate_due(lively: bool, tick: int) -> bool:
    """Continuous-refresh pacing: streaming/modal content gets smooth
    ~12.5 Hz; everything else (spinner frame, elapsed timer) runs at ~4 Hz,
    costing two thirds fewer full prompt redraws."""
    return lively or tick % 3 == 0


class BackgroundSpinner:
    """动态提示符 + 底部状态栏管理器。

    参考 kimi-cli 的 prompt 设计：
    - 提示符随状态变化（用排版符号与颜色区分，不用 emoji）
    - 底部状态栏显示 provider、模型、计划模式、子代理状态、快捷键 tips
    """

    _TIPS = [
        "Tab: 补全",
        "/help: 帮助",
        "Ctrl+C: 清空/打断/退出",
        "Shift+Enter: 换行",
    ]
    _TIP_ROTATE_INTERVAL = 30.0
    # Toolbar 会话命中率缓存：events.jsonl 读取放线程池，UI 线程只读缓存
    _SESSION_HIT_RATIO_TTL = 5.0

    def __init__(self) -> None:
        self._session: PromptSession[str] | None = None
        self._task: asyncio.Task | None = None
        self._subagent_spinner: SubagentSpinnerManager | None = None
        self._background_tool_history: Any | None = None
        # 子智能体折叠块：子智能体的过程行不再刷滚动区，而是交给折叠块仓库；
        # 明细的「展开」入口（Ctrl+O）也挂在它上面（display_controller 注入）。
        self._subagent_fold: Any | None = None
        self._paused = False
        self._root = None  # Root coara，用于读取状态
        self._modal_delegates: tuple[Any, ...] = ()
        self._suspended_buffer_document: Document | None = None
        self._streaming_block: StreamingBlock | None = None
        # retire 后待落盘的流式块：finish_turn 链（retire → flush tail → settle）传递
        self._retired_streaming_block: StreamingBlock | None = None
        # Attach 本地回合显示态：begin_turn/start_streaming 置位；收尾顺序见
        # stop_streaming / finish_turn——先落盘最终输出，再撤 spinner，之后静止。
        self._cli_turn_display_active: bool = False
        self._event_turn_label: str | None = None
        self._session_hit_ratio_cache: float | None = None
        self._session_hit_ratio_at: float = 0.0
        self._session_hit_ratio_future: asyncio.Task | None = None
        self._session_hit_ratio_bound_id: str = ""
        # Use Rich's built-in spinner frame list with time-based indexing.
        # This avoids the stutter caused by manual _FRAMES[_frame_idx] cycling
        # in _loop() — the frame is always correct regardless of refresh jitter.
        self._spinner_frames = SPINNERS["dots"]["frames"]
        self._spinner_interval = 0.08
        self._dirty = False
        self._turn_started_at: float | None = None
        self._turn_wall_start: float | None = None
        # 回合有序收尾后短暂吞掉非强制重绘（llm_turn_complete / cache% 回填等），
        # 避免 spinner 刚退后底栏又刷一下造成「又闪」。
        self._turn_end_quiet_until: float = 0.0
        self._quiet_started_at: float = 0.0
        self._cached_background_tasks: BackgroundTasksSnapshot | None = None
        self._cached_background_at: float = 0.0
        self._background_cache_ttl = 0.5
        # When the user is typing (esp. Windows CJK IME), pause spinner-driven
        # redraws so composition is not stomped by ~12.5 Hz invalidate().
        self._last_buffer_activity: float = 0.0
        self._typing_redraw_pause_s = 0.28
        self._buffer_activity_handler: Any | None = None
        # Toolbar workspace segment: foreground_active_name() syncs the
        # workspace_manager (registry path matching) — far too expensive to
        # run on every frame (~12.5 Hz during turns + every keystroke).
        self._cached_ws_mid_text: str = ""
        self._cached_ws_mid_at: float = 0.0
        self._ws_mid_ttl = 1.0
        # 多工作空间状态区：各空间活跃状态登记表（display_controller 喂事件，
        # 渲染时 reconcile 兜底校正）
        self._workspace_registry = WorkspaceActivityRegistry()
        # 本地命令等待态（无回合）：/compact 这类慢命令在等内核回执期间，Thinking
        # 槽改显本字段并转圈——内核在干活但不回任何帧，屏幕不能一片静默。
        self._local_wait_label: str = ""

    @property
    def workspace_registry(self) -> WorkspaceActivityRegistry:
        return self._workspace_registry

    def mark_turn_started(self) -> None:
        """Anchor the unique whole-turn elapsed timer (call once at turn begin).

        Mid-turn UI modes (plan mode, tool batches, delegates) must not call this
        again — the Thinking line is continuous for the entire active turn.
        """
        self._turn_started_at = time.monotonic()
        self._turn_wall_start = time.time()

    def clear_turn_timer(self) -> None:
        self._turn_started_at = None
        self._turn_wall_start = None

    # ── 本地命令等待态（非回合）──

    def begin_local_wait(self, label: str) -> None:
        """进入命令等待态：Thinking 槽改显 *label* 并开始计时。

        用在「端上发命令、内核要跑几十秒才回执」的场景（/compact 压缩长历史）：
        spinner 转起来，用户知道在干活，而不是屏幕一片静默。
        """
        self._local_wait_label = str(label or "").strip()
        self._ensure_turn_timer()
        self.request_redraw(force=True)

    def end_local_wait(self) -> None:
        """退出命令等待态（回执到手或超时）：收走本地等待行与计时。"""
        self._local_wait_label = ""
        self._turn_started_at = None
        self._turn_wall_start = None
        self.request_redraw(force=True)

    def _ensure_turn_timer(self) -> None:
        """Start the turn timer only if missing (never restart mid-turn)."""
        if self._turn_started_at is None:
            self.mark_turn_started()

    def _turn_elapsed_suffix(self) -> str:
        if self._turn_started_at is None:
            return ""
        elapsed = ActivityLiveTracker._format_elapsed(time.monotonic() - self._turn_started_at)
        # Show wall-clock start time for absolute reference
        wall = ""
        if self._turn_wall_start is not None:
            from datetime import datetime

            wall = datetime.fromtimestamp(self._turn_wall_start).strftime("@%H:%M:%S")
        return f" {elapsed}{wall}"

    def bind_session(self, session: PromptSession[str]) -> None:
        if self._session is not None and self._buffer_activity_handler is not None and self._session is not session:
            with contextlib.suppress(ValueError, AttributeError):
                self._session.default_buffer.on_text_changed.remove(self._buffer_activity_handler)
            self._buffer_activity_handler = None

        self._session = session

        def _on_buffer_text_changed(_buffer) -> None:
            self._last_buffer_activity = time.monotonic()

        self._buffer_activity_handler = _on_buffer_text_changed
        session.default_buffer.on_text_changed += _on_buffer_text_changed

    def _typing_pause_active(self) -> bool:
        # 回合已开始后绝不再因「输入框刚被 erase」而挡住 Thinking 重绘——
        # 否则回车后要等 typing_redraw_pause（~0.28s）才见 spinner。
        if self._cli_turn_display_active:
            return False
        if self._last_buffer_activity <= 0:
            return False
        return (time.monotonic() - self._last_buffer_activity) < self._typing_redraw_pause_s

    def bind_subagent_spinner(self, spinner: SubagentSpinnerManager) -> None:
        self._subagent_spinner = spinner

    def bind_background_tool_history(self, history: Any) -> None:
        """后台工作空间工具行通道（display_controller 喂事件，这里只在 flush 点带出）。"""
        self._background_tool_history = history

    def bind_subagent_fold(self, fold: Any) -> None:
        """子智能体折叠块（display_controller 注入）：过程行归集 + Ctrl+O 展开入口。"""
        self._subagent_fold = fold

    def toggle_latest_fold(self) -> bool:
        """Ctrl+O：展开/收起最近一个子智能体折叠块。返回是否真的展开了明细。"""
        fold = self._subagent_fold
        if fold is None:
            return False
        return bool(fold.toggle_latest())

    def bind_root(self, root) -> None:
        self._root = root
        self._workspace_registry.bind_root(root)
        self.sync_foreground_chrome()

    def invalidate_background_tasks_cache(self) -> None:
        self._cached_background_tasks = None
        self._cached_background_at = 0.0

    def invalidate_session_usage_chrome(self) -> None:
        """Drop toolbar context/cache caches (session /new, workspace switch)."""
        self._session_hit_ratio_cache = None
        self._session_hit_ratio_at = 0.0
        self._session_hit_ratio_bound_id = ""
        fut = self._session_hit_ratio_future
        self._session_hit_ratio_future = None
        if fut is not None and not fut.done():
            fut.cancel()

    def _background_tasks_snapshot(self) -> BackgroundTasksSnapshot:
        if self._root is None:
            return BackgroundTasksSnapshot(())
        now = time.monotonic()
        if self._cached_background_tasks is not None and now - self._cached_background_at < self._background_cache_ttl:
            return self._cached_background_tasks
        snapshot = snapshot_running_tasks(self._root)
        self._cached_background_tasks = snapshot
        self._cached_background_at = now
        return snapshot

    def _resolve_context_tokens(self) -> int | None:
        """Toolbar context usage — provider-reported only, never estimated.

        Value = last turn's prompt tokens (incl. cache read/create) plus
        completion tokens. The reply becomes part of history, so this
        approximates the NEXT request's prompt size. Estimation is
        avoided (UI-thread hazard on large restores); the compression guard
        keeps its own accounting in the turn path.

        Fresh sessions / no provider report yet → ``0`` (toolbar still shows
        ``context: 0…`` rather than hiding the segment).
        """
        if self._root is None:
            return 0
        snap = getattr(self._root.foreground_coara, "_llm_usage_snapshot", None)
        if snap is None or not snap.has_reported_input:
            return 0
        usage = snap.usage or {}
        return total_prompt_tokens(usage) + int(usage.get("output_tokens") or 0)

    def _session_cache_hit_ratio(self, fg: Any, snap: Any) -> float | None:
        """Toolbar cache % — 与 WebUI 同源：events.jsonl 当前会话 Σcache/Σprompt。

        events.jsonl 是唯一事实源；此处只做带 TTL 的轻量缓存，读取放到
        线程池避免阻塞 UI 线程。落盘延迟或读取失败时回落内存快照口径，
        两端口径自修复对齐。
        """
        fg_obj = fg if getattr(fg, "session_id", "") else self._root
        session_id = str(getattr(fg_obj, "session_id", "") or "")
        if session_id and session_id != self._session_hit_ratio_bound_id:
            self.invalidate_session_usage_chrome()
            self._session_hit_ratio_bound_id = session_id

        cached = self._session_hit_ratio_cache
        if cached is not None and (time.monotonic() - self._session_hit_ratio_at) < self._SESSION_HIT_RATIO_TTL:
            return cached
        if self._session_hit_ratio_future is None or self._session_hit_ratio_future.done():
            from src.runtime.usage_query import resolve_usage_path_for_root, session_cache_hit_ratio

            if session_id:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop is not None:
                    self._session_hit_ratio_future = loop.create_task(
                        asyncio.to_thread(session_cache_hit_ratio, resolve_usage_path_for_root(self._root), session_id),
                    )

                    def _apply(fut: Any) -> None:
                        if fut.cancelled() or fut.exception() is not None:
                            return
                        self._session_hit_ratio_cache = fut.result()
                        self._session_hit_ratio_at = time.monotonic()
                        self.request_redraw()

                    self._session_hit_ratio_future.add_done_callback(_apply)
        if cached is None and snap is not None:
            return snap.cache_hit_ratio
        return cached

    def request_redraw(self, *, force: bool = False) -> None:
        """Mark prompt/toolbar dirty; invalidate unless the user is mid-typing.

        ``force=True``：必须上屏的一帧。
        静默窗内吞掉底栏/用量类刷新；用户若已开始打字则放行，避免输入无回显。
        """
        if (
            not force
            and self._turn_end_quiet_until
            and time.monotonic() < self._turn_end_quiet_until
            and self._last_buffer_activity <= self._quiet_started_at
        ):
            # 静默开始后用户有击键 → 允许重绘（同步 soft-clear 后的 renderer 状态）
            return
        self._dirty = True
        if self._typing_pause_active():
            return
        if self._session is not None and self._session.app.is_running:
            self._session.app.invalidate()

    def sync_foreground_chrome(self) -> None:
        """Reload session-scoped chrome caches (background TTL, workspace) and redraw."""
        self.invalidate_background_tasks_cache()
        self.invalidate_session_usage_chrome()
        self._cached_ws_mid_at = 0.0
        self.request_redraw()

    def reset_after_runtime_change(self) -> None:
        """Clear mid-turn UI leftovers after workspace switch /new / interrupt.

        Mid-turn ``ws(switch)`` or an interrupted approval can leave ``_paused``,
        streaming, or event-turn chrome stuck so the next idle prompt looks
        frozen (separator visible, keystrokes ignored or not painted).
        """
        self._paused = False
        self._cli_turn_display_active = False
        if self._streaming_block is not None:
            import src.cli.streaming as _streaming_mod

            with contextlib.suppress(Exception):
                self._streaming_block.flush()
            self._streaming_block = None
            _streaming_mod._active_block = None
        self._retired_streaming_block = None
        self._event_turn_label = None
        self.clear_turn_timer()
        self._restore_terminal_title()
        if self._session is not None:
            self._session.app.erase_when_done = False
        self.sync_foreground_chrome()

    def _resolve_model_info(self) -> tuple[str, str]:
        """Foreground provider/model for the toolbar."""
        if self._root is None:
            return "—", "—"
        fg = getattr(self._root, "foreground_coara", self._root)
        provider = str(getattr(fg, "provider_name", None) or "").strip() or "—"
        model = str(getattr(fg, "model_name", None) or "").strip() or "—"
        return provider, model

    def _model_info_has_key(self) -> bool:
        """当前 foreground provider 是否配置了可用 API key。

        内核权威：attached 快照的 provider_has_key（attach 客户端本地探测 env
        不可靠——外挂进程没 load 内核的 .env / providers.yaml inline key，恒判
        无 key，状态栏退化成「/model 添加 provider」且 context 段被门控不显示）。
        快照未带该字段（旧内核/启动竞态）时回退本地探测。
        """
        if self._root is None:
            return False
        fg = getattr(self._root, "foreground_coara", self._root)
        snap_has_key = getattr(fg, "_provider_has_key", None)
        if snap_has_key is not None:
            return bool(snap_has_key)
        pn = str(getattr(fg, "provider_name", None) or "").strip()
        if not pn:
            return False
        try:
            from src.llm.registry import provider_registry

            if not provider_registry.has(pn):
                return False
            from src.cli.first_run_setup import provider_has_usable_key

            return bool(provider_has_usable_key(provider_registry.get(pn)))
        except Exception:
            return False

    def _foreground_is_plan_mode(self) -> bool:
        if self._root is None:
            return False
        fg = getattr(self._root, "foreground_coara", self._root)
        return bool(getattr(fg, "is_plan_mode", False))

    def _get_frame(self, offset: float = 0.0) -> str:
        """Return the current spinner frame, offset by a fractional amount.

        Uses time-based indexing so the frame is always correct regardless
        of how irregularly invalidate() is called.  This eliminates the
        stutter caused by manual _FRAMES indexing in _loop().
        """
        idx = int(time.time() / self._spinner_interval + offset) % len(self._spinner_frames)
        return self._spinner_frames[idx]

    def _get_root_bullet(self, index: int) -> str:
        """Animated prefix for multi-line status rows (staggered per line)."""
        return self._get_frame(index * 0.35)

    def _needs_continuous_refresh(self) -> bool:
        """True when the prompt should keep redrawing for spinner animation."""
        if self._paused:
            return False
        return self._needs_prompt_animation_invalidate()

    def _foreground_cli_spinner_active(self) -> bool:
        """前台有回合且该回合应驱动 CLI Thinking（web 来源除外）。"""
        # 本地命令等待态（无回合）：自己转自己的，不依赖内核回合。
        if self._local_wait_label:
            return True
        # 本端已 begin_turn：立刻转。不要等内核把 _active_turn_source 经
        # turn 事件灌回来——那要多一趟 attach RTT，体感就是回车后半秒才转。
        if self._cli_turn_display_active:
            return True
        if self._root is None:
            return False
        has_turn = getattr(self._root, "has_active_turn", None)
        if not callable(has_turn) or not has_turn():
            return False
        from src.coara.turn_source import cli_shows_foreground_spinner

        fg = getattr(self._root, "foreground_coara", None)
        source = getattr(fg, "_active_turn_source", None) if fg is not None else None
        return cli_shows_foreground_spinner(source)

    def _should_show_streaming_pending(self) -> bool:
        """Live tail in the dynamic area while the local attach turn is open."""
        return self._cli_turn_display_active

    def attach_modal(self, delegate: Any) -> None:
        """Attach a modal delegate to take over the prompt area."""
        # Replace the tuple atomically so concurrent readers never observe a
        # half-mutated delegate list.
        self._modal_delegates = (*self._modal_delegates, delegate)
        self._sync_buffer_state()
        if self._session is not None and self._session.app.is_running:
            self._session.app.invalidate()

    def detach_modal(self, delegate: Any) -> None:
        """Detach a modal delegate."""
        if delegate in self._modal_delegates:
            remaining = list(self._modal_delegates)
            remaining.remove(delegate)
            self._modal_delegates = tuple(remaining)
        self._sync_buffer_state()
        if self._session is not None and self._session.app.is_running:
            self._session.app.invalidate()

    def _active_modal_delegate(self) -> Any | None:
        if not self._modal_delegates:
            return None
        _, delegate = max(
            enumerate(self._modal_delegates),
            key=lambda item: (getattr(item[1], "modal_priority", 0), item[0]),
        )
        return delegate

    def _should_handle_modal_key(self, key: str) -> bool:
        delegate = self._active_modal_delegate()
        return delegate is not None and getattr(delegate, "should_handle_modal_key", lambda k: False)(key)

    def _handle_modal_key(self, key: str, event: Any) -> None:
        delegate = self._active_modal_delegate()
        if delegate is not None:
            handler = getattr(delegate, "handle_modal_key", None)
            if handler is not None:
                handler(key, event)

    def _sync_buffer_state(self) -> None:
        """Save/restore buffer document based on modal state."""
        if self._session is None:
            return
        buffer = self._session.default_buffer
        delegate = self._active_modal_delegate()
        hides = delegate is not None and getattr(delegate, "modal_hides_input_buffer", lambda: False)()

        if hides and self._suspended_buffer_document is None and buffer.text:
            self._suspended_buffer_document = buffer.document
            buffer.set_document(Document(), bypass_readonly=True)
        elif not hides and self._suspended_buffer_document is not None:
            if not buffer.text:
                buffer.set_document(self._suspended_buffer_document, bypass_readonly=True)
            self._suspended_buffer_document = None

    # -- Streaming output ------------------------------------------------------

    def start_streaming(self) -> None:
        """Begin a streaming output block."""
        import src.cli.streaming as _streaming_mod
        from src.cli.theme import get_assistant_text_style, get_tool_error_style, get_tool_text_style

        self._streaming_block = StreamingBlock(
            text_style=get_assistant_text_style(),
            tool_style=get_tool_text_style(),
            error_style=get_tool_error_style(),
        )
        _streaming_mod._active_block = self._streaming_block
        self._cli_turn_display_active = True
        # 清掉回车/erase 触发的 typing 时间戳，立刻画出 Thinking 行
        self._last_buffer_activity = 0.0
        self._ensure_turn_timer()
        self._refresh_title_spinner()  # 额外写标题；主可见态仍在 prompt
        self.request_redraw()

    def ensure_streaming(self) -> None:
        """Open a streaming block if none is active.

        Detached-turn 重挂自愈：reset_after_runtime_change 可能在重挂后清掉
        block（workspace_switched 时序竞争），调用方不得凭自有标志假设 block 还在。
        """
        if self._streaming_block is None:
            self.start_streaming()

    _DEFAULT_TERMINAL_TITLE = "coara 考拉"

    def _write_terminal_title(self, title: str) -> None:
        """OSC 0 写窗口/标签标题（辅通道；主 spinner 仍在 prompt）。"""
        if self._session is None or not self._session.app.is_running:
            return
        try:
            safe = title.replace("\x1b", "").replace("\x07", "").replace("\n", " ")[:120]
            out = self._session.app.output
            out.write_raw(f"\x1b]0;{safe}\x07")
            out.flush()
        except Exception:
            # 有意静默：OSC 0 写终端标题，部分终端不支持属常态，不影响功能
            pass

    def _refresh_title_spinner(self) -> None:
        """回合中同步标题栏（辅）；不可见也不影响 prompt Thinking。"""
        if not self._foreground_cli_spinner_active():
            return
        self._ensure_turn_timer()
        frame = self._get_frame()
        elapsed = self._turn_elapsed_suffix().strip()
        from src.records.loading_phrases import current_phrase

        phrase = truncate_to_width(current_phrase(), 40)
        title = f"{frame} {phrase}"
        if elapsed:
            title = f"{title} {elapsed}"
        self._write_terminal_title(title)

    def _restore_terminal_title(self) -> None:
        self._write_terminal_title(self._DEFAULT_TERMINAL_TITLE)

    def _needs_prompt_animation_invalidate(self) -> bool:
        """连续刷新是否必须 invalidate prompt。"""
        from src.cli.image_paste import is_image_loading

        if is_image_loading():
            return True
        if self._active_modal_delegate() is not None:
            return True
        if self._streaming_block is not None:
            return True
        if self._event_turn_label:
            return True
        if self._subagent_spinner is not None and self._subagent_spinner.has_active_blocks():
            return True
        if self._background_tasks_snapshot().count > 0:
            return True
        if self._workspace_registry.has_rows():
            return True
        return self._foreground_cli_spinner_active()

    def _simple_spinner_soft_exit_cursor_up(self) -> int | None:
        """若可就地擦掉 Thinking 行（不 invalidate），返回从输入行上移的行数。

        仅「Thinking + 分隔线 + 你：」等高退场（与空闲占位同高）时安全。
        """
        if not self._cli_turn_display_active:
            return None
        if self._streaming_block is not None and self._streaming_block.pending_line:
            return None
        if self._event_turn_label:
            return None
        if self._active_modal_delegate() is not None:
            return None
        if self._subagent_spinner is not None and self._subagent_spinner.get_status_rows():
            return None
        fold = self._subagent_fold
        fold_inner = getattr(fold, "fold", None)
        if fold_inner is not None and fold_inner.expanded_blocks():
            return None
        from src.cli.input_queue_display import pending_input_hint_lines

        if pending_input_hint_lines(self._root):
            return None
        if self._background_tasks_snapshot().idle_prompt_line():
            return None
        # Thinking、╌╌ input ╌╌、你： → 光标在「你：」行，上移 2 到 Thinking
        return 2

    def _soft_blank_thinking_line(self, cursor_up: int) -> bool:
        """就地清空 Thinking 行，保留等高空行槽；不调用 app.invalidate()。"""
        if self._session is None or not self._session.app.is_running:
            return False
        if cursor_up < 1:
            return False
        try:
            out = self._session.app.output
            out.cursor_up(cursor_up)
            out.write_raw("\r")
            erase = getattr(out, "erase_end_of_line", None)
            if callable(erase):
                erase()
            else:
                cols = max(1, out.get_size().columns - 1)
                out.write(" " * cols)
                out.write_raw("\r")
            out.cursor_down(cursor_up)
            out.flush()
            return True
        except Exception:
            return False

    def _begin_turn_end_quiet(self) -> None:
        self._quiet_started_at = time.monotonic()
        # 覆盖收尾 flush 后迟到的 llm_turn_complete / cache% 回填（约 0.3s 不够）
        self._turn_end_quiet_until = self._quiet_started_at + 0.85
        self._dirty = False

    @contextlib.contextmanager
    def _suppress_app_invalidate(self):
        """收尾 flush 期间挡住 patch_stdout 直调的 app.invalidate（绕过静默窗）。"""
        if self._session is None or not self._session.app.is_running:
            yield
            return
        app = self._session.app
        original = app.invalidate

        def _noop_invalidate(*_a: Any, **_k: Any) -> None:
            return None

        app.invalidate = _noop_invalidate  # type: ignore[method-assign]
        try:
            yield
        finally:
            app.invalidate = original  # type: ignore[method-assign]

    def retire_turn_display(self) -> bool:
        """撤回 Thinking 的 UI 态并就地清理屏幕；返回是否需要整页 force 收尾。

        「先撤状态、后落内容」：Thinking 从 prompt UI 态消失后，任何后续
        renderer 重绘画出的都是空闲 prompt——收尾 flush 触发的 run_in_terminal
        （erase → 写 → 全量重绘）不再把 Thinking 行闪现一遍再擦掉（收尾闪屏根源）。
        """
        import src.cli.streaming as _streaming_mod

        block = self._streaming_block
        pending = bool(block is not None and block.pending_line)
        # 先判 soft-exit（依赖 _cli_turn_display_active 仍为 True），再开静默窗
        soft_up = None if pending else self._simple_spinner_soft_exit_cursor_up()

        self._begin_turn_end_quiet()
        self._streaming_block = None
        self._retired_streaming_block = block
        if _streaming_mod._active_block is block:
            _streaming_mod._active_block = None
        self._cli_turn_display_active = False
        self._restore_terminal_title()

        # soft-exit 成功就地清理；否则（未提交尾 / soft 失败 / 高度会变）需整页收尾
        return not (soft_up is not None and self._soft_blank_thinking_line(soft_up))

    def flush_streaming_tail(self) -> bool:
        """落盘 retired 流式块的残余正文（正文总在 ✓ 行之后）；返回是否实际写屏。

        tail 为空时 ``block.flush()`` 不产生终端输出、也就没有 run_in_terminal
        重绘——返回 False 让 settle 补一次 force（宁多勿漏，防 Thinking 残留）。
        """
        block = self._retired_streaming_block
        self._retired_streaming_block = None
        if block is None:
            return False
        has_tail = bool(block.pending_line)
        with self._suppress_app_invalidate():
            try:
                block.flush()
            except Exception as exc:  # noqa: BLE001
                from src.core.logger import logger as _logger

                _logger.warning(f"flush streaming tail failed: {exc}", exc_info=True)
                return False
        return has_tail

    def finish_turn_settle(self, *, force: bool) -> None:
        """收尾终帧：retire 未就地清理且无落盘重绘覆盖时，整页落一次空闲态。"""
        if force:
            self.request_redraw(force=True)

    async def run_in_terminal_flush(self, write: Any) -> bool:
        """收尾内容落盘：经 patch_stdout / CliScrollback 正常上屏（落在 prompt 上方）。

        曾用 ``in_terminal`` + 直写 ``app.output`` 做「原子落盘」，但退出时的
        ``renderer.reset/_redraw`` 会盖掉刚写在 UI 区的正文（用户见内容一闪被擦）。
        StdoutProxy 的 ``run_in_terminal`` 才是把文字推到 prompt 上方的权威路径。

        写前排空 proxy 存量保 FIFO；写后再 drain 一次，等本批真正落屏。
        """
        app = self._session.app if self._session is not None else None
        running = app is not None and getattr(app, "_is_running", False)
        if running:
            await self._drain_patch_stdout(app)
        # 不套 direct_output(app.output)：Rich→StdoutProxy→run_in_terminal 即可。
        flushed = bool(write())
        if running:
            await self._drain_patch_stdout(app)
        return flushed

    async def _drain_patch_stdout(self, app: Any, *, timeout: float = 1.0) -> None:
        """等 patch_stdout 队列存量落盘完毕（保住与收尾内容的 FIFO 顺序）。

        ``StdoutProxy`` write thread 的节拍：取批 → ``run_in_terminal``（创建
        future）→ ``sleep(0.2)`` → 回到取批。排空判据 = 队列空 + 无在途
        future，并跨越一个 0.2s sleep 窗确认（否则存量会在收尾重画之后才
        落盘——再擦画一次整屏，且旧文本行插到 ✓ 行之后乱序）。``timeout``
        兜底，宁可带存量收尾也不卡死。
        """
        import sys as _sys

        from prompt_toolkit.patch_stdout import StdoutProxy

        def _proxy_pending() -> bool:
            proxy = _sys.stdout
            try:
                return isinstance(proxy, StdoutProxy) and proxy._flush_queue.qsize() > 0  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                return False

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            future = getattr(app, "_running_in_terminal_f", None)
            if future is not None and not future.done():
                with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(future), timeout=0.25)
                await asyncio.sleep(0.22)  # 覆盖 write thread 落盘后的 sleep(0.2)
                continue
            if _proxy_pending():
                await asyncio.sleep(0.04)
                continue
            # 队列空且无在途：再确认一跳（write thread 可能正合并新批）
            await asyncio.sleep(0.05)
            future = getattr(app, "_running_in_terminal_f", None)
            if (future is None or future.done()) and not _proxy_pending():
                return

    def stop_streaming(self) -> None:
        """撤 Thinking + 落残余正文（独立收尾入口；回合收尾走 finish_turn 拆分链）。

        顺序：先 retire（撤 UI 态 + 就地擦行），再落 tail——落盘触发的重绘
        画出的已是空闲 prompt，Thinking 行不再闪现。
        """
        force_needed = self.retire_turn_display()
        flushed = self.flush_streaming_tail()
        self.finish_turn_settle(force=force_needed and not flushed)

    def write_echo_lines(self, lines: list[str]) -> None:
        """接续注入/子智能体结果回显：经当前流式块的间距语义写屏。

        回显与正文 yield 共用同一套块规则（上下各一空行 顺序按到达先后）；
        无活动流式块时（罕见）退化为直写。
        """
        if self._streaming_block is not None:
            self._streaming_block.write_styled_lines(lines)
            return
        CliScrollback.write("")
        for line in lines:
            CliScrollback.write_markup(line)
        CliScrollback.write("")

    def append_streaming_chunk(self, chunk: str) -> None:
        """Append a chunk of streaming text."""
        if self._streaming_block is not None:
            self._streaming_block.append(chunk)
            self._dirty = True

    def handle_event(self, event) -> None:
        if event.event_type == "event_turn_start":
            payload = event.payload or {}
            origin = str(payload.get("origin_source") or payload.get("source_id") or "event")
            label_map = {
                "matrix": "Matrix",
                "web": "Web",
                "cli": "CLI",
                "event": "事件",
            }
            self._event_turn_label = label_map.get(origin, origin)
            self.mark_turn_started()
            # No StreamingBlock here — assistant text is mirrored by remote_sync
            # (chat_chunk). Streaming both paths used to duplicate scrollback lines.
            self._dirty = True
            return
        if event.event_type == "event_turn_complete":
            self._event_turn_label = None
            self.clear_turn_timer()
            self._dirty = True
            return
        if event.event_type == "tool_complete":
            self.invalidate_background_tasks_cache()
            self._dirty = True
            self.flush_finished_tool_history()
            return
        if event.event_type in _SPINNER_REFRESH_EVENT_TYPES:
            if event.event_type in _BACKGROUND_TASK_EVENT_TYPES:
                self.invalidate_background_tasks_cache()
            if event.event_type == "llm_turn_complete":
                self.invalidate_session_usage_chrome()
            self._dirty = True
            self.request_redraw()
            return

    def flush_finished_tool_history(self) -> bool:
        """Flush completed subagent tool blocks into scrollback (kimi-style).

        子智能体自己的过程行不再直接刷滚动区：它们归到发起它的那次 delegate 运行的
        折叠块里（CLI 端口径对齐 web 折叠区——「子智能体的过程全部进折叠」）。
        主会话自己的工具行照旧落滚动区（本方法也只处理子智能体那段）。

        返回是否有内容落盘（收尾链据此判断是否需要补终帧）。
        """
        flushed = False
        if self._background_tool_history is not None:
            flushed = self._background_tool_history.flush() or flushed
        if self._subagent_spinner is None:
            return flushed
        # 不在这里归并：归并是「滚动区降噪」的手段，而子智能体过程行已经进折叠块
        # （折叠块的项数/明细要保真）。归并留到真要落滚动区的那批之前做。
        blocks = self._subagent_spinner.flush_finished_blocks(merge=False)
        if not blocks:
            return flushed
        fold = self._subagent_fold
        if fold is not None:
            # 折叠只改去向、不改「是否有内容落盘」——折叠块没进滚动区，
            # 不该让收尾链以为需要补终帧。
            owner_of = getattr(self._subagent_spinner, "delegate_owner_for_block", None)
            remaining: list[ToolCallBlock] = []
            for block in blocks:
                owner = str(owner_of(block) or "") if callable(owner_of) else ""
                if not fold.ingest_tool_block(block, owner_key=owner):
                    remaining.append(block)
            if len(remaining) != len(blocks):
                self.request_redraw()
            blocks = remaining
            if not blocks:
                return flushed
        # 落滚动区前归并相邻同类只读工具（与旧行为一致；折叠块不参与）
        from src.cli.activity_types import merge_adjacent_tool_blocks

        blocks = merge_adjacent_tool_blocks(blocks)
        from src.cli.streaming import write_tool_history_html
        from src.cli.theme import get_tool_error_style, get_tool_text_style

        for block in blocks:
            # 单块失败（渲染异常/标记非法等）不丢剩余块：逐块 try 隔离。
            try:
                tag = self._subagent_spinner.subagent_tag_for_block(block)
                line = CliScrollback.esc(SubagentSpinnerManager.format_history_line(block, subagent_tag=tag))
                if block.is_error:
                    err = get_tool_error_style()
                    write_tool_history_html(f'<style fg="{err}">{line}</style>')
                else:
                    tool = get_tool_text_style()
                    write_tool_history_html(f'<style fg="{tool}">{line}</style>')
            except Exception:  # noqa: BLE001
                from src.core.logger import logger as _logger

                _logger.debug(f"flush tool history block failed (skipped): {block.label}", exc_info=True)
        return True

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def _sync_erase_when_done(self) -> None:
        """Keep erase_when_done in sync while prompt_async is blocked (turn may start mid-wait)."""
        if self._session is None or self._root is None:
            return
        self._session.app.erase_when_done = self._cli_turn_display_active or (
            self._root is not None and self._root.has_active_turn()
        )

    async def _loop(self) -> None:
        """Timer-based refresh loop with dirty-flag coalescing.

        Invalidates at ~12.5 Hz when there is active content (via
        ``_needs_continuous_refresh``), and immediately when the dirty
        flag is set (state changed).  When idle, the loop sleeps quietly
        without triggering redraws.

        While the user is typing (buffer changed recently), skip redraws so
        Windows CJK IME composition is not interrupted by spinner frames.
        Dirty stays set and flushes after the typing pause window.
        """
        try:
            tick = 0
            while True:
                await asyncio.sleep(0.08)
                tick += 1
                # 循环体任一步骤抛异常都不能让 loop 死掉——否则 spinner 永久消失
                # 且无迹可查（历史「spinner 突然消失」疑案）。捕住记日志继续转。
                try:
                    if self._session is not None and self._session.app.is_running:
                        self._sync_erase_when_done()
                    if self._paused:
                        continue
                    if self._turn_end_quiet_until and time.monotonic() < self._turn_end_quiet_until:
                        self._dirty = False
                        continue
                    if self._typing_pause_active():
                        continue

                    # 标题辅通道 + prompt Thinking 主通道
                    if self._foreground_cli_spinner_active():
                        self._refresh_title_spinner()

                    should_invalidate = False
                    if self._dirty:
                        self._dirty = False
                        should_invalidate = True
                    elif self._needs_continuous_refresh():
                        lively = self._streaming_block is not None or self._active_modal_delegate() is not None
                        if _continuous_invalidate_due(lively, tick):
                            should_invalidate = True

                    if should_invalidate and self._session is not None and self._session.app.is_running:
                        self._session.app.invalidate()
                except Exception:
                    from src.core.logger import logger as _spinner_logger

                    _spinner_logger.exception("BackgroundSpinner._loop iteration failed (kept alive)")
        except asyncio.CancelledError:
            pass

    # ── 动态提示符 ──

    def _background_ws_segments(self) -> tuple[FormattedText, int]:
        """后台工作空间的紧凑行内段（并入唯一 spinner 行，不单独成行）。

        每空间一段 ``[nx] 等待模型 2m58s``，多段 `` · `` 相连；标签保留空间色
        做辨识，其余与 Thinking 行同款 thinking 样式。返回 (片段, 总显示宽度)。
        """
        rows = [row for row in self._workspace_registry.render_rows() if not row.is_foreground]
        fragments = FormattedText()
        width = 0
        now = time.monotonic()
        for row in rows:
            elapsed = ActivityLiveTracker._format_elapsed(now - row.started_at).replace(" ", "")
            _bright, dim = workspace_color(row.key)
            tag = f"[{row.name}]"
            rest = f" {elapsed}"
            if fragments:
                fragments.append(("class:prompt.thinking", " "))
                width += 1
            fragments.append((dim, tag))
            fragments.append(("class:prompt.thinking", rest))
            width += display_width(tag) + display_width(rest)
        return fragments, width

    def __call__(self):
        """Return prompt message with agent status directly above the input separator.

        Layout (bottom-anchored):
          [optional streaming preview]
          [optional Ctrl+O subagent fold overlay — redrawable expand/collapse]
          [optional event / subagent activity tree]
          Thinking... <elapsed>   ← 整回合连续（空闲同槽空白占一行，防分隔线跳变）
          [optional background tasks / queue hint]
          ── input ──
          你：
        """
        from prompt_toolkit.application.current import get_app_or_none

        from src.cli.theme import fg as _fg

        app = get_app_or_none()
        columns = app.output.get_size().columns if app is not None else 80

        fragments = FormattedText()
        delegate = self._active_modal_delegate()

        if delegate is not None:
            body = getattr(delegate, "render_modal_body", lambda c: "")(columns)
            if body:
                fragments.extend(to_formatted_text(body))
            return fragments

        if (
            not self._paused
            and delegate is None
            and self._streaming_block is not None
            and self._should_show_streaming_pending()
        ):
            pending = self._streaming_block.pending_line
            if pending:
                from src.cli.terminal_width import tail_to_width
                from src.cli.theme import get_assistant_text_style

                # 钉死一行：满宽折行使输入区行高在 chunk 边界 1↔2 跳变
                pending = tail_to_width(pending, max(10, columns - 1))
                fragments.append((get_assistant_text_style(), pending + "\n"))

        # Ctrl+O 折叠明细：画在输入区上方可重绘层（收起即消失，不进滚动区）
        fold = self._subagent_fold
        overlay = getattr(fold, "expanded_overlay_lines", None) if fold is not None else None
        if callable(overlay):
            for style, line in overlay(max_lines=28, max_width=max(20, columns - 1)):
                fragments.append((style, line + "\n"))

        ws_segments: FormattedText = FormattedText()
        ws_width = 0
        if not self._paused:
            ws_segments, ws_width = self._background_ws_segments()

        status_fragments = FormattedText()
        if not self._paused:
            fg_spinner = self._foreground_cli_spinner_active()
            if not fg_spinner and not self._event_turn_label:
                self._turn_started_at = None
                self._turn_wall_start = None
            elif fg_spinner:
                self._ensure_turn_timer()

            status_rows: list[tuple[str, str]] = []
            if fg_spinner and self._subagent_spinner is not None:
                status_rows = self._subagent_spinner.get_status_rows()

            if self._event_turn_label:
                label = truncate_to_width(self._event_turn_label, 36)
                status_fragments.append(
                    ("class:prompt.thinking", f"{self._get_frame()} 事件 · {label}\n"),
                )
            if status_rows:
                max_width = max(10, columns - 6)
                for i, (line, style) in enumerate(status_rows):
                    line = truncate_preserving_trailing_paren(line, max_width)
                    if line.startswith("◌ "):
                        line = f"{self._get_root_bullet(i)} {line[2:]}"
                    status_fragments.append((style or "class:prompt.thinking", line + "\n"))
            if fg_spinner:
                fg = getattr(self._root, "foreground_coara", None)
                active = getattr(fg, "_active_turn", None) if fg is not None else None
                aborted = bool(
                    active is not None and getattr(active, "signal", None) is not None and active.signal.aborted
                )
                # 排队回合（turn_queued 帧已到达、turn_start 未至）：显式「排队中」，
                # 不与正常回合共用思考文案（轮候时转思考会让用户以为模型在跑）。
                queued = False
                streams = getattr(fg, "_turn_streams", None)
                if isinstance(streams, dict):
                    queued = any(not s.get("done") and s.get("queued") for s in streams.values())
                frame = self._get_frame()
                elapsed = self._turn_elapsed_suffix()
                if aborted:
                    status_fragments.append(("class:prompt.thinking", f"{frame} 正在中断…{elapsed}\n"))
                elif queued:
                    status_fragments.append(("class:prompt.thinking", f"{frame} 排队中…{elapsed}"))
                    if ws_segments:
                        status_fragments.append(("class:prompt.thinking", " "))
                        status_fragments.extend(ws_segments)
                    status_fragments.append(("class:prompt.thinking", "\n"))
                else:
                    from src.records.loading_phrases import current_phrase

                    # 本地命令等待态：文案固定为该命令的说明（如「正在压缩…」），
                    # 不掺随机加载短语——用户在等一件明确的事，不是等模型。
                    wait_label = self._local_wait_label or current_phrase()
                    avail = columns - display_width(frame) - display_width(elapsed) - ws_width - 4
                    status_fragments.append(("class:prompt.thinking", f"{frame} "))
                    if avail >= 6:
                        phrase = truncate_to_width(wait_label, avail)
                        status_fragments.append(("class:prompt.thinking", phrase))
                    status_fragments.append(("class:prompt.thinking", elapsed))
                    if ws_segments:
                        status_fragments.append(("class:prompt.thinking", " "))
                        status_fragments.extend(ws_segments)
                    status_fragments.append(("class:prompt.thinking", "\n"))
            elif ws_segments:
                status_fragments.append(("class:prompt.thinking", f"{self._get_frame()} "))
                status_fragments.extend(ws_segments)
                status_fragments.append(("class:prompt.thinking", "\n"))
            elif not self._event_turn_label:
                # 稳态：Thinking 槽常驻占一行，避免回合结束跳变
                status_fragments.append(("", "\n"))
        bg_fragment: tuple[str, str] | None = None
        if not self._paused and self._root is not None:
            bg_line = self._background_tasks_snapshot().idle_prompt_line()
            if bg_line:
                max_width = max(10, columns - 6)
                bg_line = truncate_to_width(bg_line, max_width)
                bg_fragment = ("class:prompt.thinking", f"{bg_line}\n")

        from src.cli.terminal_width import format_input_separator

        input_border = format_input_separator(columns)
        if not self._paused:
            queue_lines = pending_input_hint_lines(self._root)
            if queue_lines:
                max_width = max(10, columns - 6)
                rendered_queue: list[str] = []
                for line in queue_lines:
                    rendered_queue.extend(wrap_to_width(line, max_width))
                fragments.append((_fg("text.user_content"), "\n".join(rendered_queue) + "\n"))

        if status_fragments:
            fragments.extend(status_fragments)
        if bg_fragment is not None:
            fragments.extend(FormattedText([bg_fragment]))
        from src.cli.image_paste import is_image_loading

        if is_image_loading():
            fragments.append(("class:prompt.thinking", f"{self._get_frame()} 图片加载中…\n"))
        fragments.append(("class:running-prompt-separator", input_border))
        fragments.append(("", "\n"))
        from src.cli.theme import fg as _fg

        fragments.append((f"{_fg('text.user')}", "你："))
        return fragments

    # ── 底部状态栏 ──

    def _workspace_mid_text(self) -> str:
        """Toolbar workspace segment, cached (registry sync is expensive)."""
        now = time.monotonic()
        if self._cached_ws_mid_text and now - self._cached_ws_mid_at < self._ws_mid_ttl:
            return self._cached_ws_mid_text
        mid_text = ""
        if self._root is not None:
            wm = getattr(self._root, "workspace_manager", None)
            name = self._root.foreground_active_name() if hasattr(self._root, "foreground_active_name") else None
            if name is None and wm is not None:
                name = getattr(wm, "active_name", None)
            cwd_suffix = getattr(wm, "cwd_display_suffix", lambda: "")() if wm is not None else ""
            if name:
                mid_text = f" {name}/{cwd_suffix} " if cwd_suffix else f" {name} "
            else:
                work_dir = str(self._root.identity.workspace_dir)
                if display_width(work_dir) > 30:
                    # Keep path tail (drive/folder), not the prefix.
                    tail = work_dir
                    while tail and display_width("…" + tail) > 30:
                        tail = tail[1:]
                    work_dir = ("…" + tail) if tail else truncate_to_width(work_dir, 30)
                mid_text = f" {work_dir} "
        self._cached_ws_mid_text = mid_text
        self._cached_ws_mid_at = now
        return mid_text

    def bottom_toolbar(self):
        """Bottom toolbar: provider·model | workspace | context + cache%.

        Layout uses East-Asian display width. When columns are tight, the
        middle workspace name is truncated first; the right segment then
        compactly drops context detail before ever clipping ``cache N%``.
        """
        from prompt_toolkit.application.current import get_app_or_none

        app = get_app_or_none()
        columns = app.output.get_size().columns if app is not None else 80

        left_parts: list[tuple[str, str]] = []

        # 左：provider + 模型（+ 计划模式 / 后台）
        has_key = self._model_info_has_key()
        provider, model = self._resolve_model_info()
        if not has_key:
            # 无可用 API key：状态栏引导 /model，而非显示不可用的默认
            left_parts.append(("class:toolbar.workdir", " /model 添加 provider "))
        else:
            left_parts.append(("class:toolbar.workdir", f" {provider}"))
            left_parts.append(("class:toolbar.separator", "·"))
            left_parts.append(("class:toolbar.workdir", f"{model} "))

        if self._root is not None:
            if self._foreground_is_plan_mode():
                left_parts.append(("class:toolbar.separator", " │ "))
                left_parts.append(("class:toolbar.plan-mode", " ☰ 计划模式 "))

            bg_count = self._background_tasks_snapshot().count
            if bg_count > 0:
                left_parts.append(("class:toolbar.separator", " │ "))
                left_parts.append(("class:toolbar.bg-tasks", f" 后台 {bg_count} "))

        left_text = "".join(text for _, text in left_parts)
        mid_text = self._workspace_mid_text()

        # 右：context（provider 实报）+ cache 命中率；无可用 key 时不显示
        right_text = ""
        if has_key and self._root is not None:
            fg = getattr(self._root, "foreground_coara", self._root)
            try:
                used_tokens = self._resolve_context_tokens()
            except Exception:
                used_tokens = 0
            ctx_window = 0
            try:
                provider_obj = getattr(fg, "provider", None)
                if provider_obj is not None:
                    ctx_window = provider_obj.get_context_window(getattr(fg, "model_name", None))
            except Exception as exc:
                logger.debug(f"取 provider 上下文窗口失败，状态行 ctx 显示 0：{exc}")
            snap = getattr(fg, "_llm_usage_snapshot", None) or getattr(self._root, "_llm_usage_snapshot", None)
            hit_ratio = self._session_cache_hit_ratio(fg, snap)
            # Reserve room for left; mid may be dropped. Compact right if needed.
            right_budget = max(12, columns - display_width(left_text))
            right_text = format_context_segment(
                used_tokens=used_tokens,
                ctx_window=ctx_window,
                cache_hit_ratio=hit_ratio,
                max_width=right_budget,
            )

        left_text, mid_text, right_text, left_pad, right_pad = fit_three_column(
            columns=columns,
            left=left_text,
            mid=mid_text,
            right=right_text,
        )

        parts: list[tuple[str, str]] = [
            ("class:toolbar.separator", "─" * columns),
            ("", "\n"),
        ]
        parts.extend(left_parts)
        if left_pad:
            parts.append(("class:toolbar.separator", " " * left_pad))
        if mid_text:
            parts.append(("class:toolbar.workdir", mid_text))
        if right_pad:
            parts.append(("class:toolbar.separator", " " * right_pad))
        if right_text:
            parts.append(("class:toolbar", right_text))

        return to_formatted_text(parts)
