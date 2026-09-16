"""Trace 事件广播 mixin — 从 ``web_server.py`` 拆出。

EventBus → WS 的实时推送域：同步回调 ``_on_trace_event`` 过滤并按各端视图
序列化 trace 事件，入批后经 100ms 微批 flush（``_trace_flush_loop`` /
``_flush_trace_batch``）定向推给浏览器（单活跃）与各 attach 连接（按 pin
空间视图）。

端独立契约（发送端挡）——**默认端独立，例外显式列出**：
- **端作用域（绝大多数）**：回合/工具/跟话/思考/flow 生命周期等按
  ``source`` / ``origin_source`` / ``subagent_origin`` 路由——attach 只收
  cli*，浏览器只收 web*；无来源严格丢弃。各端显示可以不一样。
- **空间 chrome 例外** ``llm_switched`` / ``plan_mode_changed``：仅共看
  **同一空间/会话**的连接同收（模型显示统一 / 计划模式状态统一）；未共看的
  端/其它空间不收。不是全内核广播。
- **不进 attach** ``workspace_switched``：切空间是端/连接私有；chrome 由
  发起方 ``command_result`` 更新。
- **仅浏览器** ``flow_graph_changed``：工作流画布。
- **同空间会话门禁** ``session_started`` / ``session_auto_new``：只推 pin
  到该会话空间的连接（/new 共看要跟），不是跨空间全局。

隐式契约（由 WebServer 主类提供）：
- ``self.root``：RootCoara（``_sessions``、事件归属查询）
- ``self.registry`` / ``self.attach_registry``：浏览器与 attach 连接注册表
- ``self._view_coara()``：浏览器 D6 视图 coara（detached 判定基准）
- ``self._module_roots``：模块主体缓存（同空间不算 detached）
- ``self._trace_batch`` / ``self._trace_batch_lock`` / ``self._trace_flush_task``：
  批缓冲与 flush 任务（``__init__`` 初始化，``stop()`` 取消/兜底 flush）
- ``self._normalize_tool_ws_fields``：工具事件字段名归一（hydrate 共用）
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

from src.core.logger import logger
from src.ui.handler_contract import HandlerMixinBase

# 按注入/发起端路由：它端不需要、发了只会乱活动树/spinner/气泡。
_END_SCOPED_TRACE_TYPES = frozenset(
    {
        "tool_start",
        "tool_complete",
        "tool_call",
        "tool_result",
        "turn_start",
        "turn_end",
        "llm_turn_start",
        "llm_request_start",
        "llm_turn_complete",
        "thinking_progress",
        "user_message",
        "chat_chunk",
        "conversation_message",
        "chat_turn_retracted",
        "continuation_input_received",
        "continuation_input_injected",
        "completed",
        "turn_failed",
        "turn_interrupted",
        "event_turn_start",
        "event_turn_complete",
        "subagent_start",
        "subagent_complete",
        "subagent_failed",
        "background_agent_start",
        "background_agent_complete",
        "background_task_complete",
        "output_truncation_recovery",
        "error",
        # orchestrator 生命周期：CLI 活动树要，但只跟本端发起的 flow。
        "flow_started",
        "flow_finished",
        # 预留活动树节点（当前仓内几乎无 emit）；有 source 时按端挡。
        "process_spawned",
        "coara_created",
        "coara_terminated",
    }
)

# 仅浏览器（不进 attach）：工作流页画布增量。
_BROWSER_ONLY_TRACE_TYPES = frozenset({"flow_graph_changed"})

# 不进 attach WS：切空间 chrome 由发起连接的 command_result 更新；
# 广播会误伤「仍 pin 在 previous 的其它 attach」。内核 EventBus 本地订阅仍可收。
_ATTACH_SKIP_TRACE_TYPES = frozenset({"workspace_switched"})

# 空间级 chrome 同步（非「全内核一份」）：共看**同一空间**的各端同收。
# llm_switched — 同空间模型显示；plan_mode_changed — 同会话计划模式开关；
# session_* — 该会话起止。
# workspace_switched 不在此列（见上）。


# 关行事件跨视图放行：子智能体/后台 agent 启动按当前视图过滤后，用户切走视图
# 会把 complete/failed 也过滤掉，活动树行永远不关（09-09 审计 P1-3）。这类事件
# 不在消息区渲染，只喂活动树状态机——前端按 subagent_id/task_id 匹配关行，无
# 匹配即忽略；放行不会污染聊天/侧栏（SUBAGENT_TREE_TYPES 与侧栏集合均不含它）。
_CROSS_VIEW_CLOSE_TYPES = frozenset({"subagent_complete", "subagent_failed", "background_agent_complete"})


class TraceBroadcastHandlers(HandlerMixinBase):
    # ------------------------------------------------------------------
    # Trace event broadcast (EventBus → WS)
    # ------------------------------------------------------------------

    # 宿主 WebServer 提供的批缓冲与 flush 任务（组合后才有，见模块 docstring）
    _trace_batch: list[dict[str, Any]]
    _trace_flush_task: asyncio.Task[None] | None

    # 外挂 CLI 完整事件流的必传 topic 全集（原主 CLI 界面召回：活动树/spinner/
    # diff/续接回显都靠它驱动）。浏览器集合不变（其前端按既有集合实现）。
    _ATTACH_TRACE_TOPICS = frozenset(
        {
            # workspace_switched：不进 attach（见 _ATTACH_SKIP_TRACE_TYPES）
            "session_started",
            "output_truncation_recovery",
            "subagent_start",
            "subagent_complete",
            "subagent_failed",
            "flow_started",
            "flow_finished",
            "background_agent_start",
            "background_agent_complete",
            "background_task_complete",
            "tool_start",
            "tool_complete",
            "process_spawned",
            "coara_created",
            "coara_terminated",
            "thinking_progress",
            "llm_turn_complete",
            "llm_request_start",
            "llm_switched",
            "plan_mode_changed",
            "event_turn_start",
            "event_turn_complete",
            "continuation_input_received",
            "continuation_input_injected",
            "session_auto_new",
            "completed",
            "turn_failed",
            "turn_interrupted",
            "user_message",
            "chat_chunk",
            "conversation_message",
            "turn_start",
            "turn_end",
            "tool_call",
            "tool_result",
            "llm_turn_start",
            "error",
            "chat_turn_retracted",
        }
    )

    def _on_trace_event(self, event: Any) -> None:
        """Forward trace events to the active browser connection via WS.

        Called synchronously by EventBus. We extract relevant fields and
        enqueue them for batched flushing (every 100ms) to avoid creating
        hundreds of asyncio tasks during active streaming.
        """
        event_type = getattr(event, "event_type", "")
        for_browser = event_type in (
            "tool_start",
            "tool_call",
            "tool_result",
            "tool_complete",
            "turn_start",
            "turn_end",
            "user_message",
            "chat_chunk",
            "chat_turn_retracted",
            "llm_turn_start",
            "llm_switched",
            "error",
            "session_auto_new",
            "continuation_input_injected",
            "flow_graph_changed",
            "subagent_start",
            "subagent_complete",
            "subagent_failed",
        )
        for_attach = (
            event_type in self._ATTACH_TRACE_TOPICS
            and event_type not in _BROWSER_ONLY_TRACE_TYPES
            and event_type not in _ATTACH_SKIP_TRACE_TYPES
        )
        if not for_browser and not for_attach:
            return
        if for_browser and not self.registry.has_active():
            for_browser = False
        if for_attach and not self.attach_registry.has_connections():
            for_attach = False
        if not for_browser and not for_attach:
            return
        # conversation_message(assistant 最终全文) 不推 attach：attach 正文单一
        # 事实源是 chunk 流 + turn_end。这条事件全文只是 trace/录像带素材，推到
        # attach 会在断连重连窗口把已流式上屏的正文再完整渲染一遍（双显根因）。
        if event_type == "conversation_message" and for_attach:
            for_attach = False
            if not for_browser:
                return
        # turn_start/turn_end for web-originated turns are sent directly via WS
        # in _handle_chat (bypassing trace batching) to avoid flush races that
        # can lose turn_end and leave the browser stuck in "queued" state.
        # Skip them here so the browser doesn't receive duplicates.
        event_payload = getattr(event, "payload", None) or {}
        if event_type in ("turn_start", "turn_end") and event_payload.get("source") == "web":
            for_browser = False
            if not for_attach:
                return
        # diff 帧统一走 EndRegistry.deliver（sender emit "diff" 帧，经 TurnStream
        # 落视图 + WS）；不再经 tool_complete 事件落盘（旧分散路径已剥离）。
        # 过滤 + detached 判定共用一套函数：浏览器按 web 视图，attach 各连接
        # 按其 pin 空间视图（D6 对齐）。
        browser_payload: dict[str, Any] | None = None
        if for_browser:
            browser_payload = self._serialize_trace_event(event, self._trace_view_coara())
            if browser_payload is None:
                for_browser = False
        attach_payloads: dict[str, dict[str, Any]] = {}
        if for_attach:
            attach_payloads = self._attach_trace_payloads(event)
            if not attach_payloads:
                for_attach = False
        if not for_browser and not for_attach:
            return
        # Enqueue for batched flush — avoids creating a new asyncio task per event.
        # This sync callback cannot await _trace_batch_lock, and it doesn't need
        # to: _flush_trace_batch's locked section awaits nothing, so this append
        # always lands either before or after its atomic swap on the event loop.
        self._trace_batch.append({"browser": browser_payload if for_browser else None, "attach": attach_payloads})
        self._ensure_trace_flush_task()

    def _trace_view_coara(self) -> Any:
        """浏览器 D6 视图的 coara（过滤/detached 判定基准）。"""
        try:
            return self._view_coara()
        except Exception:
            return None

    @staticmethod
    def _end_source_allows(event_type: str, event_payload: dict[str, Any], *, loose: bool) -> bool:
        """端作用域事件：发送端按来源挡；全局 topic 一律放行（再走空间门禁）。"""
        if event_type not in _END_SCOPED_TRACE_TYPES:
            return True
        from src.coara.turn_source import cli_shows_source, resolve_trace_end_source, web_shows_source

        src = resolve_trace_end_source(event_payload)
        if loose:
            if not src:
                # 子智能体工具事件若未继承父 source：靠 origin_scope + 后续空间门禁
                # （is_local_subagent）归属，避免 attach 活动树丢 ✓；其它无来源仍挡。
                return str(event_payload.get("origin_scope") or "") == "subagent_loop"
            return cli_shows_source(src, unknown=False)
        return web_shows_source(src, unknown=False)

    def _serialize_trace_event(
        self, event: Any, view_coara: Any, *, loose: bool = False, turn_id: str | None = None
    ) -> dict[str, Any] | None:
        """把 TraceEvent 序列化成 WS 帧（浏览器/attach 共用，单一实现防漂移）。

        ``view_coara``：该端视图空间的 coara——非本端视图的事件标 detached
        （模块主体 FlowRoot 等与视图同空间，不算 detached）。
        ``loose``：attach 端宽松模式——无归属线索（既无 session_id 也无
        workspace_dir）的事件放行（subagent_start 等生命周期事件由父会话
        代发不带 sid，硬过滤会丢）；显式归属其它空间/会话的事件仍过滤。
        返回 None 表示该事件不推给该端。
        """
        if view_coara is None:
            return None
        event_type = getattr(event, "event_type", "")
        event_payload = getattr(event, "payload", None) or {}
        cross_view_close = not loose and event_type in _CROSS_VIEW_CLOSE_TYPES
        if not cross_view_close and not self._end_source_allows(event_type, event_payload, loose=loose):
            return None
        sid = str(event_payload.get("session_id") or "")
        # 模块主体（FlowRoot 等）与视图同空间工作，不算 detached
        module_sids = {r.session_id for r in self._module_roots.values()}
        event_ws = str(event_payload.get("workspace_dir") or "").strip()
        view_ws = str(getattr(view_coara, "workspace_dir", "") or "").strip()
        view_sid = str(getattr(view_coara, "session_id", "") or "")
        sid_matches = bool(sid) and (sid in module_sids or sid == view_sid)
        ws_matches = bool(event_ws and view_ws) and Path(event_ws).resolve() == Path(view_ws).resolve()
        # 本空间子智能体（delegate spawn 的 sa-*，origin_scope=subagent_loop）：
        # sid 不等于视图会话，但属本工作空间——发起端要看到它的工具调用，
        # 视为本视图而非「其它会话」，否则 attach 端 loose 过滤把它丢掉。
        is_local_subagent = bool(sid) and ws_matches and str(event_payload.get("origin_scope") or "") == "subagent_loop"
        same_view = sid_matches or is_local_subagent or (not sid and ws_matches)
        if cross_view_close:
            # 关行事件跨视图放行（见 _CROSS_VIEW_CLOSE_TYPES）——但显式归属
            # 其它空间的关行不复活已随切视图清掉的活动树行（P1-3）：此时丢弃
            # 而非标 detached（detached 分支在下面按 sid 触发，关行事件会走到）。
            if sid and not sid_matches and event_ws and not ws_matches:
                return None
            same_view = True
        if event_type == "llm_switched" and not same_view:
            # 模型 chrome：只推 pin 到该空间的端
            event_wid = str(event_payload.get("workspace_id") or "").strip()
            if event_wid:
                view_wid = ""
                with contextlib.suppress(Exception):
                    manager = getattr(view_coara, "workspace_manager", None) or getattr(
                        getattr(self, "root", None), "workspace_manager", None
                    )
                    if manager is not None and view_ws:
                        view_wid = str(manager.match_path_to_workspace_id(Path(view_ws)) or "")
                if event_wid == view_wid:
                    same_view = True
            if not same_view:
                return None
        if not same_view:
            if sid:
                # 显式 sid 归属其它会话：浏览器端标 detached（中途切空间后离去
                # 会话的流不丢，徽标化展示）；attach 端各连接视图独立，直接过滤。
                if loose:
                    return None
            elif event_ws:
                # 显式 workspace 归属其它空间——任何端都不推。
                return None
            elif not loose:
                # 无归属线索：浏览器严格（宁丢勿错）；attach 宽松放行
                # （父代发的子智能体生命周期事件无 sid，丢了活动树建不起来）。
                return None
        payload: dict[str, Any] = {"type": event_type}
        # Extract common fields safely
        for field in ("tool", "call_id", "summary", "ok", "args", "turn_id", "reason", "message"):
            val = getattr(event, field, None)
            if val is not None:
                payload[field] = val
        # Merge payload fields from the trace event (includes source, text, content, etc.)
        for key, val in event_payload.items():
            if key not in payload and val is not None:
                payload[key] = val
        # Normalize tool event field naming for the WS / hydrate protocol:
        # traces use tool_name / tool_call_id / arguments; browser expects
        # tool / call_id / args (needed for tool_start in-progress rows).
        self._normalize_tool_ws_fields(event_type, payload, event_payload)
        # diff 已统一走帧通道（EndRegistry.deliver → sender → type=diff 帧）：
        # tool_complete 事件不再携带 display_blocks，端只从 diff 帧渲染 diff
        # （单一来源，防双显）。
        if event_type == "tool_complete":
            payload.pop("display_blocks", None)
        # 事件归属事实源：attach 客户端按 coara_id 区分主会话与子智能体。
        coara_id = getattr(event, "coara_id", None)
        if coara_id is not None:
            payload["coara_id"] = coara_id
        # attach 一连接多回合：客户端按 turn_id 归属帧（事件无 turn_id 时补）。
        if turn_id is not None:
            payload.setdefault("turn_id", turn_id)
        if not same_view:
            payload["detached"] = True
            from src.coara.turn_detach import workspace_display_name

            ws_name = workspace_display_name(self.root, str(event_payload.get("workspace_dir") or ""))
            if ws_name:
                payload["workspace_name"] = ws_name
        return payload

    def _attach_trace_payloads(self, event: Any) -> dict[str, dict[str, Any]]:
        """为每个 attach 连接按其 pin 空间视图序列化本事件。conn_id → 帧。

        带 ``channel_id`` 的端作用域事件只投发起连接——同空间多 CLI 互不串
        spinner / 活动树（与 TurnStream 按 conn 定向一致）。
        """
        out: dict[str, dict[str, Any]] = {}
        event_type = getattr(event, "event_type", "")
        if event_type in _ATTACH_SKIP_TRACE_TYPES:
            return out
        event_payload = getattr(event, "payload", None) or {}
        # 同空间模型：只推 pin 到该 workspace_id 的连接
        llm_wid = ""
        if event_type == "llm_switched":
            llm_wid = str(event_payload.get("workspace_id") or "").strip()
        # 仅端作用域事件按 channel_id 定向；llm_switched / session_* 等 chrome
        # 仍同空间共看（即使 payload 因 turn 带了 channel_id）。
        target_channel = ""
        if event_type in _END_SCOPED_TRACE_TYPES:
            target_channel = str(event_payload.get("channel_id") or "").strip()
        for conn_id, workspace_id in self.attach_registry.workspace_targets():
            if llm_wid and workspace_id != llm_wid:
                continue
            if target_channel and conn_id != target_channel:
                continue
            view = self._attach_view_coara(workspace_id)
            payload = self._serialize_trace_event(event, view, loose=True)
            if payload is not None:
                out[conn_id] = payload
        return out

    def _attach_view_coara(self, workspace_id: str) -> Any:
        """attach 连接 pin 空间的视图 coara（该端 D6 视图判定基准）。"""
        try:
            session = self.root._sessions.get(workspace_id)
            return session.coara if session is not None else None
        except Exception:
            return None

    def _ensure_trace_flush_task(self) -> None:
        """Start a background flush task if not already running."""
        if self._trace_flush_task is None or self._trace_flush_task.done():
            self._trace_flush_task = asyncio.create_task(self._trace_flush_loop())

    async def _trace_flush_loop(self) -> None:
        """Flush batched trace events every 100ms.

        Loops until no more events are batched, so events that arrive during
        ``await send_to_active()`` (race window) are not lost. Without this
        loop, a turn_end emitted during the send window would stay in
        ``_trace_batch`` forever because ``_ensure_trace_flush_task`` sees the
        task as still running and never schedules a new flush.
        """
        try:
            await asyncio.sleep(0.1)
            while True:
                if not await self._flush_trace_batch():
                    break
        except asyncio.CancelledError:
            await self._flush_trace_batch()
            return
        except Exception as exc:
            logger.debug(f"Trace flush error: {exc}")

    async def _flush_trace_batch(self) -> bool:
        """Send all batched trace events as a single batch message.

        Returns True if a batch was sent (more may have arrived during the
        send, so the caller should loop), False if nothing to flush.
        """
        # Swap out the batch under the lock to avoid race with _on_trace_event.
        async with self._trace_batch_lock:
            if not self._trace_batch:
                return False
            batch = self._trace_batch
            self._trace_batch = []
        sent = False
        # 浏览器：原协议不变——扁平帧列表一条 trace_batch。
        browser_events = [item["browser"] for item in batch if item.get("browser") is not None]
        if browser_events and self.registry.has_active():
            await self.registry.send_to_active({"type": "trace_batch", "events": browser_events})
            sent = True
        # attach：每连接按其 pin 空间视图的帧集合定向推（{conn_id: [帧...]}）。
        attach_frames: dict[str, list[dict[str, Any]]] = {}
        for item in batch:
            for conn_id, frame in (item.get("attach") or {}).items():
                attach_frames.setdefault(conn_id, []).append(frame)
        for conn_id, frames in attach_frames.items():
            if await self.attach_registry.send_to(conn_id, {"type": "trace_batch", "events": frames}):
                sent = True
        return sent
